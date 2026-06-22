import argparse
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify
import ghl
import rentcast
import chatarv

WEIGHT_CHATARV   = 0.50
WEIGHT_RENTCAST  = 0.10
# BatchData weight (0.40) activates when BatchData API is integrated

app = Flask(__name__)
_field_map_cache = {}


def get_field_map():
    if not _field_map_cache:
        _field_map_cache.update(ghl.ensure_custom_fields())
    return _field_map_cache


def get_env_list(name: str, fallback: str | None = None) -> list[str]:
    """Return a comma-separated environment variable as a clean list."""
    raw = os.environ.get(name, fallback or "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def unique_list(items: list[str]) -> list[str]:
    """Preserve order while removing duplicates."""
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def get_target_stage_names() -> list[str]:
    """
    Support JT's requested trigger stages without removing the legacy Run Comps path.

    Preferred .env format:
      TARGET_STAGE_NAMES=New Lead (Untouched),Run Comps

    Backward compatible .env format:
      TARGET_STAGE_NAME=Run Comps

    If only TARGET_STAGE_NAME exists, New Lead (Untouched) is added automatically.
    """
    names = get_env_list("TARGET_STAGE_NAMES")
    if names:
        return unique_list(names)

    legacy = os.environ.get("TARGET_STAGE_NAME", "Run Comps")
    return unique_list(["New Lead (Untouched)", legacy])


def get_target_stage_ids() -> list[str]:
    """
    Support multiple webhook stage IDs.

    Preferred .env format:
      TARGET_STAGE_IDS=new_lead_stage_id,run_comps_stage_id

    Backward compatible .env format:
      TARGET_STAGE_ID=run_comps_stage_id
    """
    ids = get_env_list("TARGET_STAGE_IDS")
    if ids:
        return unique_list(ids)

    legacy = os.environ.get("TARGET_STAGE_ID")
    if legacy:
        return [legacy]

    # Legacy hardcoded Run Comps stage ID. Keep this as a fallback so existing deployments do not break.
    return ["74cdaa01-d170-4b5e-a9dd-3714c24bdf5a"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("arv.log")],
)


def blend_arv(chatarv_result: dict | None, rc_result: dict | None) -> dict:
    """Blend ChatARV and RentCast into a single ARV result."""
    sources = {}
    if chatarv_result and chatarv_result.get("arv"):
        sources["ChatARV"] = {"arv": chatarv_result["arv"], "weight": WEIGHT_CHATARV, "confidence": chatarv_result.get("confidence")}
    if rc_result and rc_result.get("balanced"):
        sources["RentCast"] = {"arv": rc_result["balanced"], "weight": WEIGHT_RENTCAST, "confidence": None}

    if not sources:
        return {"blended": None, "conservative": None, "balanced": None, "aggressive": None, "confidence_note": "No data", "sources": {}, "top_comps": []}

    total_weight = sum(s["weight"] for s in sources.values())
    blended = sum(s["arv"] * s["weight"] for s in sources.values()) / total_weight
    blended = round(blended / 1000) * 1000

    # Use RentCast tiers for conservative/aggressive spread if available
    if rc_result and rc_result.get("conservative"):
        spread = (rc_result["aggressive"] - rc_result["conservative"]) / 2
        conservative = round((blended - spread) / 1000) * 1000
        aggressive = round((blended + spread) / 1000) * 1000
    else:
        conservative = round(blended * 0.93 / 1000) * 1000
        aggressive = round(blended * 1.07 / 1000) * 1000

    confidence_parts = []
    if "ChatARV" in sources:
        confidence_parts.append(f"ChatARV {int(sources['ChatARV']['confidence'] or 0)}%")
    source_names = " + ".join(sources.keys())
    confidence_note = f"Blended ({source_names})" + (f" | {', '.join(confidence_parts)}" if confidence_parts else "")

    top_comps = []
    if chatarv_result and chatarv_result.get("top_comps"):
        top_comps = chatarv_result["top_comps"]
    elif rc_result and rc_result.get("top_comps"):
        top_comps = rc_result["top_comps"]

    return {
        "blended": blended,
        "conservative": conservative,
        "balanced": blended,
        "aggressive": aggressive,
        "confidence_note": confidence_note,
        "sources": sources,
        "top_comps": top_comps,
        "chatarv_arv": sources.get("ChatARV", {}).get("arv"),
        "chatarv_confidence": sources.get("ChatARV", {}).get("confidence"),
        "rentcast_arv": sources.get("RentCast", {}).get("arv"),
    }


