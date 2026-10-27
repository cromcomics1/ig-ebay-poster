"""
Look up live listings in the store by keyword (Actions > Look up listings).

Searches the store with eBay's Browse API and saves title, price, shipping cost,
Best Offer and link for every match to lookup.csv on the 'lookup-results' branch.
Read-only: nothing on eBay changes.
"""
import csv
import io
import os
import subprocess
import urllib.parse

import post

BRANCH = "lookup-results"


def search(token, q):
    out, offset = [], 0
    while True:
        params = {"q": q, "filter": f"sellers:{{{post.SELLER}}}", "limit": "200", "offset": str(offset)}
        r = post.http("GET", "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params),
                      headers=post.ebay_headers(token))
        page = r.get("itemSummaries", []) or []
        out += [it for it in page if (it.get("seller") or {}).get("username", "").lower() == post.SELLER.lower()]
        offset += 200
        if not page or offset >= min(r.get("total", 0), 10000):
            return out


def money(m):
    return f"{float(m['value']):.2f}" if m and m.get("value") is not None else ""


def main():
    token = post.ebay_token()
    queries = [q.strip() for q in os.environ.get("QUERIES", "").split(",") if q.strip()]
    rows = {}
    for q in queries:
        for it in search(token, q):
            ship = (it.get("shippingOptions") or [{}])[0]
            rows[it["itemId"]] = {
                "query": q, "item_id": it.get("legacyItemId", ""), "title": it.get("title", ""),
                "price": money(it.get("price")),
                "shipping": money(ship.get("shippingCost")) if ship.get("shippingCost") else "",
                "shipping_type": ship.get("shippingCostType", ""),
                "best_offer": "yes" if "BEST_OFFER" in (it.get("buyingOptions") or []) else "no",
                "link": it.get("itemWebUrl", "").split("?")[0],
            }
        print(f"{q}: {sum(1 for r in rows.values() if r['query'] == q)} listings")
    buf = io.StringIO()
    cols = ["query", "item_id", "title", "price", "shipping", "shipping_type", "best_offer", "link"]
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    w.writerows(sorted(rows.values(), key=lambda r: r["title"]))
    subprocess.run(["git", "checkout", "-q", "--orphan", BRANCH], check=True)
    subprocess.run(["git", "rm", "-rq", "--cached", "."], check=True)
    with open("lookup.csv", "w") as f:
        f.write(buf.getvalue())
    subprocess.run(["git", "add", "lookup.csv"], check=True)
    subprocess.run(["git", "commit", "-qm", f"Lookup: {', '.join(queries)}"], check=True)
    subprocess.run(["git", "push", "-qf", "origin", BRANCH], check=True)
    print(f"Saved {len(rows)} listings to lookup.csv on the '{BRANCH}' branch.")


if __name__ == "__main__":
    main()
