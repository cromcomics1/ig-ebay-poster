"""
eBay -> Instagram auto-poster for cromcomicscollectibles (seller: cromcomics1).

Runs 3x a day (see post.yml). Each run makes one post and alternates:
  - even turns: a NEW listing, posted as a REEL (slideshow video)
  - odd turns:  an OLDER listing from the backlog, posted as a photo CAROUSEL
If a Reel is ever rejected, the same item is posted as a carousel instead.

Captions: cleaned-up headline, a category hook, condition, eBay link, and
targeted hashtags. Never includes price or location.

Set DRY_RUN=true to preview (it still builds the Reel video to test it).
"""

import base64
import hashlib
import io
import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------- settings you can change ----------------
SELLER = os.environ.get("EBAY_SELLER", "cromcomics1")  # eBay username, not the store name
STORE_NAME = "cromcomicscollectibles"
POSTS_PER_RUN = int(os.environ.get("POSTS_PER_RUN", "1"))
MAX_IMAGES = 5                 # photos per post / slides per Reel
CANVAS = (1080, 1350)          # carousel photos: 4:5 portrait
REEL = (1080, 1920)            # Reels: 9:16
SLIDE_SECONDS = 2.6            # time each photo shows in a Reel
FADE_SECONDS = 0.4
BACKGROUND = (255, 255, 255)

CATEGORIES = [
    "1", "220", "281", "20081", "267", "11116", "237", "64482", "260", "870",
    "550", "11232", "11233", "1249", "11700", "11450", "14339", "619", "625",
    "293", "58058", "15032", "888", "26395", "2984", "1281", "12576", "6000",
    "172008", "1305", "3252", "316", "99",
]
KEYWORDS = ["comic", "hot toys", "figure", "pin", "jewelry", "vintage", "antique", "lot"]

# Keyword in title -> hashtags. Specific entries first; whole-word matching.
HASHTAGS = {
    # characters & franchises
    "superman": "#superman #dccomics", "batman": "#batman #batmancollector #dccomics",
    "joker": "#joker #dccomics", "wonder woman": "#wonderwoman #dccomics",
    "spider-man": "#spiderman #marvelcomics", "spiderman": "#spiderman #marvelcomics",
    "x-men": "#xmen #marvelcomics", "wolverine": "#wolverine #xmen",
    "hulk": "#hulk #marvelcomics", "avengers": "#avengers #marvelcomics",
    "iron man": "#ironman #marvel", "captain america": "#captainamerica #marvel",
    "marvel": "#marvel #marvelcollector", "star wars": "#starwars #starwarscollector",
    "star trek": "#startrek #startrekcollector", "transformers": "#transformers #g1transformers",
    "he-man": "#heman #motu #mastersoftheuniverse", "g.i. joe": "#gijoe #gijoecollector",
    "gi joe": "#gijoe #gijoecollector", "pokemon": "#pokemon #pokemontcg #pokemoncards",
    "disney": "#disney #disneycollector #vintagedisney", "barbie": "#barbie #vintagebarbie",
    # grading & eras
    "cgc": "#cgc #cgccomics #cgcgraded", "cbcs": "#cbcs #cbcsgraded", "psa": "#psa #psagraded",
    "bgs": "#bgs #beckett", "graded": "#graded #slabbed",
    "1st appearance": "#firstappearance #keyissue", "first appearance": "#firstappearance #keyissue",
    "key": "#keycomics #keyissue", "silver age": "#silverage #silveragecomics",
    "bronze age": "#bronzeage #bronzeagecomics", "golden age": "#goldenage #goldenagecomics",
    # categories
    "comic": "#comicbooks #comics #comiccollector #comicbookcollection",
    "hot toys": "#hottoys #sixthscale #onesixthscale #hottoyscollector",
    "kenner": "#kenner #vintagekenner", "mego": "#mego #megotoys",
    "hot wheels": "#hotwheels #diecast", "lego": "#lego #legocollector",
    "funko": "#funko #funkopop", "figure": "#actionfigures #figurecollector",
    "toy": "#vintagetoys #toycollector #retrotoys",
    "pin": "#pins #pincollector #enamelpins #pintrading", "button": "#vintagebuttons #pinbacks",
    "topps": "#topps #toppscards", "panini": "#panini #paninicards",
    "baseball": "#baseballcards #baseball", "football": "#footballcards",
    "basketball": "#basketballcards", "card": "#tradingcards #cardcollector #thehobby",
    "golden book": "#littlegoldenbooks #vintagechildrensbooks",
    "book": "#vintagebooks #bookcollector #oldbooks", "magazine": "#vintagemagazines",
    "vinyl": "#vinylrecords #vinylcollector", "record": "#vinylrecords #recordcollector",
    "coin": "#coins #coincollecting #numismatics",
    # jewelry
    "lagos": "#lagos #lagosjewelry", "sterling": "#sterlingsilver #925silver",
    "14k": "#14kgold #goldjewelry", "10k": "#10kgold #goldjewelry", "18k": "#18kgold #finejewelry",
    "gold": "#goldjewelry #vintagegold", "silver": "#sterlingsilver #vintagesilver",
    "diamond": "#diamonds #diamondjewelry", "turquoise": "#turquoise #turquoisejewelry",
    "navajo": "#navajojewelry #nativeamericanjewelry", "brooch": "#vintagebrooch #brooches",
    "bracelet": "#vintagebracelet", "earring": "#vintageearrings", "necklace": "#vintagenecklace",
    "ring": "#vintagerings #ringsofinstagram", "watch": "#vintagewatch #watchcollector",
    "jewelry": "#vintagejewelry #estatejewelry #jewelrycollector",
    # general
    "antique": "#antiques #antiquecollector", "sign": "#vintagesigns #advertisingcollectibles",
    "poster": "#vintageposters", "vintage": "#vintage #vintagefinds",
}
DEFAULT_TAGS = "#collectibles #collector #ebayfinds #ebayseller #cromcomics"
MAX_TAGS = 20

