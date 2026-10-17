"""
Vintage / antique / tobacco card price tracker for cromcomicscollectibles.

Runs weekly (see .github/workflows/cards.yml). No watchlist: each run
  1. searches eBay broadly (tobacco, cigarette, trade, vintage non-sport cards)
     and asks eBay which SETS appear most in those results;
  2. pulls up to 400 Buy It Now listings for each of those sets;
  3. saves a snapshot (median asking price per set and per card) to
     cards/history/YYYY-MM-DD.json;
  4. compares with earlier weeks -> cards/report.json:
       - top 20 climbing cards and top climbing sets
       - the 3 best-performing cards of each set, with photos
       - price check of the store's own card listings vs the market.

Prices are ASKING prices (eBay gives no sold prices through its API).
Read-only: it never changes a listing.
"""

import base64
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------- settings you can change ----------------
SELLER = os.environ.get("EBAY_SELLER", "cromcomics1")
DATA_DIR = Path(os.environ.get("CARDS_DIR", "cards"))
BROAD_SEARCHES = [               # how sets are discovered (no personal watchlist)
    "tobacco card",
    "cigarette card",
    "tobacco silk",
    "victorian trade card",
    "antique trade card",
    "pre war non sport card",
    "1930s gum card",
    "1940s non sport card",
    "1950s non sport card",
    "1960s non sport card",
    "1970s non sport card",
    "exhibit arcade card",
]
MAX_YEAR = int(os.environ.get("CARD_MAX_YEAR", "1979"))   # vintage cut-off
CANDIDATES = 150                 # sets checked for era before keeping SETS_PER_RUN
GENERIC_SETS = re.compile(r"^(not specified|unbranded|topps|bowman|fleer|donruss|marvel|dc|"
                          r"star wars|series \d+|mini cards|base set|single|singles|other|"
                          r"trading cards?|non.?sport)$", re.I)
SETS_PER_RUN = int(os.environ.get("CARD_SETS", "40"))   # most-listed sets to track
PAGES_PER_SET = 2                # 200 listings per page
MIN_PRICE = 3.0                  # ignore junk listings under this
MIN_SET_LISTINGS = 8             # sets need this many listings in both weeks
MIN_CARD_LISTINGS = 2            # a card needs this many listings in both weeks
TOP_N = 20
API = "https://api.ebay.com/buy/browse/v1/item_summary/search"


# ---------------- HTTP / eBay ----------------
def http(method, url, headers=None, form=None, tries=4):
    data = urllib.parse.urlencode(form).encode() if form else None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}")
        except OSError as e:
            if attempt == tries - 1:
                raise RuntimeError(f"{method} {url.split('?')[0]} -> network error: {e}")
            time.sleep(5 * (attempt + 1))


def ebay_token():
    creds = f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}"
    r = http("POST", "https://api.ebay.com/identity/v1/oauth2/token",
             form={"grant_type": "client_credentials",
                   "scope": "https://api.ebay.com/oauth/api_scope"},
             headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()})
    return r["access_token"]


def search(token, **params):
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    return http("GET", API + "?" + urllib.parse.urlencode(params), headers=headers)


# ---------------- discovery ----------------
SET_ASPECTS = ("Set", "Manufacturer", "Brand")


def discover_sets(token):
    """Ask eBay which sets are most listed for each broad search."""
    found = {}   # (category, aspect, value) -> count
    for q in BROAD_SEARCHES:
        try:
            r = search(token, q=q, limit="1", fieldgroups="ASPECT_REFINEMENTS,CATEGORY_REFINEMENTS")
        except RuntimeError as e:
            print(f"  (skipped '{q}': {e})")
            continue
        ref = r.get("refinement") or {}
        cat = ref.get("dominantCategoryId")
        aspects = {a.get("localizedAspectName"): a for a in ref.get("aspectDistributions", [])}
        name = next((n for n in SET_ASPECTS if n in aspects), None)
        print(f"  '{q}': {r.get('total', 0)} listings, category {cat}, grouping by {name}")
        if not (cat and name):
            continue
        for v in aspects[name].get("aspectValueDistributions", []):
            val = v.get("localizedAspectValue", "")
            if (not val or val.lower() in ("unknown", "does not apply", "n/a", "mixed", "various")
                    or GENERIC_SETS.match(val.strip())):
                continue
            key = (cat, name, val)
            found[key] = max(found.get(key, 0), int(v.get("matchCount", 0)))
    ranked = sorted(found.items(), key=lambda kv: kv[1], reverse=True)
    sets, seen = [], set()
    for (cat, aspect, val), n in ranked:
        if val.lower() in seen:
            continue
        seen.add(val.lower())
        sets.append({"name": val, "category": cat, "aspect": aspect, "listed": n})
        if len(sets) >= CANDIDATES:
            break
    return sets


YEAR_RE = re.compile(r"\b(18[5-9]\d|19\d\d|20[0-2]\d)\b")


