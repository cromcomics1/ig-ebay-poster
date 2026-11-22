"""
New listings from an iCloud shared album, through the eBay API.

Run from GitHub Actions (newlist.yml). Data lives on the 'newlist-data' branch.

  MODE=sheets   Reads the album (its Public Website link, ALBUM_URL secret) and saves
                numbered contact sheets (sheets/sheet-01.jpg ...) plus index.json
                (photo number -> iCloud photo id, date taken). Nothing on eBay changes.
                Claude looks at the sheets, groups photos into items and writes
                batch.json with the listings Yan approved.

  MODE=list     For each approved item in batch.json that isn't done yet:
                  1. downloads its photos, fixes rotation, uploads them to eBay
                  2. eBay checks the listing (VerifyAddFixedPriceItem) and quotes the fee
                  3. PRACTICE ONLY off: creates the listing and adds it to the ad campaign
                Results go to results.csv (every run) and log.csv (real listings).

batch.json:
  {"template_item": "",            # optional: copy shipping/returns/location from this listing
   "ad_rate": 5.1, "items": [
     {"id": "patch-01", "approve": true, "title": "...", "price": 24.99,
      "description": "<p>...</p>", "category_id": "",      # blank = eBay suggests one
      "condition_id": 3000, "condition_note": "...",
      "specifics": {"Brand": ["..."]}, "photos": [12, {"n": 13, "rotate": 90}],
      "quantity": 1, "best_offer": true, "ad_rate": 5.1}]}
"""

import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import relist
from relist import NS, EbayError, Marketing, fee_breakdown, git, total_fees, trading

BRANCH = "newlist-data"
relist.BRANCH = BRANCH  # relist's branch helpers read this
MODE = os.environ.get("MODE", "sheets")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
MAX_FEE = float((os.environ.get("MAX_FEE") or "0").replace("$", "").strip() or 0)
MAX_PER_RUN = 50
UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "text/plain", "Origin": "https://www.icloud.com"}
RESULT_COLS = ["date", "id", "status", "item_id", "title", "price", "ad_rate", "campaign", "fees", "link", "message"]


# ---------------- iCloud shared album (Public Website link) ----------------
def album_token(url):
    tok = url.strip().split("#")[-1].split(";")[0].strip("/")
    if not tok or "/" in tok:
        sys.exit("ALBUM_URL must be the album's Public Website link, like https://www.icloud.com/sharedalbum/#B0aBcD...")
    return tok


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 330:  # "use this other server"
            return 330, json.loads(e.read().decode() or "{}")
        raise RuntimeError(f"iCloud HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


class Album:
    def __init__(self, url):
        self.token = album_token(url)
        self.host = "p23-sharedstreams.icloud.com"

    def call(self, name, body):
        for _ in range(3):
            code, data = _post(f"https://{self.host}/{self.token}/sharedstreams/{name}", body)
            if code == 330:
                self.host = data.get("X-Apple-MMe-Host") or self.host
                continue
            return data
        raise RuntimeError("iCloud kept redirecting; is the album's Public Website turned on?")

    def photos(self):
        data = self.call("webstream", {"streamCtag": None})
        out = []
        for p in data.get("photos", []):
            if p.get("mediaAssetType") == "video":
                continue
            ders = [d for d in (p.get("derivatives") or {}).values() if d.get("checksum")]
            if not ders:
                continue
            best = max(ders, key=lambda d: int(d.get("width") or 0) * int(d.get("height") or 0))
            small = min(ders, key=lambda d: int(d.get("width") or 0) * int(d.get("height") or 0))
            out.append({"guid": p["photoGuid"], "date": p.get("dateCreated") or p.get("batchDateCreated") or "",
                        "big": best["checksum"], "small": small["checksum"]})
        return data.get("streamName", ""), out

    def urls(self, guids):
        found = {}
        for i in range(0, len(guids), 25):
            data = self.call("webasseturls", {"photoGuids": guids[i:i + 25]})
            locs = data.get("locations", {})
            for checksum, it in data.get("items", {}).items():
                loc = locs.get(it.get("url_location"), {})
                found[checksum] = f"{loc.get('scheme', 'https')}://{loc.get('hosts', [''])[0]}{it.get('url_path', '')}"
        return found


def fetch(url, tries=3):
    for n in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=120) as r:
                return r.read()
        except OSError:
            if n == tries - 1:
                raise
            time.sleep(3 * (n + 1))


def open_image(data, rotate=0):
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    if rotate % 360:
        im = im.rotate(-(rotate % 360), expand=True)  # rotate clockwise by `rotate` degrees
    return im