def run_one_address(address: str, repairs: float = None, fee: float = None):
    print(f"\nRunning comps for: {address}")

    rc_data, rc_status = rentcast.get_avm(address)
    rc_result = None
    if rc_status == "ok":
        rc_result, _ = rentcast.compute_arv(rc_data)
    elif rc_status == "quota":
        print("RentCast quota exhausted.")
    elif rc_status == "not_found":
        print("RentCast: address not found.")

    print("  Calling ChatARV (up to 60s)...")
    ca_data, ca_status = chatarv.get_comps(address)
    ca_result = None
    if ca_status == "ok":
        ca_result = chatarv.extract_arv(ca_data)
    elif ca_status == "quota":
        print("ChatARV quota exhausted.")
    elif ca_status == "not_found":
        print("ChatARV: address not found.")

    blended = blend_arv(ca_result, rc_result)

    print(f"\nSubject sqft : {(rc_result or {}).get('subject_sqft', 'N/A')}")
    print(f"Conservative : ${blended['conservative']:,}" if blended['conservative'] else "Conservative : N/A")
    print(f"Balanced     : ${blended['balanced']:,}" if blended['balanced'] else "Balanced     : N/A")
    print(f"Aggressive   : ${blended['aggressive']:,}" if blended['aggressive'] else "Aggressive   : N/A")
    print(f"Confidence   : {blended['confidence_note']}")
    if ca_result:
        print(f"\nChatARV ARV  : ${ca_result['arv']:,} (confidence {ca_result['confidence']}%)")
        print(f"ChatARV note : {ca_result['feedback'][:120]}")
    if rc_result:
        print(f"RentCast ARV : ${rc_result['balanced']:,}")
    if blended["top_comps"]:
        print("\nTop Comps:")
        for line in blended["top_comps"]:
            print(f"  {line}")

    if repairs is not None and fee is not None:
        bal = blended["balanced"] or 0
        mao = bal * 0.70 - repairs - fee
        print(f"\nMAO (blended ARV x 0.70 - repairs - fee): ${mao:,.0f}")