def vintage_share(items):
    """Share of listings whose title year is <= MAX_YEAR (None if too few dated titles)."""
    years = []
    for it in items:
        ys = [int(y) for y in YEAR_RE.findall(it.get("title", ""))]
        if ys:
            years.append(min(ys))
    if len(years) < 5:
        return None, None
    old = sum(1 for y in years if y <= MAX_YEAR)
    return old / len(years), int(statistics.median(years))


# ---------------- listings ----------------
GRADE_RE = re.compile(r"\b(PSA|SGC|BVG|BGS|CGC|GAI|BECKETT)\s*(?:AUTH(?:ENTIC)?|(\d{1,2}(?:\.5)?))", re.I)
NUM_RE = re.compile(r"(?:#|\bno\.?\s*|\bcard\s+)(\d{1,4}[a-z]?)\b", re.I)
LOT_RE = re.compile(r"\b(lot|lots|complete set|partial set|bundle|\d+\s*cards)\b", re.I)


def price_of(it):
    p = it.get("price") or {}
    if p.get("currency") != "USD":
        return None
    try:
        v = float(p.get("value"))
    except (TypeError, ValueError):
        return None
    return v if v >= MIN_PRICE else None


def card_key(set_name, title):
    """'<set> #<number> <grade>' or None for lots / no card number."""
    if LOT_RE.search(title):
        return None
    m = NUM_RE.search(title)
    if not m:
        return None
    g = GRADE_RE.search(title)
    grade = (g.group(1).upper() + " " + (g.group(2) or "A")) if g else "raw"
    grade = grade.replace("BECKETT", "BGS")
    return f"{set_name} #{m.group(1).lower()} {grade}"


def image_of(it):
    url = (it.get("image") or {}).get("imageUrl") or ""
    return url.replace("s-l225", "s-l500")


def fetch_set(token, s):
    items = []
    for page in range(PAGES_PER_SET):
        params = {"category_ids": s["category"], "limit": "200", "offset": str(page * 200),
                  "aspect_filter": f"categoryId:{s['category']},{s['aspect']}:{{{s['name']}}}",
                  "filter": "buyingOptions:{FIXED_PRICE},priceCurrency:USD"}
        try:
            r = search(token, **params)
        except RuntimeError as e:
            print(f"    (stopped '{s['name']}' page {page + 1}: {e})")
            break
        batch = r.get("itemSummaries", [])
        items += batch
        if len(batch) < 200:
            break
    return items


def summarize(sets_items):
    snap = {"date": date.today().isoformat(), "sets": {}, "cards": {}}
    for s, items in sets_items:
        prices, cards = [], {}
        for it in items:
            p = price_of(it)
            if p is None:
                continue
            title = it.get("title", "")
            if not LOT_RE.search(title):
                prices.append(p)
            k = card_key(s["name"], title)
            if k:
                cards.setdefault(k, []).append((p, it))
        if prices:
            snap["sets"][s["name"]] = {
                "median": round(statistics.median(prices), 2), "listings": len(prices),
                "category": s["category"], "year": s.get("year")}
        for k, rows in cards.items():
            ps = [p for p, _ in rows]
            best = min(rows, key=lambda r: r[0])[1]   # cheapest = the comp a buyer sees first
            snap["cards"][k] = {
                "set": s["name"], "median": round(statistics.median(ps), 2), "listings": len(ps),
                "title": best.get("title", ""), "image": image_of(best),
                "url": best.get("itemWebUrl", "")}
    return snap


# ---------------- own listings ----------------
def own_listings(token):
    mine = {}
    for q in BROAD_SEARCHES + ["trading card", "card"]:
        try:
            r = search(token, q=q, limit="200", filter=f"sellers:{{{SELLER}}}")
        except RuntimeError as e:
            print(f"  (own '{q}' skipped: {e})")
            continue
        for it in r.get("itemSummaries", []):
            if (it.get("seller") or {}).get("username", "").lower() == SELLER.lower():
                mine[it["itemId"]] = it
    return list(mine.values())


def price_check(mine, snap, set_change):
    rows = []
    sets_lower = {name.lower(): name for name in snap["sets"]}
    for it in mine:
        p = price_of(it)
        title = it.get("title", "")
        tl = title.lower()
        set_name = next((orig for low, orig in sorted(sets_lower.items(), key=lambda kv: -len(kv[0]))
                         if low in tl), None)
        if p is None or not set_name:
            continue
        k = card_key(set_name, title)
        market = snap["cards"].get(k) if k else None
        if not (market and market["listings"] >= 3):
            market = None
        med = market["median"] if market else None
        ratio = p / med if med else None
        flag = ""
        if ratio and ratio < 0.7:
            flag = "priced well below market (same card and grade)"
        elif ratio and ratio > 1.6:
            flag = "priced well above market (same card and grade)"
        if set_change.get(set_name, 0) >= 10:
            flag = (flag + "; " if flag else "") + "set is climbing - good one to post"
        rows.append({"title": title, "url": it.get("itemWebUrl", ""), "image": image_of(it),
                     "my_price": p, "market_median": med, "matched_card": k if market else None,
                     "vs_market_pct": round((ratio - 1) * 100) if ratio else None, "flag": flag})
    rows = [r for r in rows if r["flag"] or r["market_median"]]
    rows.sort(key=lambda r: (r["flag"] == "", r["vs_market_pct"] or 0))
    return rows