# ---------------- branch storage (binary-safe) ----------------
def save_files(files, message):
    """files: {name: bytes or str}. Keeps the other files already on the branch."""
    parent = git("rev-parse", "FETCH_HEAD", check=False) if git("fetch", "-q", "origin", BRANCH, check=False) is not None else None
    entries = {}
    if parent:
        for line in (git("ls-tree", "-r", parent) or "").splitlines():
            meta, fname = line.split("\t", 1)
            entries[fname] = meta
    with tempfile.TemporaryDirectory() as d:
        for name, content in files.items():
            p = os.path.join(d, "f")
            with open(p, "wb") as f:
                f.write(content.encode() if isinstance(content, str) else content)
            entries[name] = f"100644 blob {git('hash-object', '-w', p)}"
    # rebuild nested trees
    tree = _mktree(entries)
    args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    git("push", "-q", "origin", f"{git(*args)}:refs/heads/{BRANCH}")


def _mktree(entries):
    here, sub = {}, {}
    for name, meta in entries.items():
        if "/" in name:
            top, rest_ = name.split("/", 1)
            sub.setdefault(top, {})[rest_] = meta
        else:
            here[name] = meta
    lines = [f"{m}\t{n}" for n, m in here.items()]
    lines += [f"040000 tree {_mktree(v)}\t{k}" for k, v in sub.items()]
    return git("mktree", input_text="".join(l + "\n" for l in lines))


def load(name):
    return relist.load_branch_file(name)


