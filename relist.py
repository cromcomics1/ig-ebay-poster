"""
Refresh stale eBay listings with "Sell similar": create a brand-new listing
copied from the old one (photos, description, price, shipping, returns) with an
improved title, extra item specifics and a condition note, then end the old one.

Two steps, both run from GitHub Actions (see relist.yml):

  MODE=propose  Pick listings (by item numbers, or by price/age rule from the
                audit) and write proposal.csv: old title, suggested title and
                suggested item specifics. Nothing on eBay changes.

  MODE=apply    For approved rows only. For each listing:
                  1. eBay verifies the new listing (no changes yet)
                  2. the old listing is ended
                  3. the new listing is created
                If step 3 fails, the old listing is relisted unchanged.
                DRY_RUN=true (the default) stops after step 1.

Files live on the 'relist-data' branch so they never collide with the poster.
Safety: max 50 per run, 1,500 per month, stops at the first failure or fee.
"""

import csv
import io
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import post  # reuse title cleaning

SELLER = "cromcomics1"
MAX_PER_RUN = 50
MAX_PER_MONTH = 1500
BRANCH = "relist-data"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}


class Skip(Exception):
    """This listing can't be processed, but it is safe to continue with the rest."""
API = "https://api.ebay.com/ws/api.dll"
PROPOSAL_COLS = ["approve", "item_id", "price", "new_price", "ad_rate", "age_days", "old_title",
                 "new_title", "add_specifics", "condition_note", "link"]
MAX_AD_RATE = 20.0          # typo guard: refuse ad rates above this %
CAMPAIGN_NAME = "Refreshed listings"
LOG_COLS = ["date", "old_item_id", "new_item_id", "status", "new_title", "new_price", "ad_rate", "fees", "message"]

MODE = os.environ.get("MODE", "propose")
MAX_FEE = float((os.environ.get("MAX_FEE") or "0").replace("$", "").strip() or 0)  # per listing
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"


# ---------------- storage on the relist-data branch ----------------
def git(*args, input_text=None, check=True):
    r = subprocess.run(["git", *args], text=True, capture_output=True, input=input_text)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip() if r.returncode == 0 else None


def load_branch_file(name):
    if git("fetch", "-q", "origin", BRANCH, check=False) is None:
        return None
    return git("show", f"FETCH_HEAD:{name}", check=False)


def save_branch_files(files, message):
    """files: {name: text}. Keeps other files already on the branch."""
    parent = git("rev-parse", "FETCH_HEAD", check=False) if git("fetch", "-q", "origin", BRANCH, check=False) is not None else None
    entries = {}
    if parent:
        for line in (git("ls-tree", parent) or "").splitlines():
            meta, fname = line.split("\t", 1)
            entries[fname] = meta
    for name, text in files.items():
        sha = git("hash-object", "-w", "--stdin", input_text=text)
        entries[name] = f"100644 blob {sha}"
    tree = git("mktree", input_text="".join(f"{m}\t{n}\n" for n, m in entries.items()))
    args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    commit = git(*args)
    git("push", "-q", "origin", f"{commit}:refs/heads/{BRANCH}")


def read_csv(text):
    return list(csv.DictReader(io.StringIO(text))) if text else []


def to_csv(rows, cols):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


# ---------------- suggestions ----------------
BRANDS = ["Hot Toys", "Sideshow", "ThreeZero", "Funko", "Kenner", "Mego", "Hasbro", "Mattel",
          "LEGO", "Lionel", "NECA", "McFarlane", "Bandai", "Playmates", "Lagos", "Tiffany"]
FRANCHISES = ["Star Wars", "Star Trek", "Game of Thrones", "Marvel", "DC Comics", "Batman",
              "Superman", "Transformers", "G.I. Joe", "Masters of the Universe", "Disney",
              "Harry Potter", "Lord of the Rings", "Pokemon", "Robocop", "Conan"]
CHARACTERS = ["Batman", "Superman", "Joker", "Wonder Woman", "Spider-Man", "Wolverine", "Hulk",
              "Thor", "Iron Man", "Captain America", "Black Panther", "Deadpool", "Venom",
              "Darth Vader", "Boba Fett", "Luke Skywalker", "Robocop", "Conan", "Mickey Mouse",
              "Cinderella", "Frankenstein"]