# ---------------- history / climbers ----------------
def load_history():
    hist = {}
    for f in sorted((DATA_DIR / "history").glob("*.json")):
        try:
            hist[f.stem] = json.loads(f.read_text())
        except ValueError:
            pass
    return hist


def baseline(hist, today, days):
    """Snapshot closest to `days` ago (at least 5 days old)."""
    target = today - timedelta(days=days)
    best = None
    for d, snap in hist.items():
        dd = date.fromisoformat(d)
        if (today - dd).days < 5:
            continue
        if best is None or abs((dd - target).days) < abs((date.fromisoformat(best[0]) - target).days):
            best = (d, snap)
    return best


def pct(new, old):
    return round((new - old) / old * 100, 1) if old else None


def climbers(snap, base):
    if not base:
        return [], [], {}
    _, old = base
    cards = []
    for k, c in snap["cards"].items():
        o = old["cards"].get(k)
        if o and c["listings"] >= MIN_CARD_LISTINGS and o["listings"] >= MIN_CARD_LISTINGS:
            ch = pct(c["median"], o["median"])
            if ch and ch > 0:
                cards.append({**c, "card": k, "was": o["median"], "change_pct": ch})
    cards.sort(key=lambda c: c["change_pct"], reverse=True)
    sets, set_change = [], {}
    for name, s in snap["sets"].items():
        o = old["sets"].get(name)
        if o and s["listings"] >= MIN_SET_LISTINGS and o["listings"] >= MIN_SET_LISTINGS:
            ch = pct(s["median"], o["median"])
            set_change[name] = ch or 0
            sets.append({"set": name, "median": s["median"], "was": o["median"],
                         "change_pct": ch, "listings": s["listings"], "was_listings": o["listings"]})
    sets.sort(key=lambda s: s["change_pct"] or 0, reverse=True)
    return cards, sets, set_change


def top3_per_set(snap, card_climbers):
    by_set = {}
    for c in card_climbers:
        by_set.setdefault(c["set"], []).append(c)
    out = {}
    for name in snap["sets"]:
        picks = by_set.get(name, [])[:3]
        if len(picks) < 3:   # not enough history yet: fall back to the highest-priced cards
            have = {p["card"] for p in picks}
            rest = sorted(((k, c) for k, c in snap["cards"].items()
                           if c["set"] == name and k not in have and c["listings"] >= MIN_CARD_LISTINGS),
                          key=lambda kc: kc[1]["median"], reverse=True)
            picks += [{**c, "card": k, "change_pct": None} for k, c in rest[:3 - len(picks)]]
        if picks:
            out[name] = picks
    return out


# ---------------- main ----------------
def main():
    token = ebay_token()
    print("Finding sets on eBay...")
    sets = discover_sets(token)
    print(f"Tracking {len(sets)} sets")
    sets_items = []
    for s in sets:
        if len(sets_items) >= SETS_PER_RUN:
            break
        items = fetch_set(token, s)
        share, year = vintage_share(items)
        if share is None or share < 0.6:
            print(f"  skip {s['name']}: not vintage (typical year {year})")
            continue
        s["year"] = year
        print(f"  {s['name']} (~{year}): {len(items)} listings")
        sets_items.append((s, items))
    snap = summarize(sets_items)

    (DATA_DIR / "history").mkdir(parents=True, exist_ok=True)
    hist = load_history()
    today = date.today()
    base7 = baseline(hist, today, 7)
    base28 = baseline(hist, today, 28)
    (DATA_DIR / "history" / f"{snap['date']}.json").write_text(json.dumps(snap, indent=1))

    card_up, set_up, set_change = climbers(snap, base7)
    card_up28, set_up28, _ = climbers(snap, base28)
    mine = own_listings(token)
    report = {
        "generated": datetime.utcnow().isoformat(timespec="minutes") + "Z",
        "compared_with": base7[0] if base7 else None,
        "compared_with_4wk": base28[0] if base28 else None,
        "weeks_of_history": len(hist) + 1,
        "note": "Asking prices (Buy It Now medians), not sold prices.",
        "sets_tracked": [{"set": k, **v} for k, v in sorted(snap["sets"].items(),
                                                            key=lambda kv: -kv[1]["listings"])],
        "top_card_climbers": card_up[:TOP_N],
        "top_set_climbers": set_up[:TOP_N],
        "top_card_climbers_4wk": card_up28[:TOP_N],
        "top_set_climbers_4wk": set_up28[:TOP_N],
        "top3_per_set": top3_per_set(snap, card_up),
        "my_price_check": price_check(mine, snap, set_change),
    }
    (DATA_DIR / "report.json").write_text(json.dumps(report, indent=1))
    print(f"Saved: {len(snap['sets'])} sets, {len(snap['cards'])} cards, "
          f"{len(card_up)} climbing cards, {len(report['my_price_check'])} of my listings checked.")
    if not base7:
        print("First week of history: climbers appear after next week's run.")


if __name__ == "__main__":
    main()
