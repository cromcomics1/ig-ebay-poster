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
NON_SPORT = "182982"             # eBay category: Non-Sport Trading Cards
MAKER_SETS = int(os.environ.get("CARD_MAKER_SETS", "40"))  # tobacco/pre-war sets split from titles
MAKERS_CHECKED = 40
MAKER_PAGES = 4
SETS_PER_MAKER = 6
MIN_CLUSTER = 12                 # listings needed to count as a set
MIN_PRICE = 3.0                  # ignore junk listings under this
MIN_SET_LISTINGS = 8             # sets need this many listings in both weeks
MIN_CARD_LISTINGS = 2            # a card needs this many listings in both weeks
TOP_N = 20
CLIMB_PCT = float(os.environ.get("CLIMB_PCT", "30"))   # a "climber" rose at least this much
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


BAD_VALUES = ("unknown", "does not apply", "n/a", "mixed", "various", "not specified", "none")


def usable(val):
    val = (val or "").strip()
    return bool(val) and val.lower() not in BAD_VALUES and not GENERIC_SETS.match(val)


def aspect_counts(refinement, name):
    for a in (refinement or {}).get("aspectDistributions", []):
        if a.get("localizedAspectName") == name:
            return [(v.get("localizedAspectValue", ""), int(v.get("matchCount", 0)))
                    for v in a.get("aspectValueDistributions", [])]
    return []


def aspect_filter(cat, filters):
    return f"categoryId:{cat}," + ",".join(f"{k}:{{{v}}}" for k, v in filters.items())


def refine(token, cat, filters):
    try:
        r = search(token, category_ids=cat, limit="1", fieldgroups="ASPECT_REFINEMENTS",
                   aspect_filter=aspect_filter(cat, filters))
        return r.get("refinement") or {}
    except RuntimeError as e:
        print(f"    (refine {filters} skipped: {e})")
        return {}


DISCOVERY_LOG = {"searches": []}


def discover_sets(token):
    """Returns (sets, makers).
    sets   - groups eBay lists by exact set name (mostly 1950s-70s gum cards)
    makers - manufacturers whose cards have no set name on eBay (most tobacco and
             pre-war cards); these are split into sets from the listing titles."""
    sets_found, makers = {}, {}
    for q in BROAD_SEARCHES:
        try:
            r = search(token, q=q, category_ids=NON_SPORT, limit="1",
                       fieldgroups="ASPECT_REFINEMENTS,CATEGORY_REFINEMENTS")
        except RuntimeError as e:
            print(f"  (skipped '{q}': {e})")
            continue
        ref = r.get("refinement") or {}
        cat = ref.get("dominantCategoryId") or NON_SPORT
        print(f"  '{q}': {r.get('total', 0)} listings, category {cat}")
        DISCOVERY_LOG["searches"].append({
            "q": q, "total": r.get("total"), "category": cat,
            "aspects": {a.get("localizedAspectName"): [(v.get("localizedAspectValue"), v.get("matchCount"))
                        for v in a.get("aspectValueDistributions", [])][:15]
                        for a in ref.get("aspectDistributions", [])}})
        for val, n in aspect_counts(ref, "Set"):
            if usable(val):
                k = (cat, val)
                sets_found[k] = max(sets_found.get(k, 0), n)
        for val, n in aspect_counts(ref, "Manufacturer"):
            if usable(val):
                makers[val] = makers.get(val, 0) + n
    sets, seen = [], set()
    for (cat, val), n in sorted(sets_found.items(), key=lambda kv: -kv[1]):
        if val.lower() in seen:
            continue
        seen.add(val.lower())
        sets.append({"category": cat, "filters": {"Set": val}, "maker": "", "set": val,
                     "name": val, "listed": n})
        if len(sets) >= CANDIDATES:
            break
    maker_list = [m for m, n in sorted(makers.items(), key=lambda kv: -kv[1]) if n >= 15][:MAKERS_CHECKED]
    return sets, maker_list


# ---------------- sets from listing titles (makers without set names) ----------------
TITLE_STOP = set("""
card cards cigarette cigarettes tobacco trade tea gum silk silks single singles pick choose choice
select your you list free shipping post postage complete full set sets series 1st 2nd 3rd issued issue
circa approx nice great collectible collectable antique vintage old original genuine authentic rare
scarce large small size lot lots uk english british england ltd co company inc corp and the of a an
in on for with by to from or no nos number psa sgc bvg bgs cgc graded raw ex vg nm mt mint near
excellent good fine poor fair condition back front reverse very type style issues sold each
""".split())
GRADE_TOKENS = re.compile(r"^(?:\d+(?:\.\d)?|[a-z]?\d+[a-z]?)$")


def title_tokens(title, maker_words):
    t = title.lower()
    t = re.sub(r"(#|\bno\.?\s*)\s*\d+[a-z]?", " ", t)
    words = re.findall(r"[a-z0-9'&]+", t)
    out = []
    for w in words:
        w = w.strip("'")
        if (not w or w in TITLE_STOP or w in maker_words or GRADE_TOKENS.match(w)
                or len(w) < 2):
            continue
        out.append(w)
    return out