def write_results_to_opp(opp_id: str, blended: dict, field_map: dict):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    updates = [
        {"id": field_map["ARV Conservative"], "field_value": blended["conservative"]},
        {"id": field_map["ARV Balanced"],     "field_value": blended["balanced"]},
        {"id": field_map["ARV Aggressive"],   "field_value": f"${blended['aggressive']:,}"},
        {"id": field_map["Top Sold Comps"],   "field_value": "\n".join(blended["top_comps"])},
        {"id": field_map["Comp Confidence"],  "field_value": blended["confidence_note"]},
        {"id": field_map["Comps Run Date"],   "field_value": today},
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

    logging.info("Discovering pipeline and target stages...")

    target_stages = []
    for target_stage_name in get_target_stage_names():
        pipeline_id, stage_id, pipeline_name, stage_name = ghl.find_pipeline_and_stage(
            os.environ["TARGET_PIPELINE_NAME"], target_stage_name
        )
        if pipeline_id and stage_id:
            target_stages.append({
                "pipeline_id": pipeline_id,
                "stage_id": stage_id,
                "pipeline_name": pipeline_name,
                "stage_name": stage_name,
            })
            logging.info(f"Target stage enabled: {pipeline_name} | {stage_name} ({stage_id})")
        else:
            logging.warning(f"Target stage not found: {target_stage_name}")

    if not target_stages:
        logging.error("No target stages found. Check TARGET_PIPELINE_NAME and TARGET_STAGE_NAMES in .env")
        return

    logging.info("Ensuring custom fields exist...")
    field_map = ghl.ensure_custom_fields()

    if test_opp_id:
        logging.info(f"TEST MODE: will only process opportunity {test_opp_id}")

    while True:
        try:
            logging.info("Starting sweep...")
            total_processed = 0
            stop_current_sweep = False

            for target in target_stages:
                if stop_current_sweep:
                    break

                pipeline_id = target["pipeline_id"]
                stage_id = target["stage_id"]
                stage_name = target["stage_name"]
                page = 1

                logging.info(f"Scanning stage: {stage_name}")

                while True:
                    opps, meta = ghl.get_opportunities(pipeline_id, stage_id, limit=100, page=page)
                    if not opps:
                        break

                    if page == 1:
                        total = meta.get("total", len(opps))
                        logging.info(f"Deals in {stage_name}: {total}")

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

                        logging.info(f"Running comps for {opp_name} @ {address} | Trigger stage: {stage_name}")

                        rc_data, rc_status = rentcast.get_avm(address)
                        rc_result = None
                        if rc_status == "quota":
                            logging.warning("RentCast 429 — backing off 30 minutes")
                            time.sleep(1800)
                            stop_current_sweep = True
                            break
                        elif rc_status == "ok":
                            rc_result, _ = rentcast.compute_arv(rc_data)
                        else:
                            logging.warning(f"RentCast {rc_status} for {opp_name}")

                        logging.info(f"  Calling ChatARV...")
                        ca_data, ca_status = chatarv.get_comps(address)
                        ca_result = None
                        if ca_status == "quota":
                            logging.warning("ChatARV quota exhausted")
                        elif ca_status == "ok":
                            ca_result = chatarv.extract_arv(ca_data)
                        else:
                            logging.warning(f"ChatARV {ca_status} for {opp_name}")

                        blended = blend_arv(ca_result, rc_result)
                        if not blended["balanced"]:
                            logging.error(f"No ARV data from any source for {opp_name}")
                            continue

                        write_results_to_opp(opp_id, blended, field_map)
                        today = datetime.utcnow().strftime("%Y-%m-%d")
                        note_body = ghl.build_arv_note(blended, address, today)
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
                        logging.info(f"  Conservative: ${blended['conservative']:,} | Balanced: ${blended['balanced']:,} | Aggressive: ${blended['aggressive']:,} | Sources: {list(blended['sources'].keys())}")
                        total_processed += 1

                    if stop_current_sweep or len(opps) < 100:
                        break
                    page += 1

            logging.info(f"Sweep done. Processed {total_processed} deal(s). Sleeping {poll_interval}s.")

        except Exception as e:
            logging.error(f"Sweep error: {e}", exc_info=True)

        time.sleep(poll_interval)


def process_opportunity_now(opp_id: str):
    """Run comps on a single opportunity immediately — called by webhook."""
    try:
        field_map = get_field_map()
        opp_data = ghl.get_opportunity(opp_id)
        if not opp_data:
            logging.warning(f"Webhook: opportunity {opp_id} not found")
            return

        contact_id = opp_data.get("contactId")
        if contact_id:
            try:
                contact = ghl.get_contact(contact_id)
                opp_data["_contact"] = contact
            except Exception:
                pass

        address = ghl.extract_address(opp_data)
        opp_name = opp_data.get("name", opp_id)

        if not address:
            logging.warning(f"Webhook: no address for {opp_name}")
            ghl.update_opportunity_fields(opp_id, [
                {"id": field_map["Comp Confidence"], "field_value": "Skipped: no address found"},
                {"id": field_map["Comps Run Date"], "field_value": datetime.utcnow().strftime("%Y-%m-%d")},
            ])
            return

        logging.info(f"Webhook: running comps for {opp_name} @ {address}")
        data, status = rentcast.get_avm(address)

        if status == "quota":
            logging.warning("Webhook: RentCast 429 — quota exhausted")
            return
        if status == "not_found":
            logging.warning(f"Webhook: RentCast 404 for {address}")
            ghl.update_opportunity_fields(opp_id, [
                {"id": field_map["Comp Confidence"], "field_value": "RentCast: address not found"},
                {"id": field_map["Comps Run Date"], "field_value": datetime.utcnow().strftime("%Y-%m-%d")},
            ])
            return

        result, err = rentcast.compute_arv(data)
        if err:
            logging.error(f"Webhook: ARV error for {opp_name}: {err}")
            return

        write_results_to_opp(opp_id, result, field_map)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        note_body = ghl.build_arv_note(result, address, today)
        if contact_id and not ghl.arv_note_exists_today(contact_id, today):
            ghl.add_contact_note(contact_id, note_body)
        logging.info(f"Webhook: done — Conservative: ${result['conservative']:,} | Balanced: ${result['balanced']:,}")

    except Exception as e:
        logging.error(f"Webhook processing error for {opp_id}: {e}", exc_info=True)


@app.route("/webhook/ghl", methods=["POST"])
def ghl_webhook():
    payload = request.get_json(silent=True) or {}
    logging.info(f"Webhook received: {str(payload)[:200]}")

    opp_id = (
        payload.get("id")
        or payload.get("opportunityId")
        or (payload.get("opportunity") or {}).get("id")
    )
    stage_id = (
        payload.get("pipelineStageId")
        or (payload.get("opportunity") or {}).get("pipelineStageId")
    )

    target_stage_ids = get_target_stage_ids()

    if not opp_id:
        return jsonify({"status": "ignored", "reason": "no opportunity id"}), 200

    if stage_id and target_stage_ids and stage_id not in target_stage_ids:
        return jsonify({"status": "ignored", "reason": "wrong stage"}), 200

    threading.Thread(target=process_opportunity_now, args=(opp_id,), daemon=True).start()
    return jsonify({"status": "processing", "opportunityId": opp_id}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def run_server_and_loop():
    """Start daily poll loop in background, Flask webhook server in foreground."""
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    port = int(os.environ.get("PORT", 8080))
    logging.info(f"Webhook server starting on port {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARV Automation")
    sub = parser.add_subparsers(dest="cmd")

    p_addr = sub.add_parser("check", help="Run one address end-to-end (no GHL writes)")
    p_addr.add_argument("address", help="Full property address")
    p_addr.add_argument("--repairs", type=float, default=None)
    p_addr.add_argument("--fee", type=float, default=None)

    p_loop = sub.add_parser("run", help="Start the automation loop (no webhook server)")
    p_loop.add_argument("--test-opp", default=None, help="Only process this one GHL opportunity ID")

    sub.add_parser("serve", help="Start webhook server + daily poll loop (production mode)")

    p_setup = sub.add_parser("setup", help="Discover pipeline/stage IDs and create custom fields")

    args = parser.parse_args()

    if args.cmd == "check":
        run_one_address(args.address, args.repairs, args.fee)
    elif args.cmd == "run":
        run_loop(test_opp_id=args.test_opp)
    elif args.cmd == "serve":
        run_server_and_loop()
    elif args.cmd == "setup":
        print("Target stages:")
        found_any_stage = False
        for target_stage_name in get_target_stage_names():
            pipeline_id, stage_id, pname, sname = ghl.find_pipeline_and_stage(
                os.environ["TARGET_PIPELINE_NAME"], target_stage_name
            )
            if pipeline_id:
                found_any_stage = True
                print(f"  Pipeline: {pname} ({pipeline_id})")
                print(f"  Stage   : {sname} ({stage_id})")
            else:
                print(f"  Stage not found: {target_stage_name}")
        if not found_any_stage:
            print("No target stages found.")
        field_map = ghl.ensure_custom_fields()
        print("Custom fields:")
        for k, v in field_map.items():
            print(f"  {k}: {v}")
    else:
        parser.print_help()

