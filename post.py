"""
eBay -> Instagram auto-poster for cromcomicscollectibles.

Each run:
  1. Pulls active listings from the eBay store (Browse API).
  2. Picks the newest listing not yet posted.
  3. Pads its photos to Instagram's 4:5 shape and publishes them to Instagram
     (single photo or carousel) with an auto-written caption.
  4. Records the item in posted.json so it is never posted twice.

Set DRY_RUN=true to preview the caption without posting anything.
"""

import base64
import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------- settings you can change ----------------
SELLER = os.environ.get("EBAY_SELLER", "cromcomicscollectibles")
POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "1"))
MAX_IMAGES = 5                # photos per post (Instagram allows up to 10)
CANVAS = (1080, 1350)         # 4:5 portrait, Instagram's tallest allowed shape
BACKGROUND = (255, 255, 255)  # white padding around photos

# Every eBay US top-level category, so no listing is missed, plus keyword
# sweeps as a safety net.
CATEGORIES = [
    "1", "220", "281", "20081", "267", "11116", "237", "64482", "260", "870",
    "550", "11232", "11233", "1249", "11700", "11450", "14339", "619", "625",
    "293", "58058", "15032", "888", "26395", "2984", "1281", "12576", "6000",
    "172008", "1305", "3252", "316", "99",
]
KEYWORDS = ["comic", "hot toys", "figure", "pin", "jewelry", "vintage", "antique", "lot"]

HASHTAGS = {
    "comic": "#comicbooks #comics #keyissues #comiccollector #comicbookcollection #cgc",
    "hot toys": "#hottoys #sixthscale #onesixthscale #hottoyscollector #actionfigures",
    "batman": "#batman #batmancollector #dccomics",
    "pin": "#disneypins #pintrading #pincollector #enamelpins #disneypin",
    "star wars": "#starwars #starwarscollector",
    "marvel": "#marvel #marvelcollector",
    "jewelry": "#vintagejewelry #estatejewelry #jewelrycollector #sterlingsilver",
    "silver": "#sterlingsilver #vintagesilver",
    "antique": "#antiques #antique #vintagefinds #oldstuff",
    "coin": "#coins #coincollecting #numismatics",
    "figure": "#actionfigures #toycollector #figurecollector",
    "golden book": "#littlegoldenbooks #goldenbooks #vintagechildrensbooks",
    "book": "#vintagebooks #bookcollector #oldbooks #booksofinstagram",
    "magazine": "#vintagemagazines #magazinecollector",
    "disney": "#disney #disneycollector #vintagedisney",
    "toy": "#vintagetoys #toycollector #retrotoys",
    "lego": "#lego #legocollector",
    "funko": "#funko #funkopop #funkocollector",
    "card": "#tradingcards #cardcollector #thehobby",
    "pokemon": "#pokemon #pokemontcg #pokemoncards",
    "vinyl": "#vinylrecords #vinylcollector #recordcollector",
    "record": "#vinylrecords #recordcollector",
    "watch": "#vintagewatch #watchcollector",
    "ring": "#vintagerings #ringsofinstagram",
    "necklace": "#vintagenecklace #jewelrylover",
    "gold": "#goldjewelry #vintagegold",
    "vintage": "#vintage #vintagestyle",
    "sign": "#vintagesigns #advertisingcollectibles",
    "button": "#vintagebuttons #pinbacks",
    "poster": "#vintageposters #postercollector",
    "star trek": "#startrek #startrekcollector",
    "transformers": "#transformers #g1transformers",
    "he-man": "#heman #motu #mastersoftheuniverse",
    "barbie": "#barbie #vintagebarbie",
}
DEFAULT_TAGS = "#collectibles #collector #ebayfinds #vintagefinds #cromcomics"
# ----------------------------------------------------------

STATE = Path("posted.json")
CATALOG = Path("catalog.json")
IMG_DIR = Path("images")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
IG_API = "https://graph.instagram.com/v21.0"


# ---------------- HTTP helper ----------------
def http(method, url, form=None, json_body=None, headers=None, raw=False):
    headers = dict(headers or {})
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    headers.setdefault("User-Agent", "ig-ebay-poster")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            if raw:
                return data
            return json.loads(data.decode() or "{}")
    except urllib.error.HTTPError as e:
        # never print query strings: they can contain the access token
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}")


# ---------------- eBay ----------------
def ebay_token():
    creds = f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}"
    r = http(
        "POST",
        "https://api.ebay.com/identity/v1/oauth2/token",
        form={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()},
    )
    return r["access_token"]


def ebay_headers(token):
    return {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}


def slim(it):
    """Keep only the fields we need, so the saved catalog stays small."""
    keep = ("itemId", "legacyItemId", "title", "condition", "image",
            "additionalImages", "itemCreationDate")
    return {k: it[k] for k in keep if k in it}


