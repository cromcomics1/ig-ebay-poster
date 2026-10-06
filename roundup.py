
"""
Roundup Reel for cromcomicscollectibles, posted every 2 days.

Builds one 20-30 second video: an intro card, one slide per item (photo +
title), and a shop-now end card. It features new listings first, then fills
the rest with other items from the store. Items are never repeated: featured
item numbers are remembered in roundup_state.json. Never shows price or location.

DRY_RUN=true builds the video and saves it as roundup.mp4 for preview
(the workflow attaches it to the run) without posting.
"""

import json
import os
import random
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import post  # reuse eBay access, slide drawing, hosting and Instagram posting

MAX_ITEMS = int(os.environ.get("ROUNDUP_ITEMS", "8"))
MIN_ITEMS = 3
SLIDE_SECONDS = 2.8
FADE_SECONDS = 0.4
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
OUT = Path("roundup.mp4")
STATE = Path("roundup_state.json")
NEW_DAYS = 14  # a listing counts as "new" for this long


def group_of(title):
    """Rough category, used only to mix different kinds of items in the video."""
    low = title.lower()
    for group, keys in post.HOOK_KEYS:
        if any(post.has_word(low, k) for k in keys):
            return group
    return "other"


def mix(items, limit):
    """Take items one category at a time, so the video isn't all one kind."""
    buckets = {}
    for it in items:
        buckets.setdefault(group_of(it.get("title", "")), []).append(it)
    picked = []
    while len(picked) < limit and any(buckets.values()):
        for g in list(buckets):
            if buckets[g] and len(picked) < limit:
                picked.append(buckets[g].pop(0))
    return picked


def pick_items(listings, catalog, featured, token):
    """New, not-yet-featured listings first; then other store items to fill the video.
    Returns (items, how many of them are new)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NEW_DAYS)).isoformat()
    fresh = [i for i in listings if i.get("image") and i["itemId"] not in featured
             and i.get("itemCreationDate", "") >= cutoff]
    picked = mix(fresh[:80], MAX_ITEMS)
    new_count = len(picked)
    if len(picked) < MAX_ITEMS:
        have = {i["itemId"] for i in picked}
        pool = [i for i in catalog if i.get("image") and i["itemId"] not in featured and i["itemId"] not in have]
        random.Random(datetime.now(timezone.utc).strftime("%Y-%m-%d")).shuffle(pool)
        for it in mix(pool[:200], 60):
            if len(picked) >= MAX_ITEMS:
                break
            if post.still_for_sale(token, it["itemId"]):  # the saved catalog can be a few days old
                picked.append(it)
    return picked, new_count


def intro_card(count, label):
    from PIL import Image, ImageDraw
    W, H = post.REEL
    im = Image.new("RGB", post.REEL, (18, 18, 18))
    d = ImageDraw.Draw(im)
    d.text((W // 2, H // 2 - 150), label, font=post.load_font(104), fill=(255, 200, 60), anchor="mm")
    sub = f"{count} fresh finds" if label == "NEW ARRIVALS" else f"{count} finds from the store"
    d.text((W // 2, H // 2 - 20), sub, font=post.load_font(64), fill="white", anchor="mm")
    d.text((W // 2, H // 2 + 90), post.STORE_NAME, font=post.load_font(44), fill=(200, 200, 200), anchor="mm")
    return im


def build_video(slides, out_path):
    tmp = Path(tempfile.mkdtemp())
    files = []
    for i, s in enumerate(slides):
        f = tmp / f"s{i}.png"
        s.save(f)
        files.append(f)
    D, T = SLIDE_SECONDS, FADE_SECONDS
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for f in files:
        cmd += ["-loop", "1", "-t", str(D), "-framerate", "30", "-i", str(f)]
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    n = len(files)
    parts = [f"[{i}:v]format=yuv420p,setsar=1[v{i}]" for i in range(n)]
    last, length = "v0", D
    for i in range(1, n):
        parts.append(f"[{last}][v{i}]xfade=transition=fade:duration={T}:offset={length - T:.2f}[x{i}]")
        last, length = f"x{i}", length + D - T
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]", "-map", f"{n}:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", "30",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-shortest",
            "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True)
    return length


def build_caption(items, label):
    head = ("New arrivals at cromcomicscollectibles" if label == "NEW ARRIVALS"
            else "Store picks from cromcomicscollectibles")
    lines = [head, ""]
    for n, it in enumerate(items, 1):
        lines.append(f"{n}. {post.clean_title(it.get('title', ''))}")
    lines += ["", "Shop them all on eBay (link in bio)", ""]
    tags = []
    for it in items:
        tags += [t for t in post.hashtags_for(it.get("title", "")) if t not in post.DEFAULT_TAGS.split()]
    extra = ["#newarrivals"] if label == "NEW ARRIVALS" else ["#storepicks"]
    tags = list(dict.fromkeys(tags))[:14] + extra + post.DEFAULT_TAGS.split()
    lines.append(" ".join(tags))
    return "\n".join(lines)[:2200]


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {"featured": []}
    featured = set(state["featured"])
    token = post.ebay_token()
    listings = post.fetch_listings(token, full=False)
    catalog = json.loads(post.CATALOG.read_text()) if post.CATALOG.exists() else []
    items, new_count = pick_items(listings, catalog, featured, token)
    if len(items) < MIN_ITEMS:
        print("Not enough items that haven't been featured yet, so no roundup this time.")
        return
    label = "NEW ARRIVALS" if new_count >= (len(items) + 1) // 2 else "STORE PICKS"
    print(f"\nRoundup ({label}): {len(items)} items, {new_count} of them new")

    slides, used = [], []
    for it in items:
        photos = post.download_photos({"image": it.get("image")})  # main photo only
        if not photos:
            continue
        n = len(used) + 1
        slides.append(post.reel_slide(photos[0], headline=post.clean_title(it.get("title", "")),
                                      footer=f"{n} of {len(items)}"))
        used.append(it)
    if len(used) < MIN_ITEMS:
        print("Could not download enough photos, skipping this time.")
        return
    slides = [intro_card(len(used), label)] + slides + [post.end_card()]
    secs = build_video(slides, OUT)
    caption = build_caption(used, label)
    print(f"video: {secs:.1f}s, {OUT.stat().st_size // 1024} KB\n")
    print(caption)

    if DRY_RUN:
        print("\nPREVIEW ONLY: nothing was posted. Download roundup.mp4 from this run's Artifacts.")
        return

    url = post.host_media([OUT])[0]
    ig = post.IG(os.environ["IG_ACCESS_TOKEN"])
    try:
        print(f"\npublished roundup Reel {ig.post_reel(url, caption)}")
    except RuntimeError as e:
        raise SystemExit(f"Instagram did not accept the roundup: {e}")
    # remember what was shown, so the next video features different items
    state["featured"] = (state["featured"] + [i["itemId"] for i in used])[-3000:]
    STATE.write_text(json.dumps(state))
    subprocess.run(["git", "pull", "-q", "--rebase"], check=False)
    post.commit_and_push([STATE], "Roundup posted")


if __name__ == "__main__":
    main()
