import argparse
import logging
import os
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import ghl
import rentcast

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("arv.log")],
)


def run_one_address(address: str, repairs: float = None, fee: float = None):
    print(f"\nRunning comps for: {address}")
    data, status = rentcast.get_avm(address)
    if status == "not_found":
        print("RentCast has no record for this address (404).")
        return
    if status == "quota":
        print("RentCast quota exhausted (429). Try again later.")
        return

    result, err = rentcast.compute_arv(data)
    if err:
        print(f"Error: {err}")
        return

    print(f"\nSubject sqft : {result['subject_sqft']}")
    print(f"Conservative : ${result['conservative']:,}")
    print(f"Balanced     : ${result['balanced']:,}")
    print(f"Aggressive   : ${result['aggressive']:,}")
    print(f"Confidence   : {result['confidence']}")
    if result["top_comps"]:
        print("\nTop Comps:")
        for line in result["top_comps"]:
            print(f"  {line}")

    if repairs is not None and fee is not None:
        balanced = result["balanced"] or 0
        mao = balanced * 0.70 - repairs - fee
        print(f"\nMAO (balanced ARV x 0.70 - repairs - fee): ${mao:,.0f}")


def write_results_to_opp(opp_id: str, result: dict, field_map: dict):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    updates = [
        {"id": field_map["ARV Conservative"], "field_value": result["conservative"]},
        {"id": field_map["ARV Balanced"], "field_value": result["balanced"]},
        {"id": field_map["ARV Aggressive"], "field_value": f"${result['aggressive']:,}"},
        {"id": field_map["Top Sold Comps"], "field_value": "\n".join(result["top_comps"])},
        {"id": field_map["Comp Confidence"], "field_value": result["confidence"]},
        {"id": field_map["Comps Run Date"], "field_value": today},
    ]
    ghl.update_opportunity_fields(opp_id, updates)


def get_comps_run_date(opp: dict, field_map: dict) -> datetime | None:
    for cf in opp.get("customFields", []):
        if cf.get("id") == field_map.get("Comps Run Date"):
            val = cf.get("value") or cf.get("fieldValue")
            if val:
                try:
                    return datetime.fromisoformat(str(val)[:10])
                except ValueError:
                    pass
    return None