def fetch_listings(token, full):
    """full=False: newest page per category (fast, daily).
    full=True: every page of every category + keyword sweeps (slow, weekly)."""
    headers = ebay_headers(token)
    items = {}
    queries = [{"category_ids": c} for c in CATEGORIES]
    if full:
        queries += [{"q": k} for k in KEYWORDS]
    max_pages = 50 if full else 1
    started = time.time()
    for q in queries:
        offset, total, pages = 0, None, 0
        while pages < max_pages:
            pages += 1
            params = {**q, "filter": f"sellers:{{{SELLER}}}", "sort": "newlyListed",
                      "limit": "200", "offset": str(offset)}
            url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params)
            try:
                r = http("GET", url, headers=headers)
            except RuntimeError as e:
                print(f"  (skipped search {q} at {offset}: {e})")
                break
            page = r.get("itemSummaries", [])
            for it in page:
                items[it["itemId"]] = slim(it)
            total = r.get("total", 0)
            offset += 200
            # eBay returns at most 10,000 results per search
            if not page or offset >= min(total, 10000):
                break
        if total:
            print(f"  {q}: {total} listings ({int(time.time() - started)}s elapsed)")
    return sorted(items.values(), key=lambda i: i.get("itemCreationDate", ""), reverse=True)


def still_for_sale(token, item_id):
    url = ("https://api.ebay.com/buy/browse/v1/item/"
           + urllib.parse.quote(item_id, safe="") + "?fieldgroups=COMPACT")
    try:
        r = http("GET", url, headers=ebay_headers(token))
    except RuntimeError:
        return False
    for a in r.get("estimatedAvailabilities", []) or []:
        if a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
            return False
    return True


def image_urls(item):
    urls = []
    for img in [item.get("image")] + item.get("additionalImages", []):
        if img and img.get("imageUrl"):
            # ask eBay for the largest version of each photo
            u = re.sub(r"s-l\d+\.\w+", "s-l1600.jpg", img["imageUrl"])
            if u not in urls:
                urls.append(u)
    return urls[:MAX_IMAGES]


# ---------------- caption ----------------
def build_caption(item):
    title = item.get("title", "").strip()
    low = title.lower()
    # whole-word match, so "gold" doesn't fire on "golden"; "pin"/"card"/"book" allow plurals
    tags = [t for key, t in HASHTAGS.items() if re.search(rf"\b{re.escape(key)}s?\b", low)]
    tag_words = " ".join(tags + [DEFAULT_TAGS]).split()
    tag_words = list(dict.fromkeys(tag_words))[:25]  # dedupe, stay under IG's 30 limit

    lines = [title, ""]
    if item.get("condition"):
        lines.append(f"Condition: {item['condition']}")
        lines.append("")
    lines.append("Shop it now on eBay (link in bio)")
    legacy = item.get("legacyItemId")
    if legacy:
        lines.append(f"ebay.com/itm/{legacy}")
    lines += ["", " ".join(tag_words)]
    return "\n".join(lines)  # no price, no location