MATERIALS = ["Sterling Silver", "Brass", "Bronze", "Leather", "Cast Iron", "Ceramic", "Glass",
             "Porcelain", "Wood", "Copper", "Pewter", "Plastic", "Enamel", "Crystal"]
COLORS = ["Black", "White", "Red", "Blue", "Green", "Yellow", "Gold", "Silver", "Brown",
          "Orange", "Purple", "Pink"]
GRADERS = {"CGC": "Certified Guaranty Company (CGC)", "CBCS": "Comic Book Certification Service (CBCS)",
           "PSA": "Professional Sports Authenticator (PSA)", "BGS": "Beckett Grading Services (BGS)"}


def found(options, title):
    for o in options:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(o)}(?![A-Za-z0-9])", title, re.I):
            return o
    return None


def suggest_specifics(title, missing):
    """Only suggests values readable from the title, and only for specifics eBay
    recommends for this listing's category (taken from the audit)."""
    year = re.search(r"(?<!\d)(18[5-9]\d|19\d\d|20[0-2]\d)(?!\d)", title)
    y = int(year.group(1)) if year else None
    grader = found(list(GRADERS), title)
    grade = re.search(r"(?:CGC|CBCS|PSA|BGS)\s*(\d{1,2}(?:\.\d)?)(?!\d)", title, re.I)
    purity = re.search(r"(?<![A-Za-z0-9])(925|10k|14k|18k|24k)(?![A-Za-z0-9])", title, re.I)
    scale = re.search(r"(?<!\d)1/(6|12|18|4)(?!\d)", title)
    era = None
    if y:
        era = ("Golden Age (1938-55)" if 1938 <= y <= 1955 else "Silver Age (1956-69)" if 1956 <= y <= 1969
               else "Bronze Age (1970-83)" if 1970 <= y <= 1983 else "Copper Age (1984-1991)" if 1984 <= y <= 1991
               else "Modern Age (1992-Now)" if y >= 1992 else None)
    low = title.lower()
    guess = {
        "Brand": found(BRANDS, title),
        "Franchise": found(FRANCHISES, title),
        "Character": found(CHARACTERS, title),
        "Material": found(MATERIALS, title),
        "Color": found(COLORS, title),
        "Scale": f"1:{scale.group(1)}" if scale else None,
        "Publisher": "Marvel Comics" if re.search(r"\bmarvel\b", low) else "DC Comics" if re.search(r"\bdc\b", low) else None,
        "Professional Grader": GRADERS.get(grader.upper()) if grader else None,
        "Grade": grade.group(1) if grade else None,
        "Year Manufactured": str(y) if y else None,
        "Year": str(y) if y else None,
        "Publication Year": str(y) if y else None,
        "Time Period Manufactured": f"{y // 10 * 10}-{y // 10 * 10 + 9}" if y else None,
        "Era": era if "comic" in low or grader in ("CGC", "CBCS") else None,
        "Vintage": "Yes" if re.search(r"\b(vintage|antique|vtg)\b", low) else None,
        "Metal": "Sterling Silver" if "sterling" in low else None,
        "Metal Purity": purity.group(1).lower() if purity else None,
    }
    return [(k, v) for k, v in guess.items() if v and k in missing]


