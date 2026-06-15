import os
import requests
import logging
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.chatarv.ai/api/public"


def get_comps(address: str) -> tuple:
    """Returns (result_dict, status) where status is 'ok', 'not_found', 'quota', or 'error'."""
    try:
        r = requests.post(
            f"{BASE}/comps",
            headers={
                "X-API-KEY": os.environ["CHATARV_API_KEY"],
                "Content-Type": "application/json",
            },
            json={"address": address},
            timeout=90,
            verify=False,
        )
    except requests.exceptions.Timeout:
        logging.warning(f"ChatARV timeout for {address}")
        return None, "error"
    except Exception as e:
        logging.warning(f"ChatARV request error: {e}")
        return None, "error"

    if r.status_code == 401:
        logging.error("ChatARV: invalid API key")
        return None, "error"
    if r.status_code == 429:
        return None, "quota"
    if r.status_code == 404 or r.status_code == 400:
        return None, "not_found"
    if r.status_code != 200:
        logging.warning(f"ChatARV unexpected status {r.status_code}")
        return None, "error"

    return r.json(), "ok"


def extract_arv(data: dict) -> dict | None:
    """Extract ARV, confidence, and top comps from ChatARV response."""
    est_value = data.get("est_value")
    confidence = data.get("confidence")
    feedback = data.get("feedbackSummary", "")

    if not est_value:
        return None

    comps = data.get("comps", [])
    sold_comps = [
        c for c in comps
        if (c.get("price") or {}).get("mlsStatus", "").lower() == "sold"
        and c.get("squareFeet")
        and c.get("price", {}).get("amount")
    ]
    sold_comps.sort(key=lambda c: c.get("distance", 99))
    top3 = sold_comps[:3]

    comp_lines = []
    for c in top3:
        addr = (c.get("address") or {}).get("address", "unknown")
        price = c.get("price", {}).get("amount", 0)
        sqft = c.get("squareFeet", 0)
        dist = c.get("distance", 0)
        comp_lines.append(f"${price:,.0f} | {int(sqft)} sqft | {dist:.1f}mi | sold | {addr}")

    return {
        "arv": est_value,
        "confidence": confidence,
        "feedback": feedback,
        "top_comps": comp_lines,
    }
