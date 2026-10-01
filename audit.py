"""
Listing audit for the cromcomics1 eBay store.

For every active listing it checks what eBay search rewards:
  - title length and clutter (L@@K, NR, ALL CAPS)
  - number of photos
  - item specifics vs. what eBay marks required / recommended for that category
  - Best Offer, returns, free shipping
  - listing age (stale listings rank lower)

Writes audit/audit.csv (one row per listing, worst first) and audit/summary.json.
Uses the same secrets as post.py. Set AUDIT_LIMIT=50 for a quick test run.
"""

import csv
import json
import math
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import post  # reuse eBay login, HTTP helper and catalog

OUT = Path("audit")
LIMIT = int(os.environ.get("AUDIT_LIMIT", "0") or 0)
JUNK_RE = re.compile(rf"(?i)\b{post.JUNK}(?=\W|$)")
_aspect_cache = {}


def get_json(url, token, tries=4):
    for attempt in range(tries):
        try:
            return post.http("GET", url, headers=post.ebay_headers(token))
        except RuntimeError as e:
            if "HTTP 429" in str(e) or "HTTP 5" in str(e):
                time.sleep(10 * (attempt + 1))  # rate limited / eBay hiccup: back off
                continue
            raise
    raise RuntimeError(f"gave up after {tries} tries: {url.split('?')[0]}")


def category_aspects(cat_id, token):
    """Aspects eBay marks as required / recommended for a leaf category."""
    if cat_id in _aspect_cache:
        return _aspect_cache[cat_id]
    url = ("https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/"
           f"get_item_aspects_for_category?category_id={cat_id}")
    required, recommended = [], []
    try:
        r = get_json(url, token)
        for a in r.get("aspects", []):
            name = a.get("localizedAspectName")
            c = a.get("aspectConstraint", {})
            if c.get("aspectRequired"):
                required.append(name)
            elif c.get("aspectUsage") == "RECOMMENDED":
                recommended.append(name)
    except RuntimeError as e:
        print(f"  (no aspect info for category {cat_id}: {e})")
    _aspect_cache[cat_id] = (required, recommended)
    return required, recommended


def audit_item(item_id, token, now):
    url = "https://api.ebay.com/buy/browse/v1/item/" + urllib.parse.quote(item_id, safe="")
    it = get_json(url, token)

    title = it.get("title", "")
    photos = (1 if it.get("image") else 0) + len(it.get("additionalImages") or [])
    filled = {a.get("name") for a in (it.get("localizedAspects") or []) if a.get("value")}
    required, recommended = category_aspects(it.get("categoryId"), token)
    miss_req = [a for a in required if a not in filled]
    miss_rec = [a for a in recommended if a not in filled]
    price = float((it.get("price") or {}).get("value") or 0)
    buying = it.get("buyingOptions") or []
    best_offer = "BEST_OFFER" in buying
    ship_costs = [float((s.get("shippingCost") or {}).get("value", 1) or 0)
                  for s in it.get("shippingOptions") or []]
    free_ship = bool(ship_costs) and min(ship_costs) == 0
    returns = bool((it.get("returnTerms") or {}).get("returnsAccepted"))
    created = it.get("itemCreationDate")
    age = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).days if created else None
    used = "new" not in (it.get("condition") or "").lower()
    cond_note = bool(it.get("conditionDescription"))

    issues, score = [], 0.0
    if len(title) < 50:
        issues.append(f"Short title ({len(title)}/80 chars)"); score += 3
    elif len(title) < 65:
        issues.append(f"Title could be longer ({len(title)}/80)"); score += 1
    if JUNK_RE.search(title):
        issues.append("Remove filler words (L@@K, NR, HTF...)"); score += 1
    if title.isupper():
        issues.append("ALL CAPS title"); score += 1
    if photos < 3:
        issues.append(f"Only {photos} photo(s), add more (up to 24)"); score += 3
    elif photos < 6:
        issues.append(f"{photos} photos, 6+ recommended"); score += 1
    if miss_req:
        issues.append("Missing REQUIRED specifics: " + ", ".join(miss_req[:5])); score += min(6, 3 * len(miss_req))
    if miss_rec:
        issues.append("Missing recommended specifics: " + ", ".join(miss_rec[:6])); score += min(4, 0.5 * len(miss_rec))
    if "FIXED_PRICE" in buying and not best_offer and price >= 20:
        issues.append("Turn on Best Offer"); score += 1
    if not returns:
        issues.append("No returns accepted"); score += 1
    if ship_costs and not free_ship:
        issues.append("Not free shipping (consider building into price)"); score += 0.5
    if used and not cond_note:
        issues.append("Add a condition description"); score += 0.5
    if age is not None and age > 180:
        issues.append(f"Stale ({age} days): End & Sell Similar"); score += 2
    elif age is not None and age > 90:
        issues.append(f"Aging ({age} days)"); score += 1

    # fix valuable items first: weight by price
    priority = round(score * (1 + math.log10(price + 1)), 1)
    return {
        "priority": priority,
        "issue_score": score,
        "price": price,
        "title": title,
        "link": f"https://www.ebay.com/itm/{it.get('legacyItemId', '')}",
        "category": it.get("categoryPath", ""),
        "title_len": len(title),
        "photos": photos,
        "specifics_filled": len(filled),
        "missing_required": "; ".join(miss_req),
        "missing_recommended": "; ".join(miss_rec),
        "best_offer": "yes" if best_offer else "no",
        "free_shipping": "yes" if free_ship else "no",
        "returns": "yes" if returns else "no",
        "age_days": age if age is not None else "",
        "issues": " | ".join(issues),
    }