# One hook line is picked per post (varies by item, stays consistent on reruns).
HOOKS = {
    "graded": ["Slabbed and ready for your collection.", "Graded, sealed and display-ready.",
               "A graded piece for the serious collector."],
    "comic": ["A great addition to any long box.", "Bag it, board it, own it.",
              "Comic collectors, this one's for you."],
    "hot toys": ["Sixth-scale display piece ready for your shelf.",
                 "For the Hot Toys collector who wants the full lineup."],
    "figure": ["Ready for your display shelf.", "A nice piece for any figure collection."],
    "toy": ["Straight out of childhood.", "Retro toy goodness, ready for a new home."],
    "pin": ["Pin traders, add this to your board.", "A fun one for the pin collection."],
    "jewelry": ["Vintage character you won't find at the mall.",
                "Estate piece with real charm.", "A timeless piece to wear or gift."],
    "card": ["A solid pickup for your binder.", "Card collectors, check this one out."],
    "book": ["A nostalgic read for collectors.", "Vintage pages with plenty of charm."],
    "coin": ["A great piece for any coin collection."],
    "antique": ["A true piece of history.", "Old-school craftsmanship you can hold."],
    "default": ["A great find for collectors.", "Fresh to the store and ready to ship.",
                "One of a kind. When it's gone, it's gone."],
}
HOOK_KEYS = [  # which hook group a title belongs to, checked in order
    ("graded", ["cgc", "cbcs", "psa", "bgs", "graded", "slab"]),
    ("hot toys", ["hot toys", "sideshow"]),
    ("comic", ["comic"]),
    ("card", ["card", "topps", "panini"]),
    ("pin", ["pin", "pins"]),
    ("jewelry", ["jewelry", "ring", "necklace", "bracelet", "brooch", "earring",
                 "sterling", "14k", "10k", "18k", "lagos"]),
    ("coin", ["coin"]),
    ("book", ["book", "magazine"]),
    ("figure", ["figure", "kenner", "mego"]),
    ("toy", ["toy", "lego", "hot wheels", "funko"]),
    ("antique", ["antique"]),
]
# Words that clutter eBay titles but read badly on Instagram.
JUNK = r"(l@@k|look!*|wow!*|htf|vhtf|nr|no reserve|must see|free ship(?:ping)?|fast ship(?:ping)?)"
# ----------------------------------------------------------

