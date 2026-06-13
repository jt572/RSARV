import os
import requests
import logging
import urllib3
from datetime import datetime, timedelta
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAX_DISTANCE_MILES = 1.5
MAX_AGE_DAYS = 365
SQFT_TOLERANCE = 400
COMP_COUNT = 20


def get_avm(address: str):
    r = requests.get(
        "https://api.rentcast.io/v1/avm/value",
        headers={"X-Api-Key": os.environ["RENTCAST_API_KEY"]},
        params={"address": address, "compCount": COMP_COUNT},
        verify=False,
    )
    if r.status_code == 404:
        return None, "not_found"
    if r.status_code == 429:
        return None, "quota"
    r.raise_for_status()
    return r.json(), "ok"


def is_sold(comp: dict) -> bool:
    return comp.get("status", "").lower() == "inactive" or bool(comp.get("removedDate"))


def filter_comps(comps: list, subject_sqft: float) -> list:
    cutoff = datetime.utcnow() - timedelta(days=MAX_AGE_DAYS)
    results = []
    for c in comps:
        price = c.get("price")
        sqft = c.get("squareFootage")
        if not price or not sqft:
            continue
        if c.get("distance", 999) > MAX_DISTANCE_MILES:
            continue
        if abs(sqft - subject_sqft) > SQFT_TOLERANCE:
            continue
        last_seen_str = c.get("removedDate") or c.get("listedDate") or c.get("lastSeenDate")
        if last_seen_str:
            try:
                last_seen = datetime.fromisoformat(last_seen_str[:10])
                if last_seen < cutoff:
                    continue
            except ValueError:
                pass
        results.append(c)
    return results


def compute_arv(data: dict):
    subject = data.get("subjectProperty", {})
    subject_sqft = subject.get("squareFootage")
    avm_price = data.get("price")
    avm_low = data.get("priceRangeLow")
    avm_high = data.get("priceRangeHigh")

    if not subject_sqft:
        confidence = "No sqft on record — using RentCast AVM fallback"
        conservative = round((avm_low or avm_price) / 1000) * 1000 if avm_low or avm_price else None
        balanced = round(avm_price / 1000) * 1000 if avm_price else None
        aggressive = round((avm_high or avm_price) / 1000) * 1000 if avm_high or avm_price else None
        return {
            "conservative": conservative,
            "balanced": balanced,
            "aggressive": aggressive,
            "confidence": confidence,
            "top_comps": [],
            "subject_sqft": None,
        }, None

    all_comps = data.get("comparables", [])
    filtered = filter_comps(all_comps, subject_sqft)

    sold = sorted([c for c in filtered if is_sold(c)], key=lambda c: c.get("correlation", 0), reverse=True)
    active = sorted([c for c in filtered if not is_sold(c)], key=lambda c: c.get("correlation", 0), reverse=True)
    ranked = (sold + active)[:3]

    if len(ranked) < 3:
        confidence = f"Only {len(ranked)} comp(s) found after filtering — using RentCast AVM fallback"
        conservative = round((avm_low or avm_price) / 1000) * 1000 if avm_low or avm_price else None
        balanced = round(avm_price / 1000) * 1000 if avm_price else None
        aggressive = round((avm_high or avm_price) / 1000) * 1000 if avm_high or avm_price else None
        return {
            "conservative": conservative,
            "balanced": balanced,
            "aggressive": aggressive,
            "confidence": confidence,
            "top_comps": [],
            "subject_sqft": subject_sqft,
        }, None

    ppsf = [c["price"] / c["squareFootage"] for c in ranked]
    max_comp_price = max(c["price"] for c in ranked)

    conservative = min(round(min(ppsf) * subject_sqft / 1000) * 1000, max_comp_price)
    balanced = min(round((sum(ppsf) / len(ppsf)) * subject_sqft / 1000) * 1000, max_comp_price)
    aggressive = min(round(max(ppsf) * subject_sqft / 1000) * 1000, max_comp_price)

    comp_lines = []
    for c in ranked:
        status = "sold" if is_sold(c) else "active"
        addr = c.get("formattedAddress") or c.get("address") or c.get("addressLine1", "unknown address")
        comp_lines.append(
            f"${c['price']:,.0f} | {int(c['squareFootage'])} sqft | {c.get('distance', 0):.1f}mi | {status} | {addr}"
        )

    return {
        "conservative": conservative,
        "balanced": balanced,
        "aggressive": aggressive,
        "confidence": f"3 comps, basis: top sold comps",
        "top_comps": comp_lines,
        "subject_sqft": subject_sqft,
    }, None