def cluster_titles(items, maker):
    """Group a maker's listings into sets by the phrase most titles share
    (e.g. 'Film Stars', 'Famous Footballers'). Returns [(phrase, items)]."""
    maker_words = set(re.findall(r"[a-z0-9]+", maker.lower()))
    grams = []
    for it in items:
        tk = title_tokens(it.get("title", ""), maker_words)
        g = set()
        for n in (2, 3, 4):
            for i in range(len(tk) - n + 1):
                g.add(" ".join(tk[i:i + n]))
        grams.append(g)
    remaining = set(range(len(items)))
    clusters = []
    while len(clusters) < SETS_PER_MAKER:
        df = {}
        for i in remaining:
            for g in grams[i]:
                df[g] = df.get(g, 0) + 1
        if not df:
            break
        top = max(df.values())
        if top < MIN_CLUSTER:
            break
        phrase = max((g for g, c in df.items() if c >= 0.8 * top),
                     key=lambda g: (len(g.split()), df[g]))
        members = [i for i in remaining if phrase in grams[i]]
        remaining -= set(members)
        clusters.append((phrase.title(), [items[i] for i in members]))
    return clusters


def maker_sets(token, maker):
    """Search one maker's non-sport listings and split them into exact sets."""
    q = re.sub(r"\b(co|ltd|inc|s\.a|company|corp)\b\.?", "", maker, flags=re.I).strip(" .,")
    items = []
    for page in range(MAKER_PAGES):
        try:
            r = search(token, q=q, category_ids=NON_SPORT, limit="200", offset=str(page * 200),
                       filter="buyingOptions:{FIXED_PRICE},priceCurrency:USD")
        except RuntimeError as e:
            print(f"    (stopped '{maker}' page {page + 1}: {e})")
            break
        batch = r.get("itemSummaries", [])
        items += batch
        if len(batch) < 200:
            break
    old = []
    for it in items:
        ys = [int(y) for y in YEAR_RE.findall(it.get("title", ""))]
        if ys and min(ys) <= MAX_YEAR:
            old.append(it)
    if len(old) < MIN_CLUSTER:
        print(f"  skip maker {maker}: {len(old)} dated vintage listings")
        return []
    out = []
    for phrase, members in cluster_titles(old, maker):
        years = sorted(min(int(y) for y in YEAR_RE.findall(m["title"])) for m in members)
        year = years[len(years) // 2]
        name = f"{maker} - {phrase}"
        out.append(({"category": NON_SPORT, "maker": maker, "set": phrase, "name": name,
                     "year": year, "listed": len(members)}, members))
        print(f"  {name} (~{year}): {len(members)} listings")
    return out


KNOWN_MAKERS = [  # (pattern in listing titles, display name)
    (r"o-?pee-?chee|\bopc\b", "O-Pee-Chee"), (r"\btopps\b", "Topps"), (r"\bbowman\b", "Bowman"),
    (r"\bfleer\b", "Fleer"), (r"\bdonruss\b", "Donruss"), (r"\bleaf\b", "Leaf"),
    (r"philadelphia", "Philadelphia Gum"), (r"a\s*&\s*bc\b|\babc gum", "A&BC"),
    (r"\bgoudey\b", "Goudey"), (r"\bgum,? inc", "Gum Inc."), (r"national chicle", "National Chicle"),
    (r"exhibit", "Exhibit Supply Co."), (r"nu-?card", "Nu-Cards"), (r"bubbles,? inc", "Bubbles Inc."),
    (r"parkhurst", "Parkhurst"), (r"\bwills\b", "W.D. & H.O. Wills"), (r"player'?s\b", "John Player & Sons"),
    (r"carreras", "Carreras"), (r"godfrey phillips|\bphillips\b", "Godfrey Phillips"),
    (r"gallaher", "Gallaher"), (r"ogden'?s", "Ogden's"), (r"lambert", "Lambert & Butler"),
    (r"churchman", "Churchman's"), (r"allen\s*&\s*ginter", "Allen & Ginter"), (r"\bduke\b", "W. Duke Sons"),
    (r"kinney", "Kinney Bros."), (r"goodwin", "Goodwin & Co."), (r"liebig", "Liebig"),
    (r"imperial tobacco|\bitc\b", "Imperial Tobacco"), (r"american tobacco|\batc\b", "American Tobacco"),
    (r"\bmecca\b", "Mecca"), (r"\bhassan\b", "Hassan"), (r"\bfatima\b", "Fatima"),
    (r"sweet caporal", "Sweet Caporal"), (r"\bmurad\b", "Murad"), (r"cracker jack", "Cracker Jack"),
    (r"wheaties", "Wheaties"), (r"\bpost cereal", "Post"), (r"kellogg", "Kellogg's"),
    (r"brooke bond", "Brooke Bond"), (r"barratt", "Barratt"), (r"\ballens\b", "Allens"),
    (r"ardath", "Ardath"), (r"abdulla", "Abdulla"), (r"\bb\.?a\.?t\.?\b|british american", "British American Tobacco"),
    (r"monty gum", "Monty Gum"), (r"donruss", "Donruss"), (r"\bscanlens\b", "Scanlens"),
    (r"edito.?service", "Edito-Service"), (r"\bduocards?\b", "DuoCards"), (r"\bfrito", "Frito-Lay"),
]
KNOWN_MAKERS = [(re.compile(p, re.I), name) for p, name in KNOWN_MAKERS]


def maker_from_titles(items):
    """Most common maker named in the listing titles (needs 30%+ of titles)."""
    counts = {}
    for it in items:
        t = it.get("title", "")
        for rx, name in KNOWN_MAKERS:
            if rx.search(t):
                counts[name] = counts.get(name, 0) + 1
                break
    if not counts or not items:
        return ""
    name, n = max(counts.items(), key=lambda kv: kv[1])
    return name if n >= 0.3 * len(items) else ""


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
                  "aspect_filter": aspect_filter(s["category"], s["filters"]),
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
                "category": s["category"], "year": s.get("year"),
                "maker": s.get("maker", ""), "set_name": s.get("set", s["name"])}
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
        if set_change.get(set_name, 0) >= CLIMB_PCT:
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
            if ch and ch >= CLIMB_PCT:
                cards.append({**c, "card": k, "was": o["median"], "change_pct": ch})
    cards.sort(key=lambda c: c["change_pct"], reverse=True)
    sets, set_change = [], {}
    for name, s in snap["sets"].items():
        o = old["sets"].get(name)
        if o and s["listings"] >= MIN_SET_LISTINGS and o["listings"] >= MIN_SET_LISTINGS:
            ch = pct(s["median"], o["median"])
            set_change[name] = ch or 0
            if ch and ch >= CLIMB_PCT:
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