# ---------------- images ----------------
def pad_image(data):
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail(CANVAS, Image.LANCZOS)
    canvas = Image.new("RGB", CANVAS, BACKGROUND)
    canvas.paste(im, ((CANVAS[0] - im.width) // 2, (CANVAS[1] - im.height) // 2))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90)
    return out.getvalue()


def prepare_images(item):
    IMG_DIR.mkdir(exist_ok=True)
    safe_id = re.sub(r"[^0-9A-Za-z]", "_", item["itemId"])
    paths = []
    for n, url in enumerate(image_urls(item)):
        try:
            data = pad_image(http("GET", url, raw=True))
        except Exception as e:  # skip a bad photo, keep the rest
            print(f"  (skipped photo {n}: {e})")
            continue
        p = IMG_DIR / f"{safe_id}_{n}.jpg"
        p.write_bytes(data)
        paths.append(p)
    return paths


def public_url(path):
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path.as_posix()}"


def wait_until_public(urls):
    for url in urls:
        for _ in range(24):
            try:
                http("GET", url, raw=True)
                break
            except Exception:
                time.sleep(5)
        else:
            raise RuntimeError(f"image never became public: {url}")


# ---------------- git ----------------
def git(*args):
    subprocess.run(["git", *args], check=True)


def commit_and_push(paths, message):
    git("add", *[str(p) for p in paths])
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    git("commit", "-m", message)
    git("push")


# ---------------- Instagram ----------------
def ig_publish(urls, caption, token):
    me = http("GET", f"{IG_API}/me?fields=user_id,username&access_token={token}")
    uid = me.get("user_id") or me["id"]
    print(f"  posting as @{me.get('username')}")

    def create(params):
        params["access_token"] = token
        return http("POST", f"{IG_API}/{uid}/media", form=params)["id"]

    def wait(cid):
        for _ in range(36):
            status = http("GET", f"{IG_API}/{cid}?fields=status_code&access_token={token}").get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram rejected media container ({status})")
            time.sleep(5)
        raise RuntimeError("Instagram took too long to process the photos")

    if len(urls) == 1:
        cid = create({"image_url": urls[0], "caption": caption})
    else:
        children = [create({"image_url": u, "is_carousel_item": "true"}) for u in urls]
        for c in children:
            wait(c)
        cid = create({"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})
    wait(cid)
    return http("POST", f"{IG_API}/{uid}/media_publish",
                form={"creation_id": cid, "access_token": token})["id"]


def refresh_token_weekly(token):
    """Instagram tokens last 60 days; refresh every Sunday so it never lapses."""
    if time.gmtime().tm_wday != 6:
        return
    try:
        r = http("GET", "https://graph.instagram.com/refresh_access_token"
                        f"?grant_type=ig_refresh_token&access_token={token}")
    except RuntimeError as e:
        print(f"WARNING: token refresh failed: {e}")
        return
    print(f"Token refreshed, valid for about {r.get('expires_in', 0) // 86400} days")
    new = r.get("access_token")
    if new and new != token:
        save_new_token(new)


def save_new_token(value):
    pat = os.environ.get("GH_PAT")
    if not pat:
        print("WARNING: Instagram issued a new token but GH_PAT is not set. "
              "Generate a new token in the Meta dashboard and update IG_ACCESS_TOKEN.")
        return
    from nacl import encoding, public
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    key = http("GET", f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=h)
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
    http("PUT", f"https://api.github.com/repos/{REPO}/actions/secrets/IG_ACCESS_TOKEN",
         json_body={"encrypted_value": encrypted, "key_id": key["key_id"]}, headers=h)
    print("Saved the refreshed token to IG_ACCESS_TOKEN")


# ---------------- main ----------------
def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"posted": [], "skipped": []}
    done = set(state["posted"]) | set(state.get("skipped", []))
    token = os.environ["IG_ACCESS_TOKEN"]
    etoken = ebay_token()

    # Daily: newest page of each category (about a minute).
    newest = fetch_listings(etoken, full=False)
    print(f"Newest listings checked: {len(newest)}")

    # Weekly (Sundays), on first run, or on request: full catalog scan, saved to catalog.json.
    need_full = (not CATALOG.exists() or time.gmtime().tm_wday == 6
                 or os.environ.get("FULL_SCAN", "false").lower() == "true")
    if need_full and not DRY_RUN:
        print("Running full catalog scan (can take 15+ minutes)...")
        catalog = fetch_listings(etoken, full=True)
        CATALOG.write_text(json.dumps(catalog))
        commit_and_push([CATALOG], "Refresh catalog")
    else:
        catalog = json.loads(CATALOG.read_text()) if CATALOG.exists() else []
    print(f"Full catalog: {len(catalog)} listings for {SELLER}")

    fresh_ids = {i["itemId"] for i in newest}
    queue = [i for i in newest if i["itemId"] not in done]
    queue += [i for i in catalog if i["itemId"] not in done and i["itemId"] not in fresh_ids]
    print(f"{len(queue)} not yet posted")
    if not queue:
        return

    posted_now = 0
    for item in queue:
        if posted_now >= POSTS_PER_RUN:
            break
        # older items come from the saved catalog, so confirm they haven't sold
        if item["itemId"] not in fresh_ids and not still_for_sale(etoken, item["itemId"]):
            print(f"  skipping (no longer for sale): {item.get('title')}")
            continue
        caption = build_caption(item)
        print("\n--- next post ---")
        print(caption)
        print(f"photos: {len(image_urls(item))}")

        if DRY_RUN:
            print("\nDRY RUN: nothing was posted.")
            posted_now += 1
            continue

        paths = prepare_images(item)
        if not paths:
            print("  no usable photos, skipping this listing")
            state.setdefault("skipped", []).append(item["itemId"])
            continue
        commit_and_push(paths, f"Images for {item.get('legacyItemId', item['itemId'])}")
        urls = [public_url(p) for p in paths]
        wait_until_public(urls)

        media_id = ig_publish(urls, caption, token)
        print(f"  published Instagram post {media_id}")
        state["posted"].append(item["itemId"])
        STATE.write_text(json.dumps(state, indent=1))
        commit_and_push([STATE], f"Posted {item.get('legacyItemId', item['itemId'])}")
        posted_now += 1

    STATE.write_text(json.dumps(state, indent=1))
    if not DRY_RUN:
        commit_and_push([STATE], "Update posted list")
        refresh_token_weekly(token)


if __name__ == "__main__":
    main()