def suggest_title(title):
    t = post.clean_title(title)
    t = re.sub(r"(?i)\bONLY 4\b", "Only for", t)
    t = re.sub(r"\b4 (Toy|Figure)", r"for \1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t if len(t) <= 80 else title


def propose():
    audit_path = Path("audit/audit.csv")
    if not audit_path.exists():
        sys.exit("audit/audit.csv not found. Run the Listing audit workflow first (Actions > Listing audit).")
    audit = read_csv(audit_path.read_text(encoding="utf-8"))
    for r in audit:
        r["item_id"] = r["link"].rstrip("/").rsplit("/", 1)[-1]
    done = {r["old_item_id"] for r in read_csv(load_branch_file("log.csv")) if r["status"] == "relisted"}

    wanted = [i.strip() for i in os.environ.get("ITEMS", "").replace("\n", ",").split(",") if i.strip()]
    if wanted:
        rows = [r for r in audit if r["item_id"] in wanted]
        missing = set(wanted) - {r["item_id"] for r in rows}
        if missing:
            print(f"Not found in the audit (ended, or typo?): {', '.join(sorted(missing))}")
    else:
        min_price = float(os.environ.get("MIN_PRICE") or 100)
        min_age = int(os.environ.get("MIN_AGE") or 180)
        count = int(os.environ.get("COUNT") or 5)
        rows = [r for r in audit if float(r["price"]) >= min_price and int(r["age_days"] or 0) >= min_age]
        rows.sort(key=lambda r: float(r["price"]), reverse=True)
        rows = [r for r in rows if r["item_id"] not in done][:count]

    pct = float((os.environ.get("PRICE_CHANGE") or "0").replace("%", "").strip() or 0)
    ad_rate = (os.environ.get("AD_RATE") or "").replace("%", "").strip()
    out = []
    for r in rows:
        if r["item_id"] in done:
            print(f"Already relisted, skipping: {r['item_id']}")
            continue
        miss = [m.strip() for m in r["missing_recommended"].split(";") if m.strip()]
        specs = suggest_specifics(r["title"], miss)
        out.append({
            "approve": "", "item_id": r["item_id"], "price": r["price"],
            # round up to the next whole dollar (27.49 -> 28.00)
            "new_price": f"{math.ceil(round(float(r['price']) * (1 + pct / 100), 2)):.2f}" if pct else "",
            "ad_rate": ad_rate, "age_days": r["age_days"],
            "old_title": r["title"], "new_title": suggest_title(r["title"]),
            "add_specifics": "; ".join(f"{k}={v}" for k, v in specs),
            "condition_note": "", "link": r["link"],
        })
    if not out:
        sys.exit("No listings matched.")
    save_branch_files({"proposal.csv": to_csv(out, PROPOSAL_COLS)}, f"Proposal: {len(out)} listings")
    print(f"\nPlan saved with {len(out)} listings (nothing on eBay has changed).\n"
          "Next: run STEP \"2 - Carry out the plan\" with PRACTICE ONLY on.\n")
    for r in out:
        print(f"{r['item_id']}  ${r['price']}  {r['age_days']} days")
        print(f"   old: {r['old_title']}")
        print(f"   new: {r['new_title']}")
        print(f"   add: {r['add_specifics'] or '(nothing readable from the title)'}")
        print(f"   price: {'$' + r['new_price'] if r['new_price'] else 'unchanged'}   ad rate: {r['ad_rate'] + '%' if r['ad_rate'] else 'none'}\n")


# ---------------- eBay Trading API ----------------
class EbayError(RuntimeError):
    def __init__(self, message, codes=()):
        super().__init__(message)
        self.codes = list(codes)


READ_ONLY_CALLS = ("GetItem", "VerifyAddFixedPriceItem")


def trading(call, inner):
    token = os.environ["EBAY_USER_TOKEN"]
    body = (f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<RequesterCredentials><eBayAuthToken>{escape(token)}</eBayAuthToken></RequesterCredentials>"
            f"<ErrorLanguage>en_US</ErrorLanguage><WarningLevel>Low</WarningLevel>{inner}</{call}Request>")
    headers = {"X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": "1193",
               "X-EBAY-API-CALL-NAME": call, "Content-Type": "text/xml"}
    last = None
    # AddFixedPriceItem carries a UUID, so eBay refuses to create it twice: safe to retry
    retry = call in READ_ONLY_CALLS or call == "AddFixedPriceItem"
    for attempt in range(3):
        try:
            req = urllib.request.Request(API, data=body.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                root = ET.fromstring(r.read())
            break
        except (OSError, ET.ParseError) as e:
            last = e
            if not retry:
                raise RuntimeError(f"{call}: network error, result unknown ({e}). Check this listing on eBay.")
            time.sleep(5 * (attempt + 1))
    else:
        raise RuntimeError(f"{call}: network error ({last})")
    ack = root.findtext("e:Ack", "", NS)
    errs = root.findall("e:Errors", NS)
    texts = [f"{e.findtext('e:SeverityCode', '', NS)}: {e.findtext('e:LongMessage', '', NS)}" for e in errs]
    if ack not in ("Success", "Warning"):
        codes = [e.findtext("e:ErrorCode", "", NS) for e in errs if e.findtext("e:SeverityCode", "", NS) == "Error"]
        err = EbayError(f"{call} failed: " + " | ".join(t for t in texts if t.startswith("Error"))[:600], codes)
        err.root = root
        raise err
    return root, [t for t in texts if t.startswith("Warning")]


def get_item(item_id):
    root, _ = trading("GetItem", f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
                                 "<IncludeItemSpecifics>true</IncludeItemSpecifics>")
    it = root.find("e:Item", NS)
    specifics = []
    for nv in it.findall("e:ItemSpecifics/e:NameValueList", NS):
        specifics.append((nv.findtext("e:Name", "", NS), [v.text or "" for v in nv.findall("e:Value", NS)]))
    return {
        "xml": it,
        "title": it.findtext("e:Title", "", NS),
        "seller": it.findtext("e:Seller/e:UserID", "", NS),
        "status": it.findtext("e:SellingStatus/e:ListingStatus", "", NS),
        "type": it.findtext("e:ListingType", "", NS),
        "has_variations": it.find("e:Variations", NS) is not None,
        "specifics": specifics,
    }


def parse_specifics(text):
    pairs = []
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                pairs.append((k.strip(), v.strip()))
    return pairs


# ---------------- build the "sell similar" copy ----------------
def local(el):
    return el.tag.split("}", 1)[-1]


def ser(el, drop=(), keep=None):
    """Serialize an element from eBay's reply for re-sending (namespace removed).
    drop: child tag names to leave out at any depth. keep: only these direct children."""
    name = local(el)
    attrs = "".join(f' {k.split("}", 1)[-1]}="{escape(v, {chr(34): "&quot;"})}"' for k, v in el.attrib.items())
    kids = [c for c in el if local(c) not in drop and (keep is None or local(c) in keep)]
    inner = escape(el.text or "") if not len(el) else "".join(ser(c, drop) for c in kids)
    return f"<{name}{attrs}>{inner}</{name}>"


# fields eBay fills in itself; sending them back causes warnings or errors
SHIPPING_DROP = ("SellingManagerSalesRecordNumber", "ThirdPartyCheckout", "TaxTable", "ApplyShippingDiscount",
                 "InsuranceFee", "InsuranceOption", "InsuranceDetails", "InternationalInsuranceDetails",
                 "PaymentEdited", "ExpeditedService", "ShippingTimeMin", "ShippingTimeMax", "ImportCharge",
                 "GetItFast", "SellerExcludeShipToLocationsPreference", "CODCost")
RETURN_KEEP = ("ReturnsAcceptedOption", "ReturnsWithinOption", "RefundOption", "ShippingCostPaidByOption",
               "Description", "InternationalReturnsAcceptedOption", "InternationalReturnsWithinOption",
               "InternationalRefundOption", "InternationalShippingCostPaidByOption")
COPY_AS_IS = ("Description", "ConditionID", "ConditionDescriptors", "Country", "Currency",
              "DispatchTimeMax", "ListingType", "Location", "PostalCode", "ShippingPackageDetails",
              "ShipToLocations", "SKU", "Site", "PrivateListing", "ItemCompatibilityList", "SubTitle")


def build_new_item(item, title, specifics, note, uuid, new_price=None):
    it = item["xml"]
    find = lambda path: it.find(path, NS)
    x = [f"<UUID>{uuid}</UUID>", f"<Title>{escape(title)}</Title>", "<ListingDuration>GTC</ListingDuration>",
         "<CategoryMappingAllowed>true</CategoryMappingAllowed>"]
    for tag in COPY_AS_IS:
        el = find(f"e:{tag}")
        if el is not None:
            x.append(ser(el))
    old_price = float(it.findtext("e:StartPrice", "0", NS) or 0)
    price = new_price if new_price else old_price
    ratio = price / old_price if old_price else 1.0
    cur = (find("e:StartPrice").get("currencyID") if find("e:StartPrice") is not None else None) or "USD"
    x.append(f'<StartPrice currencyID="{cur}">{price:.2f}</StartPrice>')
    for tag in ("PrimaryCategory", "SecondaryCategory"):
        cid = it.findtext(f"e:{tag}/e:CategoryID", "", NS)
        if cid and cid != "0":
            x.append(f"<{tag}><CategoryID>{cid}</CategoryID></{tag}>")
    qty = int(it.findtext("e:Quantity", "1", NS) or 1) - int(it.findtext("e:SellingStatus/e:QuantitySold", "0", NS) or 0)
    x.append(f"<Quantity>{max(qty, 1)}</Quantity>")
    cond_note = note or it.findtext("e:ConditionDescription", "", NS)
    if cond_note:
        x.append(f"<ConditionDescription>{escape(cond_note)}</ConditionDescription>")
    pics = [p.text for p in it.findall("e:PictureDetails/e:PictureURL", NS) if p.text]
    if not pics:
        raise Skip("no photos found on the old listing")
    x.append("<PictureDetails>" + "".join(f"<PictureURL>{escape(p)}</PictureURL>" for p in pics[:24]) + "</PictureDetails>")
    if specifics:
        x.append("<ItemSpecifics>" + "".join(
            f"<NameValueList><Name>{escape(n)}</Name>" + "".join(f"<Value>{escape(v)}</Value>" for v in vals) + "</NameValueList>"
            for n, vals in specifics) + "</ItemSpecifics>")
    profiles = find("e:SellerProfiles")
    if profiles is not None and profiles.find("e:SellerShippingProfile/e:ShippingProfileID", NS) is not None:
        p = "<SellerProfiles>"
        for kind, idtag in (("SellerShippingProfile", "ShippingProfileID"), ("SellerReturnProfile", "ReturnProfileID"),
                            ("SellerPaymentProfile", "PaymentProfileID")):
            pid = profiles.findtext(f"e:{kind}/e:{idtag}", "", NS)
            if pid:
                p += f"<{kind}><{idtag}>{pid}</{idtag}></{kind}>"
        x.append(p + "</SellerProfiles>")
    else:  # no business policies: copy the shipping and return settings directly
        ship = find("e:ShippingDetails")
        if ship is not None:
            x.append(ser(ship, drop=SHIPPING_DROP))
        ret = find("e:ReturnPolicy")
        if ret is not None:
            x.append(ser(ret, keep=RETURN_KEEP))
    if it.findtext("e:BestOfferDetails/e:BestOfferEnabled", "", NS) == "true":
        x.append("<BestOfferDetails><BestOfferEnabled>true</BestOfferEnabled></BestOfferDetails>")
        ld = ""
        for tag in ("BestOfferAutoAcceptPrice", "MinimumBestOfferPrice"):
            el = find(f"e:ListingDetails/e:{tag}")
            if el is not None and float(el.text or 0) > 0:  # keep offer limits in step with the price
                ld += f'<{tag} currencyID="{cur}">{float(el.text) * ratio:.2f}</{tag}>'
        if ld:
            x.append(f"<ListingDetails>{ld}</ListingDetails>")
    store = ""
    for tag in ("StoreCategoryID", "StoreCategory2ID"):
        v = it.findtext(f"e:Storefront/e:{tag}", "", NS)
        if v and v != "0":
            store += f"<{tag}>{v}</{tag}>"
    if store:
        x.append(f"<Storefront>{store}</Storefront>")
    pld = find("e:ProductListingDetails")
    if pld is not None:
        x.append(ser(pld, keep=("UPC", "EAN", "ISBN", "BrandMPN", "ProductReferenceID", "IncludeeBayProductDetails")))
    return "<Item>" + "".join(x) + "</Item>"


def total_fees(root):
    """Net cost of the listing. eBay lists each fee next to a PromotionalDiscount; free
    store listings appear as e.g. Fee $0.10 with a $0.10 discount, so the net is $0.00."""
    fees = root.findall("e:Fees/e:Fee", NS)
    def net(fee):
        amount = float(fee.findtext("e:Fee", "0", NS) or 0)
        discount = float(fee.findtext("e:PromotionalDiscount", "0", NS) or 0)
        return max(amount - discount, 0.0)
    for fee in fees:
        if fee.findtext("e:Name", "", NS) == "ListingFee":  # the total of all listing fees
            return round(net(fee), 2)
    return round(sum(net(f) for f in fees if f.findtext("e:Name", "", NS) != "ListingFee"), 2)


DUPLICATE_LISTING = "21919067"  # "this is a duplicate of your item" (expected while the old one is live)


def fee_breakdown(root):
    parts = []
    for fee in root.findall("e:Fees/e:Fee", NS):
        amount = float(fee.findtext("e:Fee", "0", NS) or 0)
        discount = float(fee.findtext("e:PromotionalDiscount", "0", NS) or 0)
        if amount or discount:
            parts.append(f"{fee.findtext('e:Name', '', NS)} ${amount:.2f}" + (f" - discount ${discount:.2f}" if discount else ""))
    return "; ".join(parts) or "no fees listed"


def verify(new_item):
    """Ask eBay to check the new listing without creating it. Returns the net fee it would charge."""
    try:
        root, _ = trading("VerifyAddFixedPriceItem", new_item)
        print(f"   eBay fee quote: {fee_breakdown(root)}")
        return total_fees(root)
    except EbayError as e:
        if e.codes and all(c == DUPLICATE_LISTING for c in e.codes):
            return 0.0  # only complaint is the duplicate, which goes away once the old one ends
        raise Skip(f"eBay rejected the new listing, nothing was changed: {e}")


def money(text):
    """'24.99', '$24.99', '5%' -> float; blank -> None."""
    t = (text or "").replace("$", "").replace("%", "").replace(",", "").strip()
    try:
        return float(t) if t else None
    except ValueError:
        raise Skip(f"can't read the number '{text}'")


def rest(method, url, token, body=None):
    """Small JSON helper for eBay's REST APIs. Returns (status, headers, data)."""
    import json
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, dict(r.headers), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {e.read().decode(errors='replace')[:400]}")
    except OSError as e:
        raise RuntimeError(f"{method} {url.split('?')[0]} -> network error: {e}")


class Marketing:
    """Promoted Listings (pay only when an item sells through the ad)."""
    BASE = "https://api.ebay.com/sell/marketing/v1"
    SCOPE = "https://api.ebay.com/oauth/api_scope/sell.marketing"

    def __init__(self, token):
        self.token = token
        self.campaign_id = None

    @classmethod
    def connect(cls):
        refresh = os.environ.get("EBAY_REFRESH_TOKEN")
        if not refresh:
            return None
        import base64
        import urllib.parse
        creds = base64.b64encode(f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}".encode()).decode()
        form = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh, "scope": cls.SCOPE}).encode()
        req = urllib.request.Request("https://api.ebay.com/identity/v1/oauth2/token", data=form, method="POST",
                                     headers={"Authorization": f"Basic {creds}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            import json
            with urllib.request.urlopen(req, timeout=60) as r:
                return cls(json.loads(r.read().decode())["access_token"])
        except (urllib.error.HTTPError, OSError) as e:
            print(f"Advertising access failed ({e}); ad rates will be skipped. Re-run the eBay advertising sign-in.")
            return None

    def find_campaign(self):
        if self.campaign_id:
            return self.campaign_id
        _, _, data = rest("GET", f"{self.BASE}/ad_campaign?campaign_name={CAMPAIGN_NAME.replace(' ', '%20')}"
                                 "&campaign_status=RUNNING&limit=10", self.token)
        for c in data.get("campaigns", []):
            if c.get("campaignName") == CAMPAIGN_NAME:
                self.campaign_id = c["campaignId"]
        return self.campaign_id

    def campaign_label(self):
        return f'"{CAMPAIGN_NAME}"' + (" (exists)" if self.find_campaign() else " (will be created)")

    def promote(self, listing_id, rate):
        if not self.find_campaign():
            start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            _, headers, _ = rest("POST", f"{self.BASE}/ad_campaign", self.token, {
                "campaignName": CAMPAIGN_NAME, "marketplaceId": "EBAY_US", "startDate": start,
                "fundingStrategy": {"fundingModel": "COST_PER_SALE", "bidPercentage": f"{rate:.1f}"}})
            loc = headers.get("Location") or headers.get("location") or ""
            self.campaign_id = loc.rstrip("/").rsplit("/", 1)[-1] or self.find_campaign()
            if not self.campaign_id:
                raise RuntimeError("created the campaign but could not read its id")
        rest("POST", f"{self.BASE}/ad_campaign/{self.campaign_id}/ad", self.token,
             {"listingId": str(listing_id), "bidPercentage": f"{rate:.1f}"})


def apply():
    proposal = read_csv(load_branch_file("proposal.csv"))
    if not proposal:
        sys.exit("No plan found. Run STEP \"1 - Make a plan\" first.")
    log = read_csv(load_branch_file("log.csv"))
    done = {r["old_item_id"] for r in log if r["status"] == "relisted"}
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    this_month = sum(1 for r in log if r["status"] == "relisted" and r["date"].startswith(month))

    approve = os.environ.get("APPROVE", "").strip().lower()
    pending = [r for r in proposal if r["item_id"] not in done]
    if approve.isdigit() and len(approve) <= 3:
        # a small number like "3" means: the first 3 listings in the proposal
        rows = pending[:int(approve)]
        print(f"Approving the first {len(rows)} listing(s) in the proposal.")
    else:
        approved_ids = {a.strip() for a in approve.replace(" ", ",").split(",") if a.strip()}
        rows = [r for r in pending if (
            approve == "all" or r["item_id"] in approved_ids
            or r.get("approve", "").strip().lower() in ("yes", "y", "x", "ok", "approve", "approved"))]
    if not rows:
        ids = ", ".join(r["item_id"] for r in pending[:10]) or "(none left)"
        if not pending:
            sys.exit("Everything in the current plan has already been relisted. "
                     "Run STEP \"1 - Make a plan\" to pick new items.")
        sys.exit("No items selected. In WHICH ITEMS type: all, a count like 3, or item numbers "
                 f"from the plan. Items in the current plan: {ids}")
    room = min(MAX_PER_RUN, MAX_PER_MONTH - this_month)
    if len(rows) > room:
        print(f"Limiting this run to {room} listings (safety cap).")
        rows = rows[:max(room, 0)]

    marketing = Marketing.connect() if any((r.get("ad_rate") or "").strip() for r in rows) else None
    print(f"{'PRACTICE ONLY (nothing will change): ' if DRY_RUN else 'LIVE: '}{len(rows)} listing(s) to sell-similar. Done this month so far: {this_month}\n")
    new_log, stop = [], False
    for r in rows:
        iid = r["item_id"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = {"date": now, "old_item_id": iid, "new_item_id": "", "status": "", "new_title": "", "fees": "", "message": ""}
        try:
            print(f"{iid}")
            item = get_item(iid)
            if item["seller"].lower() != SELLER:
                raise Skip(f"not your listing (seller {item['seller']})")
            if item["status"] != "Active":
                raise Skip(f"listing is not active ({item['status']})")
            if item["type"] != "FixedPriceItem":
                raise Skip(f"not a fixed-price listing ({item['type']})")
            if item["has_variations"]:
                raise Skip("listing has variations; refresh this one by hand")
            title = (r.get("new_title") or "").strip() or item["title"]
            if len(title) > 80:
                raise Skip(f"new title is {len(title)} characters (max 80)")
            have = {n.lower() for n, _ in item["specifics"]}
            added = [(k, [v]) for k, v in parse_specifics(r.get("add_specifics", "")) if k.lower() not in have]
            note = (r.get("condition_note") or "").strip()
            entry["new_title"] = title
            old_price = float(item["xml"].findtext("e:StartPrice", "0", NS) or 0)
            new_price = money(r.get("new_price"))
            if new_price and old_price and not (0.5 * old_price <= new_price <= 3 * old_price):
                raise Skip(f"new price ${new_price:.2f} is far from the current ${old_price:.2f}; "
                           "check for a typo (allowed range: half to triple)")
            rate = money(r.get("ad_rate"))
            if rate and not (2 <= rate <= MAX_AD_RATE):
                raise Skip(f"ad rate {rate}% is outside the allowed 2% to {MAX_AD_RATE:.0f}%")
            entry["new_price"] = f"{new_price:.2f}" if new_price else ""
            uuid = os.urandom(16).hex().upper()
            new_item = build_new_item(item, title, item["specifics"] + added, note, uuid, new_price)

            print(f"   title: {item['title']}")
            if title != item["title"]:
                print(f"      ->  {title}")
            print(f"   specifics: keeping {len(item['specifics'])}, adding {', '.join(f'{k}={v[0]}' for k, v in added) or 'none'}")
            if note:
                print(f"   condition note: {note}")
            if new_price:
                print(f"   price: ${old_price:.2f}  ->  ${new_price:.2f}")
            if rate:
                print(f"   Promoted Listings ad rate: {rate}%")
                if marketing is None:
                    print("   (ad rate will be skipped: advertising access is not set up yet)")

            fee = verify(new_item)  # eBay checks the new listing; nothing is created or ended yet
            print(f"   eBay check passed. Listing fee: ${fee:.2f}")
            if fee > MAX_FEE + 0.001:
                raise Skip(f"eBay would charge ${fee:.2f} for this listing; your fee limit is ${MAX_FEE:.2f}. "
                           "To allow it, raise CARRY OUT: max fee per listing")
            if DRY_RUN:
                entry["status"] = "dry-run"
                if rate and marketing:
                    print(f"   advertising access OK (campaign: {marketing.campaign_label()})")
                print("   (practice run: nothing changed)\n")
                continue

            trading("EndFixedPriceItem", f"<ItemID>{iid}</ItemID><EndingReason>OtherListingError</EndingReason>")
            print("   old listing ended")
            try:
                root, warns = trading("AddFixedPriceItem", new_item)
            except RuntimeError as e:
                # never leave an item ended: put the old listing back up unchanged
                print(f"   creating the new listing failed ({e}); relisting the old one unchanged")
                root, warns = trading("RelistFixedPriceItem", f"<Item><ItemID>{iid}</ItemID></Item>")
                entry["message"] = f"relisted WITHOUT changes: {e}"[:300]
                entry["new_title"] = item["title"]
                stop = True
            new_id = root.findtext("e:ItemID", "", NS)
            fees = total_fees(root)
            entry.update(new_item_id=new_id, status="relisted", fees=f"{fees:.2f}")
            if warns and not entry["message"]:
                entry["message"] = " | ".join(warns)[:300]
            print(f"   new listing: {new_id}  https://www.ebay.com/itm/{new_id}   fees: ${fees:.2f}")
            # confirm every photo carried over to the new listing
            old_pics = len(item["xml"].findall("e:PictureDetails/e:PictureURL", NS))
            try:
                new_pics = len(get_item(new_id)["xml"].findall("e:PictureDetails/e:PictureURL", NS))
                print(f"   photos: {new_pics} of {old_pics} carried over\n")
                if new_pics < min(old_pics, 24):
                    entry["message"] = (entry["message"] + f" PHOTOS: only {new_pics} of {old_pics}").strip()
                    print("STOPPING: some photos did not carry over. Check the new listing.")
                    stop = True
            except RuntimeError as e:
                print(f"   (could not re-check photos: {e})\n")
            if rate and marketing and entry["status"] == "relisted" and not entry["message"].startswith("relisted WITHOUT"):
                try:
                    marketing.promote(new_id, rate)
                    entry["ad_rate"] = f"{rate:g}"
                    print(f"   promoted at {rate:g}% ad rate\n")
                except RuntimeError as e:  # the listing is fine; only the ad failed
                    entry["message"] = (entry["message"] + f" AD NOT SET: {e}")[:300].strip()
                    print(f"   could not set the ad rate: {e}\n")
            if fees > MAX_FEE + 0.001:
                print(f"STOPPING: eBay charged ${fees:.2f}, above your limit of ${MAX_FEE:.2f}.")
                stop = True
            time.sleep(1)
        except Skip as e:
            entry.update(status="skipped", message=str(e)[:300])
            print(f"   SKIPPED: {e}\n")
        except RuntimeError as e:
            entry.update(status="failed", message=str(e)[:300])
            print(f"   FAILED: {e}\n")
            if not DRY_RUN:
                stop = True  # an eBay-side failure: stop so nothing else is touched
        finally:
            new_log.append(entry)
        if stop:
            print("Batch stopped early. Review the message above before running again.")
            break

    if not DRY_RUN:
        save_branch_files({"log.csv": to_csv(log + new_log, LOG_COLS)}, f"Sell-similar log: {len(new_log)} entries")
    ok = sum(1 for e in new_log if e["status"] == "relisted")
    passed = sum(1 for e in new_log if e["status"] == "dry-run")
    skipped = sum(1 for e in new_log if e["status"] in ("failed", "skipped"))
    print(f"Done. New listings: {ok}. Passed practice check: {passed}. Failed/skipped: {skipped}.")
    if any(e["status"] == "failed" for e in new_log) and not DRY_RUN:
        sys.exit(1)


if __name__ == "__main__":
    apply() if MODE == "apply" else propose()