STATE = Path("posted.json")
CATALOG = Path("catalog.json")
OLD_IMG_DIR = Path("images")    # used by the first version; cleaned up automatically
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
IG_API = "https://graph.instagram.com/v21.0"
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]


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
            return data if raw else json.loads(data.decode() or "{}")
    except urllib.error.HTTPError as e:
        # never print query strings: they can contain the access token
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}")


# ---------------- eBay ----------------
def ebay_token():
    creds = f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}"
    r = http("POST", "https://api.ebay.com/identity/v1/oauth2/token",
             form={"grant_type": "client_credentials",
                   "scope": "https://api.ebay.com/oauth/api_scope"},
             headers={"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()})
    return r["access_token"]


def ebay_headers(token):
    return {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}


def slim(it):
    keep = ("itemId", "legacyItemId", "title", "condition", "image",
            "additionalImages", "itemCreationDate")
    return {k: it[k] for k in keep if k in it}


def fetch_listings(token, full):
    """full=False: newest page per category (fast). full=True: everything (slow, weekly)."""
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
                if (it.get("seller") or {}).get("username", "").lower() != SELLER.lower():
                    continue  # safety: never keep another seller's listing
                items[it["itemId"]] = slim(it)
            total = r.get("total", 0)
            offset += 200
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
    for img in [item.get("image")] + (item.get("additionalImages") or []):
        if img and img.get("imageUrl"):
            u = re.sub(r"s-l\d+\.\w+", "s-l1600.jpg", img["imageUrl"])  # largest size
            if u not in urls:
                urls.append(u)
    return urls[:MAX_IMAGES]


# ---------------- caption ----------------
def has_word(text, key):
    return re.search(rf"(?<![a-z0-9]){re.escape(key)}s?(?![a-z0-9])", text) is not None


def clean_title(title):
    t = re.sub(rf"(?i)\b{JUNK}(?=\W|$)", " ", title)
    t = re.sub(r"[!*~]{2,}", " ", t)              # !!!, ***, ~~~
    t = re.sub(r"\s+[-|/]\s+(?=[-|/]|$)", " ", t)  # dangling separators
    t = re.sub(r"\s{2,}", " ", t).strip(" -|/,")
    if t.isupper():                               # SHOUTY TITLES -> Title Case,
        t = " ".join(w if re.search(r"\d", w) else w.title() for w in t.split())  # keep MMS692
    return t


def pick(options, seed):
    n = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return options[n % len(options)]


def hook_for(title, seed):
    low = title.lower()
    for group, keys in HOOK_KEYS:
        if any(has_word(low, k) for k in keys):
            return pick(HOOKS[group], seed)
    return pick(HOOKS["default"], seed)


def hashtags_for(title):
    low = title.lower()
    tags = [t for key, t in HASHTAGS.items() if has_word(low, key)]
    words = " ".join(tags + [DEFAULT_TAGS]).split()
    words = list(dict.fromkeys(words))
    # keep the store tags even when the list is long
    specific = [w for w in words if w not in DEFAULT_TAGS.split()]
    return specific[:MAX_TAGS - 5] + DEFAULT_TAGS.split()


def build_caption(item):
    title = item.get("title", "").strip()
    seed = item.get("itemId", title)
    lines = [clean_title(title), "", hook_for(title, seed), ""]
    if item.get("condition"):
        lines += [f"Condition: {item['condition']}", ""]
    lines.append("Shop it on eBay (link in bio)")
    if item.get("legacyItemId"):
        lines.append(f"ebay.com/itm/{item['legacyItemId']}")
    lines += ["", " ".join(hashtags_for(title))]
    return "\n".join(lines)  # no price, no location


# ---------------- images ----------------
def load_font(size):
    from PIL import ImageFont
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def download_photos(item):
    from PIL import Image
    photos = []
    for n, url in enumerate(image_urls(item)):
        try:
            photos.append(Image.open(io.BytesIO(http("GET", url, raw=True))).convert("RGB"))
        except Exception as e:
            print(f"  (skipped photo {n}: {e})")
    return photos


def carousel_image(photo):
    from PIL import Image
    im = photo.copy()
    im.thumbnail(CANVAS, Image.LANCZOS)
    canvas = Image.new("RGB", CANVAS, BACKGROUND)
    canvas.paste(im, ((CANVAS[0] - im.width) // 2, (CANVAS[1] - im.height) // 2))
    return canvas


def wrap(draw, text, font, width, max_lines):
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=font) <= width:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,") + "..."
    return lines


def reel_slide(photo, headline=None, footer=None):
    """9:16 frame: blurred photo background, sharp photo centered, optional text."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    W, H = REEL
    bg = photo.copy()
    scale = max(W / bg.width, H / bg.height) * 1.2   # oversize, blur, then crop: no light edges
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1))
    bg = bg.filter(ImageFilter.GaussianBlur(40))
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    fg = photo.copy()
    fg.thumbnail((W - 80, 1250), Image.LANCZOS)
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2 + 30))
    d = ImageDraw.Draw(bg)
    if headline:
        f = load_font(58)
        y = 150
        for line in wrap(d, headline, f, W - 120, 3):
            d.text((W // 2, y), line, font=f, fill="white", anchor="ma",
                   stroke_width=3, stroke_fill="black")
            y += 72
    if footer:
        f = load_font(46)
        d.text((W // 2, H - 230), footer, font=f, fill="white", anchor="ma",
               stroke_width=3, stroke_fill="black")
    return bg


def end_card():
    from PIL import Image, ImageDraw
    W, H = REEL
    im = Image.new("RGB", REEL, (18, 18, 18))
    d = ImageDraw.Draw(im)
    d.text((W // 2, H // 2 - 120), "Shop now on eBay", font=load_font(84), fill="white", anchor="mm")
    d.text((W // 2, H // 2 + 10), "Link in bio", font=load_font(60), fill=(255, 200, 60), anchor="mm")
    d.text((W // 2, H // 2 + 120), STORE_NAME, font=load_font(44), fill=(200, 200, 200), anchor="mm")
    return im


def make_reel(photos, headline, out_path):
    tmp = Path(tempfile.mkdtemp())
    slides = []
    for i, p in enumerate(photos):
        s = reel_slide(p, headline=headline if i == 0 else None,
                       footer="Link in bio to shop" if i == len(photos) - 1 else None)
        slides.append(s)
    slides.append(end_card())
    files = []
    for i, s in enumerate(slides):
        f = tmp / f"s{i}.png"
        s.save(f)
        files.append(f)

    D, T = SLIDE_SECONDS, FADE_SECONDS
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in files:
        cmd += ["-loop", "1", "-t", str(D), "-framerate", "30", "-i", str(f)]
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]  # silent audio track
    n = len(files)
    parts = [f"[{i}:v]format=yuv420p,setsar=1[v{i}]" for i in range(n)]
    last, length = "v0", D
    for i in range(1, n):
        out = f"x{i}"
        parts.append(f"[{last}][v{i}]xfade=transition=fade:duration={T}:offset={length - T:.2f}[{out}]")
        last, length = out, length + D - T
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]", "-map", f"{n}:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", "30",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-shortest",
            "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True)
    return length


# ---------------- hosting (a throwaway 'media' branch, so the repo never grows) ----------------
def git(*args, input_text=None):
    r = subprocess.run(["git", *args], check=True, text=True, capture_output=True, input=input_text)
    return r.stdout.strip()


def host_media(paths):
    entries = []
    for p in paths:
        sha = git("hash-object", "-w", str(p))
        entries.append(f"100644 blob {sha}\t{p.name}")
    tree = git("mktree", input_text="\n".join(entries) + "\n")
    commit = git("commit-tree", tree, "-m", "media for next post")
    git("push", "-f", "origin", f"{commit}:refs/heads/media")
    urls = [f"https://raw.githubusercontent.com/{REPO}/{commit}/{p.name}" for p in paths]
    for url in urls:  # wait until each file is downloadable
        for _ in range(24):
            try:
                http("GET", url, raw=True)
                break
            except Exception:
                time.sleep(5)
        else:
            raise RuntimeError(f"file never became public: {url}")
    return urls


def commit_and_push(paths, message):
    if paths:
        subprocess.run(["git", "add", "-A", *[str(p) for p in paths]], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", message], check=True)
    subprocess.run(["git", "push", "-q"], check=True)


# ---------------- Instagram ----------------
class IG:
    def __init__(self, token):
        self.token = token
        me = http("GET", f"{IG_API}/me?fields=user_id,username&access_token={token}")
        self.uid = me.get("user_id") or me["id"]
        print(f"  posting as @{me.get('username')}")

    def create(self, params):
        params["access_token"] = self.token
        return http("POST", f"{IG_API}/{self.uid}/media", form=params)["id"]

    def wait(self, cid, tries=36):
        for _ in range(tries):
            status = http("GET", f"{IG_API}/{cid}?fields=status_code&access_token={self.token}").get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram rejected the media ({status})")
            time.sleep(5)
        raise RuntimeError("Instagram took too long to process the media")

    def publish(self, cid):
        return http("POST", f"{IG_API}/{self.uid}/media_publish",
                    form={"creation_id": cid, "access_token": self.token})["id"]

    def post_carousel(self, urls, caption):
        if len(urls) == 1:
            cid = self.create({"image_url": urls[0], "caption": caption})
        else:
            kids = [self.create({"image_url": u, "is_carousel_item": "true"}) for u in urls]
            for k in kids:
                self.wait(k)
            cid = self.create({"media_type": "CAROUSEL", "children": ",".join(kids), "caption": caption})
        self.wait(cid)
        return self.publish(cid)

    def post_reel(self, video_url, caption):
        cid = self.create({"media_type": "REELS", "video_url": video_url,
                           "caption": caption, "share_to_feed": "true"})
        self.wait(cid, tries=72)  # videos take longer (up to 6 min)
        return self.publish(cid)


def refresh_token_weekly(token, state):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if time.gmtime().tm_wday != 6 or state.get("refreshed_on") == today:
        return
    try:
        r = http("GET", "https://graph.instagram.com/refresh_access_token"
                        f"?grant_type=ig_refresh_token&access_token={token}")
    except RuntimeError as e:
        print(f"WARNING: token refresh failed: {e}")
        return
    state["refreshed_on"] = today
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
def post_one(item, as_reel, ig, workdir):
    """Build and publish one post. Returns 'reel', 'carousel' or None (skipped)."""
    caption = build_caption(item)
    print("\n--- next post ---")
    print(f"format: {'REEL' if as_reel else 'CAROUSEL'}")
    print(caption)
    photos = download_photos(item)
    print(f"photos: {len(photos)}")
    if not photos:
        return None

    safe = re.sub(r"[^0-9A-Za-z]", "_", item["itemId"])
    files = []
    for i, p in enumerate(photos):
        f = workdir / f"{safe}_{i}.jpg"
        carousel_image(p).save(f, "JPEG", quality=90)
        files.append(f)
    video = None
    if as_reel:
        video = workdir / f"{safe}.mp4"
        try:
            secs = make_reel(photos, clean_title(item.get("title", "")), video)
            print(f"reel video: {secs:.1f}s, {video.stat().st_size // 1024} KB")
        except Exception as e:
            print(f"  (couldn't build Reel, using carousel: {e})")
            video = None

    if DRY_RUN:
        print("\nDRY RUN: nothing was posted.")
        return "reel" if video else "carousel"

    urls = host_media(files + ([video] if video else []))
    photo_urls, video_url = urls[:len(files)], (urls[-1] if video else None)
    if video_url:
        try:
            print(f"  published Reel {ig.post_reel(video_url, caption)}")
            return "reel"
        except RuntimeError as e:
            print(f"  Reel failed ({e}); posting as carousel instead")
    print(f"  published carousel {ig.post_carousel(photo_urls, caption)}")
    return "carousel"


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"posted": [], "skipped": []}
    done = set(state["posted"]) | set(state.get("skipped", []))
    etoken = ebay_token()

    if not DRY_RUN and OLD_IMG_DIR.exists():  # tidy up files from the first version
        subprocess.run(["git", "rm", "-r", "-q", str(OLD_IMG_DIR)], check=True)
        commit_and_push([], "Remove old images folder")

    newest = fetch_listings(etoken, full=False)
    print(f"Newest listings checked: {len(newest)}")

    if CATALOG.exists() and "catalog_at" not in state:
        state["catalog_at"] = time.time()
    stale = time.time() - state.get("catalog_at", 0) > 7 * 86400
    if (not CATALOG.exists() or stale or os.environ.get("FULL_SCAN", "") == "true") and not DRY_RUN:
        print("Running full catalog scan (can take 15+ minutes)...")
        catalog = fetch_listings(etoken, full=True)
        CATALOG.write_text(json.dumps(catalog))
        state["catalog_at"] = time.time()
        STATE.write_text(json.dumps(state, indent=1))
        commit_and_push([CATALOG, STATE], "Refresh catalog")
    else:
        catalog = json.loads(CATALOG.read_text()) if CATALOG.exists() else []
    print(f"Full catalog: {len(catalog)} listings")

    fresh_ids = {i["itemId"] for i in newest}
    fresh = [i for i in newest if i["itemId"] not in done]
    backlog = [i for i in catalog if i["itemId"] not in done and i["itemId"] not in fresh_ids]
    random.Random(len(state["posted"])).shuffle(backlog)  # variety across the whole store
    print(f"Not yet posted: {len(fresh)} new, {len(backlog)} older")

    ig = None if DRY_RUN else IG(os.environ["IG_ACCESS_TOKEN"])
    workdir = Path(tempfile.mkdtemp())
    posted_now = 0
    while posted_now < POSTS_PER_RUN and (fresh or backlog):
        turn = state.get("turn", 0)
        want_backlog = turn % 2 == 1  # alternate: new (Reel) / older (carousel)
        sources = [backlog, fresh] if want_backlog else [fresh, backlog]
        item = None
        for src in sources:
            while src and item is None:
                cand = src.pop(0)
                if src is backlog and not still_for_sale(etoken, cand["itemId"]):
                    print(f"  skipping (sold or ended): {cand.get('title')}")
                    continue
                item = cand
            if item:
                break
        if item is None:
            break
        print(f"\nSlot: {'older item' if want_backlog else 'new listing'}")
        result = post_one(item, as_reel=not want_backlog, ig=ig, workdir=workdir)
        if DRY_RUN:
            posted_now += 1
            state["turn"] = turn + 1   # preview the next slot too if POSTS_PER_RUN > 1
            continue
        if result is None:
            print("  no usable photos, skipping this listing")
            state.setdefault("skipped", []).append(item["itemId"])
            continue
        state["posted"].append(item["itemId"])
        state["turn"] = turn + 1
        STATE.write_text(json.dumps(state, indent=1))
        commit_and_push([STATE], f"Posted {item.get('legacyItemId', item['itemId'])} ({result})")
        posted_now += 1

    if not DRY_RUN:
        refresh_token_weekly(os.environ["IG_ACCESS_TOKEN"], state)
        STATE.write_text(json.dumps(state, indent=1))
        commit_and_push([STATE], "Update state")


if __name__ == "__main__":
    main()