# ---------------- MODE=sheets ----------------
def sheets():
    from PIL import Image, ImageDraw, ImageFont
    name, photos = Album(os.environ["ALBUM_URL"]).photos()
    photos.sort(key=lambda p: (p["date"], p["guid"]))
    print(f'Album "{name}": {len(photos)} photos')
    album = Album(os.environ["ALBUM_URL"])
    urls = album.urls([p["guid"] for p in photos])
    index, files = {}, {}
    per, cols, cell = 30, 6, 300
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    for s in range(0, len(photos), per):
        chunk = photos[s:s + per]
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell, rows * cell), "white")
        draw = ImageDraw.Draw(sheet)
        for i, p in enumerate(chunk):
            n = s + i + 1
            index[str(n)] = {"guid": p["guid"], "date": p["date"]}
            try:
                im = open_image(fetch(urls[p["small"]]))
                im.thumbnail((cell - 10, cell - 10))
                x, y = (i % cols) * cell, (i // cols) * cell
                sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
            except Exception as e:  # keep going; the number still shows
                print(f"   photo {n}: {e}")
                x, y = (i % cols) * cell, (i // cols) * cell
            draw.rectangle([x + 4, y + 4, x + 74, y + 44], fill="black")
            draw.text((x + 8, y + 6), str(n), fill="yellow", font=font)
        buf = io.BytesIO()
        sheet.save(buf, "JPEG", quality=80)
        files[f"sheets/sheet-{s // per + 1:02d}.jpg"] = buf.getvalue()
        print(f"   sheet {s // per + 1}: photos {s + 1}-{s + len(chunk)}")
    files["index.json"] = json.dumps({"album": name, "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "photos": index}, indent=1)
    save_files(files, f"Contact sheets: {len(photos)} photos")
    print(f"Saved {len(files) - 1} sheets and index.json to the {BRANCH} branch.")


# ---------------- MODE=list ----------------
def upload_picture(jpeg, name):
    token = os.environ["EBAY_USER_TOKEN"]
    xml = (f'<?xml version="1.0" encoding="utf-8"?><UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           f"<RequesterCredentials><eBayAuthToken>{escape(token)}</eBayAuthToken></RequesterCredentials>"
           f"<PictureName>{escape(name[:50])}</PictureName><PictureSet>Supersize</PictureSet>"
           f"<ExtensionInDays>30</ExtensionInDays></UploadSiteHostedPicturesRequest>")
    boundary = "----crom" + uuidlib.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="XML Payload"\r\n'
            f"Content-Type: text/xml;charset=utf-8\r\n\r\n{xml}\r\n"
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="image.jpg"\r\n'
            f"Content-Type: image/jpeg\r\nContent-Transfer-Encoding: binary\r\n\r\n").encode() + jpeg + f"\r\n--{boundary}--\r\n".encode()
    headers = {"X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": "1193",
               "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    import xml.etree.ElementTree as ET
    for attempt in range(3):
        try:
            req = urllib.request.Request(relist.API, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                root = ET.fromstring(r.read())
            break
        except OSError as e:
            if attempt == 2:
                raise RuntimeError(f"photo upload failed: {e}")
            time.sleep(5 * (attempt + 1))
    url = root.findtext("e:SiteHostedPictureDetails/e:FullURL", "", NS)
    if not url:
        msgs = [e.findtext("e:LongMessage", "", NS) for e in root.findall("e:Errors", NS)]
        raise RuntimeError("photo upload failed: " + " | ".join(msgs))
    return url


_app_token = None


def app_token():
    global _app_token
    if not _app_token:
        import base64
        creds = base64.b64encode(f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}".encode()).decode()
        form = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
        req = urllib.request.Request("https://api.ebay.com/identity/v1/oauth2/token", data=form, method="POST",
                                     headers={"Authorization": f"Basic {creds}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=60) as r:
            _app_token = json.loads(r.read().decode())["access_token"]
    return _app_token


def suggest_category(title):
    q = urllib.parse.quote(title[:350])
    _, _, data = relist.rest("GET", f"https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q={q}",
                             app_token())
    sugg = data.get("categorySuggestions") or []
    if not sugg:
        raise RuntimeError("eBay had no category suggestion; put a category_id in the batch")
    c = sugg[0]["category"]
    return c["categoryId"], c.get("categoryName", "")


def template_item(batch):
    tid = str(batch.get("template_item") or os.environ.get("TEMPLATE_ITEM") or "").strip()
    if not tid:
        cat = json.load(open("catalog.json"))
        cat.sort(key=lambda c: c.get("itemCreationDate") or "", reverse=True)
        tid = cat[0]["legacyItemId"]
    item = relist.get_item(tid)
    print(f"Shipping/returns/location copied from your listing {tid}: {item['title'][:60]}")
    return item["xml"]


def build_item(row, pics, category_id, tmpl, uid):
    find = lambda p: tmpl.find(p, NS)
    x = [f"<UUID>{uid}</UUID>", f"<Title>{escape(row['title'][:80])}</Title>",
         f"<Description><![CDATA[{row.get('description', '')}]]></Description>",
         "<ListingType>FixedPriceItem</ListingType>", "<ListingDuration>GTC</ListingDuration>",
         f"<PrimaryCategory><CategoryID>{category_id}</CategoryID></PrimaryCategory>",
         "<CategoryMappingAllowed>true</CategoryMappingAllowed>",
         f'<StartPrice currencyID="USD">{float(row["price"]):.2f}</StartPrice>',
         f"<Quantity>{int(row.get('quantity') or 1)}</Quantity>", "<Currency>USD</Currency>", "<Site>US</Site>",
         f"<ConditionID>{int(row.get('condition_id') or 3000)}</ConditionID>"]
    if row.get("condition_note"):
        x.append(f"<ConditionDescription>{escape(row['condition_note'][:1000])}</ConditionDescription>")
    for tag in ("Country", "Location", "PostalCode", "DispatchTimeMax"):
        v = tmpl.findtext(f"e:{tag}", "", NS)
        if v:
            x.append(f"<{tag}>{escape(v)}</{tag}>")
    x.append("<PictureDetails>" + "".join(f"<PictureURL>{escape(p)}</PictureURL>" for p in pics[:24]) + "</PictureDetails>")
    specs = row.get("specifics") or {}
    if specs:
        x.append("<ItemSpecifics>" + "".join(
            f"<NameValueList><Name>{escape(n)}</Name>" + "".join(f"<Value>{escape(str(v))}</Value>" for v in (vals if isinstance(vals, list) else [vals])) + "</NameValueList>"
            for n, vals in specs.items()) + "</ItemSpecifics>")
    profiles = find("e:SellerProfiles")
    if profiles is not None and profiles.find("e:SellerShippingProfile/e:ShippingProfileID", NS) is not None:
        p = "<SellerProfiles>"
        for kind, idtag in (("SellerShippingProfile", "ShippingProfileID"), ("SellerReturnProfile", "ReturnProfileID"),
                            ("SellerPaymentProfile", "PaymentProfileID")):
            pid = profiles.findtext(f"e:{kind}/e:{idtag}", "", NS)
            if pid:
                p += f"<{kind}><{idtag}>{pid}</{idtag}></{kind}>"
        x.append(p + "</SellerProfiles>")
    else:
        ship, ret = find("e:ShippingDetails"), find("e:ReturnPolicy")
        if ship is not None:
            x.append(relist.ser(ship, drop=relist.SHIPPING_DROP))
        if ret is not None:
            x.append(relist.ser(ret, keep=relist.RETURN_KEEP))
        pkg = find("e:ShippingPackageDetails")
        if pkg is not None:
            x.append(relist.ser(pkg))
    if row.get("best_offer", True):
        x.append("<BestOfferDetails><BestOfferEnabled>true</BestOfferEnabled></BestOfferDetails>")
    store = row.get("store_category_id")
    if store:
        x.append(f"<Storefront><StoreCategoryID>{store}</StoreCategoryID></Storefront>")
    return "<Item>" + "".join(x) + "</Item>"


def list_items():
    batch = json.loads(load("batch.json") or "{}")
    index = json.loads(load("index.json") or "{}").get("photos", {})
    log = relist.read_csv(load("log.csv"))
    done = {r["id"] for r in log if r.get("status") == "listed"}
    rows = [r for r in batch.get("items", []) if r.get("approve") and r["id"] not in done][:MAX_PER_RUN]
    print(f"{len(rows)} approved item(s) to do{' (PRACTICE ONLY: nothing will be listed)' if DRY_RUN else ''}.")
    if not rows:
        return
    album = Album(os.environ["ALBUM_URL"])
    _, photos = album.photos()
    by_guid = {p["guid"]: p for p in photos}
    tmpl = template_item(batch)
    ads = Marketing.connect()
    if ads:
        print(f"Ad campaign: {ads.campaign_label()}")
    results, new_log = [], []
    now = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    for row in rows:
        rate = float(row.get("ad_rate") or batch.get("ad_rate") or 0)
        res = {"date": now(), "id": row["id"], "title": row["title"], "price": row["price"], "ad_rate": rate,
               "campaign": ads.campaign_name if ads and rate else ""}
        print(f"\n== {row['id']}: {row['title']}")
        try:
            if len(row["title"]) > 80:
                raise RuntimeError(f"title is {len(row['title'])} characters (max 80)")
            if not 0 <= rate <= relist.MAX_AD_RATE:
                raise RuntimeError(f"ad rate {rate}% looks wrong")
            picks = [p if isinstance(p, dict) else {"n": p} for p in row["photos"]]
            guids = []
            for p in picks:
                info = index.get(str(p["n"]))
                if not info or info["guid"] not in by_guid:
                    raise RuntimeError(f"photo {p['n']} is no longer in the album")
                guids.append(info["guid"])
            urls = album.urls(guids)
            pics = []
            for p, g in zip(picks, guids):
                im = open_image(fetch(urls[by_guid[g]["big"]]), int(p.get("rotate") or 0))
                im.thumbnail((2000, 2000))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=90)
                pics.append(upload_picture(buf.getvalue(), f"{row['id']}-{p['n']}"))
            print(f"   {len(pics)} photos uploaded")
            cat = str(row.get("category_id") or "")
            if not cat:
                cat, cname = suggest_category(row["title"])
                print(f"   category: {cname} ({cat})")
            uid = hashlib.md5(f"newlist-{row['id']}".encode()).hexdigest().upper()
            item = build_item(row, pics, cat, tmpl, uid)
            root, _ = trading("VerifyAddFixedPriceItem", item)
            fee = total_fees(root)
            res["fees"] = fee_breakdown(root)
            print(f"   eBay check OK. fee: {res['fees']}")
            if fee > MAX_FEE:
                raise RuntimeError(f"fee ${fee:.2f} is above the max fee ${MAX_FEE:.2f}; not listed")
            if DRY_RUN:
                res["status"] = "checked OK (practice)"
            else:
                root, warns = trading("AddFixedPriceItem", item)
                new_id = root.findtext("e:ItemID", "", NS)
                res.update(status="listed", item_id=new_id, link=f"https://www.ebay.com/itm/{new_id}")
                print(f"   LISTED: {res['link']}")
                if ads and rate:
                    try:
                        ads.promote(new_id, rate)
                        print(f"   promoted at {rate}% in {ads.campaign_name}")
                    except RuntimeError as e:
                        res["message"] = f"listed, but the ad failed: {e}"
                        print(f"   {res['message']}")
                new_log.append(res)
        except (RuntimeError, EbayError, KeyError, ValueError) as e:
            res.update(status="problem", message=str(e)[:500])
            print(f"   PROBLEM: {e}")
        results.append(res)
    files = {"results.csv": relist.to_csv(results, RESULT_COLS)}
    if new_log:
        files["log.csv"] = relist.to_csv(log + new_log, RESULT_COLS)
    save_files(files, f"New listings: {len(results)} item(s){' practice' if DRY_RUN else ''}")
    ok = sum(1 for r in results if r["status"] != "problem")
    print(f"\nDone: {ok} OK, {len(results) - ok} with problems. See results.csv on the {BRANCH} branch.")


if __name__ == "__main__":
    if not os.environ.get("ALBUM_URL"):
        sys.exit("Add the album's Public Website link as the ALBUM_URL secret first.")
    sheets() if MODE == "sheets" else list_items()
