"""
Make a batch of TikTok-ready videos on demand (Actions > TikTok batch).

Picks listings that haven't been on TikTok yet (newest first, then a random mix
from the whole store), builds a short vertical video for each from its photos,
and saves video + caption to the 'tiktok' branch. Nothing is posted anywhere:
Claude (or you) posts them to TikTok from there. Never shows price or location.
"""
import json
import os
import random
import re
import tempfile
from pathlib import Path

import post

COUNT = max(1, min(int(os.environ.get("COUNT") or 10), 25))
# Optional: specific eBay item numbers (comma/space separated). When given, only
# these are made, even if they were on TikTok before.
ITEMS = re.findall(r"\d{9,15}", os.environ.get("ITEMS") or "")


def fetch_item(token, legacy_id):
    url = ("https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id="
           + legacy_id)
    try:
        return post.slim(post.http("GET", url, headers=post.ebay_headers(token)))
    except RuntimeError as e:
        print(f"  (couldn't find item {legacy_id} on eBay: {e})")
        return None


def main():
    global COUNT
    token = post.ebay_token()
    label = lambda it: re.sub(r"[^0-9A-Za-z]", "_", it["itemId"])
    if ITEMS:
        fresh = [it for it in (fetch_item(token, i) for i in ITEMS) if it]
        pool = []
        COUNT = len(fresh)
    else:
        done = post.tiktok_saved_ids()
        newest = post.fetch_listings(token, full=False)
        catalog = json.loads(post.CATALOG.read_text()) if post.CATALOG.exists() else []
        fresh = [i for i in newest if label(i) not in done]
        pool = [i for i in catalog if label(i) not in done and i["itemId"] not in {f["itemId"] for f in fresh}]
        random.shuffle(pool)
    workdir = Path(tempfile.mkdtemp())
    made = 0
    for it in fresh + pool:
        if made >= COUNT:
            break
        if it in pool and not post.still_for_sale(token, it["itemId"]):
            continue
        photos = post.download_photos(it)
        if len(photos) < 1:
            continue
        title = it.get("title", "")
        video = workdir / f"{label(it)}.mp4"
        try:
            post.make_reel(photos, post.clean_title(title), video)
        except Exception as e:
            print(f"  (couldn't build video for {title}: {e})")
            continue
        ebay = f"ebay.com/itm/{it['legacyItemId']}" if it.get("legacyItemId") else ""
        post.save_for_tiktok(video, post.tiktok_caption(title, ebay), label(it))
        made += 1
        print(f"{made}. {title}")
    print(f"\nMade {made} TikTok videos. See the 'tiktok' branch.")


if __name__ == "__main__":
    main()