def main():
    token = post.ebay_token()
    if post.CATALOG.exists():
        catalog = json.loads(post.CATALOG.read_text())
    else:
        print("No catalog yet, scanning the store (15+ min)...")
        catalog = post.fetch_listings(token, full=True)
    ids = [i["itemId"] for i in catalog]
    if LIMIT:
        ids = ids[:LIMIT]
    print(f"Auditing {len(ids)} listings...")

    now = datetime.now(timezone.utc)
    rows, failed, started = [], 0, time.time()
    for n, item_id in enumerate(ids, 1):
        if time.time() - started > 50 * 60:  # stay inside the job's time limit
            print("Time limit reached, saving what's done so far.")
            break
        try:
            rows.append(audit_item(item_id, token, now))
        except RuntimeError as e:
            failed += 1  # usually a listing that ended since the catalog was saved
            if failed <= 5:
                print(f"  (skipped {item_id}: {e})")
        if n % 250 == 0:
            print(f"  {n}/{len(ids)} done ({int(time.time() - started)}s)")
            token = post.ebay_token()  # tokens last 2 hours; refresh to be safe

    rows.sort(key=lambda r: r["priority"], reverse=True)
    OUT.mkdir(exist_ok=True)
    with open(OUT / "audit.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["priority"])
        w.writeheader()
        w.writerows(rows)

    def count(pred):
        return sum(1 for r in rows if pred(r))
    summary = {
        "audited": len(rows), "skipped": failed, "run_at": now.isoformat(),
        "short_titles_under_50": count(lambda r: r["title_len"] < 50),
        "titles_50_to_64": count(lambda r: 50 <= r["title_len"] < 65),
        "under_3_photos": count(lambda r: r["photos"] < 3),
        "under_6_photos": count(lambda r: r["photos"] < 6),
        "missing_required_specifics": count(lambda r: r["missing_required"]),
        "missing_recommended_specifics": count(lambda r: r["missing_recommended"]),
        "no_best_offer_over_20": count(lambda r: "Best Offer" in r["issues"]),
        "no_returns": count(lambda r: r["returns"] == "no"),
        "not_free_shipping": count(lambda r: r["free_shipping"] == "no"),
        "stale_over_180_days": count(lambda r: "Stale" in r["issues"]),
        "aging_90_to_180_days": count(lambda r: "Aging" in r["issues"]),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
