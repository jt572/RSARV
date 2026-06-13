import os
import requests
import logging
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VERIFY_SSL = False  # GHL's intermediate cert fails Python 3.14 strict validation

BASE = "https://services.leadconnectorhq.com"
HEADERS = lambda: {
    "Authorization": f"Bearer {os.environ['GHL_LOCATION_PIT']}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
}


def get_pipelines():
    r = requests.get(
        f"{BASE}/opportunities/pipelines",
        headers=HEADERS(),
        params={"locationId": os.environ["GHL_LOCATION_ID"]},
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json().get("pipelines", [])


def find_pipeline_and_stage(pipeline_name: str, stage_name: str):
    pipelines = get_pipelines()
    for p in pipelines:
        if pipeline_name.lower() in p["name"].lower():
            for s in p.get("stages", []):
                if stage_name.lower() in s["name"].lower():
                    return p["id"], s["id"], p["name"], s["name"]
    return None, None, None, None


def get_opportunities(pipeline_id: str, stage_id: str, limit=100, page=1):
    r = requests.get(
        f"{BASE}/opportunities/search",
        headers=HEADERS(),
        params={
            "location_id": os.environ["GHL_LOCATION_ID"],
            "pipeline_id": pipeline_id,
            "pipeline_stage_id": stage_id,
            "limit": limit,
            "page": page,
        },
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("opportunities", []), data.get("meta", {})


def get_contact(contact_id: str):
    r = requests.get(
        f"{BASE}/contacts/{contact_id}",
        headers=HEADERS(),
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json().get("contact", {})


def get_custom_fields():
    r = requests.get(
        f"{BASE}/locations/{os.environ['GHL_LOCATION_ID']}/customFields",
        headers=HEADERS(),
        params={"model": "opportunity"},
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json().get("customFields", [])


def create_custom_field(name: str, data_type: str):
    r = requests.post(
        f"{BASE}/locations/{os.environ['GHL_LOCATION_ID']}/customFields",
        headers=HEADERS(),
        json={"name": name, "dataType": data_type, "model": "opportunity"},
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json().get("customField", {})


def ensure_custom_fields():
    wanted = [
        ("ARV Conservative", "MONETORY"),
        ("ARV Balanced", "MONETORY"),
        ("ARV Aggressive", "TEXT"),
        ("Top Sold Comps", "LARGE_TEXT"),
        ("Comp Confidence", "TEXT"),
        ("Comps Run Date", "DATE"),
    ]
    existing = {f["name"]: f for f in get_custom_fields()}
    field_map = {}
    for name, dtype in wanted:
        if name in existing:
            field_map[name] = existing[name]["id"]
            logging.info(f"Custom field already exists: {name}")
        else:
            f = create_custom_field(name, dtype)
            field_map[name] = f["id"]
            logging.info(f"Created custom field: {name}")
    return field_map


def update_opportunity_fields(opp_id: str, field_updates: list):
    r = requests.put(
        f"{BASE}/opportunities/{opp_id}",
        headers=HEADERS(),
        json={"customFields": field_updates},
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json()


def add_contact_note(contact_id: str, body: str):
    r = requests.post(
        f"{BASE}/contacts/{contact_id}/notes",
        headers=HEADERS(),
        json={"body": body},
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json()


def get_contact_notes(contact_id: str) -> list:
    r = requests.get(
        f"{BASE}/contacts/{contact_id}/notes",
        headers=HEADERS(),
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json().get("notes", [])


def arv_note_exists_today(contact_id: str, run_date: str) -> bool:
    notes = get_contact_notes(contact_id)
    for n in notes:
        if run_date in n.get("dateAdded", "") and "COMPS" in n.get("body", ""):
            return True
    return False


def p(text: str) -> str:
    return f'<p style="padding-left: 0px!important;">{text}</p>'


def build_arv_note(result: dict, address: str, run_date: str) -> str:
    conservative = f"${result['conservative']:,}" if result.get('conservative') else "N/A"
    balanced     = f"${result['balanced']:,}"     if result.get('balanced')     else "N/A"
    aggressive   = f"${result['aggressive']:,}"   if result.get('aggressive')   else "N/A"

    comps_lines = result.get("top_comps") or ["No comps — AVM fallback used"]

    lines = [
        p(f"<strong><u>COMPS — Auto-Generated {run_date}</u></strong>"),
        p(f"<strong>Address:</strong> {address}"),
        p(""),
        p(f"<strong>ARV Conservative:</strong> {conservative}"),
        p(f"<strong>ARV Balanced:</strong> {balanced}"),
        p(f"<strong>ARV Aggressive:</strong> {aggressive}"),
        p(""),
        p(f"<strong>Confidence:</strong> {result.get('confidence', 'N/A')}"),
        p(""),
        p("<strong>--- Top Comps ---</strong>"),
    ]
    for comp in comps_lines:
        lines.append(p(comp))

    lines += [
        p(""),
        p("<strong>--- COMPS by Source ---</strong>"),
        p(f"RentCast ARV: {balanced}"),
        p("ChatARV $: (pending API)"),
        p("PropStream ARV $: (pending API)"),
        p("BatchData ARV $: (pending API)"),
    ]

    return "".join(lines)


def extract_address(opp: dict) -> str | None:
    contact = opp.get("_contact") or {}
    parts = [
        contact.get("address1", ""),
        contact.get("city", ""),
        contact.get("state", ""),
        contact.get("postalCode", ""),
    ]
    address = ", ".join(p for p in parts if p and p.strip())
    if address and len(address) > 10:
        return address

    name = opp.get("name", "")
    import re
    m = re.search(r"\d{2,6}\s+[\w\s]+(?:St|Ave|Rd|Dr|Blvd|Ln|Way|Ct|Pl|Ter|Circle|Trail)[^,]*", name, re.I)
    if m:
        return m.group(0).strip()

    return None