def run_loop(test_opp_id: str = None):
    cooldown_days = int(os.environ.get("RERUN_COOLDOWN_DAYS", 3))
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", 60))

    logging.info("Discovering pipeline and stage...")
    pipeline_id, stage_id, pipeline_name, stage_name = ghl.find_pipeline_and_stage(
        os.environ["TARGET_PIPELINE_NAME"], os.environ["TARGET_STAGE_NAME"]
    )
    if not pipeline_id:
        logging.error("Pipeline/stage not found. Check TARGET_PIPELINE_NAME and TARGET_STAGE_NAME in .env")
        return

    logging.info(f"Pipeline: {pipeline_name} | Stage: {stage_name}")
    logging.info("Ensuring custom fields exist...")
    field_map = ghl.ensure_custom_fields()

    if test_opp_id:
        logging.info(f"TEST MODE: will only process opportunity {test_opp_id}")

    while True:
        try:
            logging.info("Starting sweep...")
            page = 1
            total_processed = 0

            while True:
                opps, meta = ghl.get_opportunities(pipeline_id, stage_id, limit=100, page=page)
                if not opps:
                    break

                if page == 1:
                    total = meta.get("total", len(opps))
                    logging.info(f"Deals in stage: {total}")

                for opp in opps:
                    opp_id = opp["id"]
                    opp_name = opp.get("name", opp_id)

                    if test_opp_id and opp_id != test_opp_id:
                        continue

                    last_run = get_comps_run_date(opp, field_map)
                    if last_run and (datetime.utcnow() - last_run).days < cooldown_days:
                        logging.info(f"Skipping {opp_name} — ran {(datetime.utcnow()-last_run).days}d ago")
                        continue

                    contact_id = opp.get("contactId")
                    if contact_id:
                        try:
                            contact = ghl.get_contact(contact_id)
                            opp["_contact"] = contact
                        except Exception as e:
                            logging.warning(f"Could not fetch contact for {opp_name}: {e}")

                    address = ghl.extract_address(opp)
                    if not address:
                        logging.warning(f"No address found for {opp_name} — skipping")
                        ghl.update_opportunity_fields(opp_id, [
                            {"id": field_map["Comp Confidence"], "field_value": "Skipped: no address found"},
                            {"id": field_map["Comps Run Date"], "field_value": datetime.utcnow().strftime("%Y-%m-%d")},
                        ])
                        continue

                    logging.info(f"Running comps for {opp_name} @ {address}")
                    data, status = rentcast.get_avm(address)

                    if status == "quota":
                        logging.warning("RentCast 429 — backing off 30 minutes")
                        time.sleep(1800)
                        break

                    if status == "not_found":
                        logging.warning(f"RentCast 404 for {opp_name} @ {address}")
                        ghl.update_opportunity_fields(opp_id, [
                            {"id": field_map["Comp Confidence"], "field_value": "RentCast: address not found"},
                            {"id": field_map["Comps Run Date"], "field_value": datetime.utcnow().strftime("%Y-%m-%d")},
                        ])
                        continue

                    result, err = rentcast.compute_arv(data)
                    if err:
                        logging.error(f"ARV error for {opp_name}: {err}")
                        continue

                    write_results_to_opp(opp_id, result, field_map)
                    today = datetime.utcnow().strftime("%Y-%m-%d")
                    note_body = ghl.build_arv_note(result, address, today)
                    contact_id = opp.get("contactId")
                    if contact_id:
                        try:
                            if not ghl.arv_note_exists_today(contact_id, today):
                                ghl.add_contact_note(contact_id, note_body)
                                logging.info(f"  Note written to contact {contact_id}")
                            else:
                                logging.info(f"  Note already exists today for {contact_id}, skipping")
                        except Exception as ne:
                            logging.warning(f"  Note write failed: {ne}")
                    logging.info(f"  Conservative: ${result['conservative']:,} | Balanced: ${result['balanced']:,} | Aggressive: ${result['aggressive']:,}")
                    total_processed += 1

                if len(opps) < 100:
                    break
                page += 1

            logging.info(f"Sweep done. Processed {total_processed} deal(s). Sleeping {poll_interval}s.")

        except Exception as e:
            logging.error(f"Sweep error: {e}", exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARV Automation")
    sub = parser.add_subparsers(dest="cmd")

    p_addr = sub.add_parser("check", help="Run one address end-to-end (no GHL writes)")
    p_addr.add_argument("address", help="Full property address")
    p_addr.add_argument("--repairs", type=float, default=None)
    p_addr.add_argument("--fee", type=float, default=None)

    p_loop = sub.add_parser("run", help="Start the automation loop")
    p_loop.add_argument("--test-opp", default=None, help="Only process this one GHL opportunity ID")

    p_setup = sub.add_parser("setup", help="Discover pipeline/stage IDs and create custom fields")

    args = parser.parse_args()

    if args.cmd == "check":
        run_one_address(args.address, args.repairs, args.fee)
    elif args.cmd == "run":
        run_loop(test_opp_id=args.test_opp)
    elif args.cmd == "setup":
        pipeline_id, stage_id, pname, sname = ghl.find_pipeline_and_stage(
            os.environ["TARGET_PIPELINE_NAME"], os.environ["TARGET_STAGE_NAME"]
        )
        if pipeline_id:
            print(f"Pipeline: {pname} ({pipeline_id})")
            print(f"Stage   : {sname} ({stage_id})")
        else:
            print("Pipeline/stage not found.")
        field_map = ghl.ensure_custom_fields()
        print("Custom fields:")
        for k, v in field_map.items():
            print(f"  {k}: {v}")
    else:
        parser.print_help()