# ---------------- thumbnails ----------------
def save_thumbs(report):
    """Download small photos for every card shown in the report (cards/img/), so the
    report page can embed them (the page cannot load eBay images directly)."""
    import hashlib, shutil
    img_dir = DATA_DIR / "img"
    shutil.rmtree(img_dir, ignore_errors=True)
    img_dir.mkdir(parents=True, exist_ok=True)
    rows = (report["top_card_climbers"] + report["top_card_climbers_4wk"] + report["my_price_check"]
            + [c for cs in report["top3_per_set"].values() for c in cs])
    done, ok = {}, 0
    for row in rows:
        url = (row.get("image") or "").replace("s-l500", "s-l225")
        if not url:
            continue
        if url not in done:
            name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    (img_dir / name).write_bytes(r.read())
                done[url] = f"img/{name}"
                ok += 1
            except OSError:
                done[url] = None
        if done[url]:
            row["thumb"] = done[url]
    print(f"Saved {ok} thumbnails")


# ---------------- main ----------------
def main():
    token = ebay_token()
    print("Finding sets on eBay...")
    sets, makers = discover_sets(token)
    print(f"{len(sets)} named sets and {len(makers)} makers to check")
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
        s["maker"] = maker_from_titles(items)
        st = s["set"]
        s["name"] = st if (not s["maker"] or s["maker"].split()[0].lower() in st.lower()) else f"{s['maker']} - {st}"
        print(f"  {s['name']} (~{year}): {len(items)} listings")
        sets_items.append((s, items))
    print("Splitting makers into sets from listing titles...")
    have = {s["name"].lower() for s, _ in sets_items}
    maker_found = []
    for m in makers:
        for s, items in maker_sets(token, m):
            if s["name"].lower() not in have:
                have.add(s["name"].lower())
                maker_found.append((s, items))
    maker_found.sort(key=lambda si: -si[0]["listed"])
    sets_items += maker_found[:MAKER_SETS]
    print(f"Tracking {len(sets_items)} sets")
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
        "climb_threshold_pct": CLIMB_PCT,
        "sets_tracked": [{"set": k, **v} for k, v in sorted(snap["sets"].items(),
                                                            key=lambda kv: -kv[1]["listings"])],
        "top_card_climbers": card_up[:TOP_N],
        "top_set_climbers": set_up[:TOP_N],
        "top_card_climbers_4wk": card_up28[:TOP_N],
        "top_set_climbers_4wk": set_up28[:TOP_N],
        "top3_per_set": top3_per_set(snap, card_up),
        "my_price_check": price_check(mine, snap, set_change),
    }
    save_thumbs(report)
    (DATA_DIR / "discovery.json").write_text(json.dumps(DISCOVERY_LOG, indent=1))
    (DATA_DIR / "report.json").write_text(json.dumps(report, indent=1))
    print(f"Saved: {len(snap['sets'])} sets, {len(snap['cards'])} cards, "
          f"{len(card_up)} climbing cards, {len(report['my_price_check'])} of my listings checked.")
    if not base7:
        print("First week of history: climbers appear after next week's run.")


if __name__ == "__main__":
    main()
