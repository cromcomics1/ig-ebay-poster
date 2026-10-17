"""Build the Card Climbers page: python3 cards_page.py cards/ out.html [web.json]
web.json = [["note text", "Source name", "https://..."], ...]"""
import json, base64, html, sys
from pathlib import Path
from datetime import datetime
src = Path(sys.argv[1]); out = Path(sys.argv[2])
r = json.loads((src / "report.json").read_text())
e = html.escape
def img(row):
    t = row.get("thumb")
    if t and (src / t).exists():
        b = base64.b64encode((src / t).read_bytes()).decode()
        return f'<img src="data:image/jpeg;base64,{b}" alt="{e(row.get("title",""))}" loading="lazy">'
    return '<div class="noimg">No photo</div>'
def money(v): return "—" if v is None else f"${v:,.2f}"
gen = datetime.fromisoformat(r["generated"].rstrip("Z"))
thr = int(r.get("climb_threshold_pct") or 30)
sets = {s["set"]: s for s in r["sets_tracked"]}

def climbers_html():
    rows = r["top_card_climbers"]
    if not rows:
        msg = ("No earlier week to compare yet. The first climbers list appears on Sunday, October 25."
               if not r["compared_with"] else f"No card rose {thr}% or more this week.")
        return f'<p class="empty">{msg}</p>'
    out = ['<ol class="climbers">']
    for c in rows:
        out.append(f'<li><a href="{e(c["url"])}" target="_blank" rel="noopener">{img(c)}</a>'
                   f'<div class="cmeta"><b>{e(c["card"])}</b><span class="sub">{e(c["title"])}</span></div>'
                   f'<div class="cnum"><span class="up">+{c["change_pct"]:.0f}%</span>'
                   f'<span class="sub">{money(c["was"])} → {money(c["median"])}</span></div></li>')
    return "".join(out) + "</ol>"

def setclimb_html():
    rows = r["top_set_climbers"]
    if not rows:
        return '<p class="empty">No set has risen 30% or more yet.</p>' if r["compared_with"] else '<p class="empty">Starts on Sunday, October 25.</p>'
    return "<table><thead><tr><th>Set</th><th>Was</th><th>Now</th><th>Change</th></tr></thead><tbody>" + "".join(
        f'<tr><td>{e(s["set"])}</td><td>{money(s["was"])}</td><td>{money(s["median"])}</td><td class="up">+{s["change_pct"]:.0f}%</td></tr>' for s in rows) + "</tbody></table>"

def set_card(name, picks):
    s = sets.get(name, {})
    by_price = all(p.get("change_pct") is None for p in picks)
    cards = "".join(
        f'<a class="pick" href="{e(p["url"])}" target="_blank" rel="noopener">{img(p)}'
        f'<span class="pn">#{e(p["card"].split("#",1)[-1])}</span><span class="pt">{e(p.get("title",""))}</span>'
        f'<span class="pp">{money(p["median"])}{"" if p.get("change_pct") is None else f" · +{p["change_pct"]:.0f}%"}</span></a>'
        for p in picks)
    maker = s.get("maker") or ""; setn = s.get("set_name") or name
    return (f'<article class="set"><header><div><span class="mk">{e(maker) or "Maker not listed"}</span><h3>{e(setn)}</h3></div><span class="yr">c. {s.get("year","?")}</span></header>'
            f'<p class="stats"><span>Median ask <b>{money(s.get("median"))}</b></span><span><b>{s.get("listings",0)}</b> listings</span></p>'
            f'<div class="picks">{cards}</div>'
            f'<p class="basis">{"Top 3 by asking price (no climbers yet)" if by_price else "Top 3 climbers"}</p></article>')

groups = [("Pre-war and tobacco", lambda y: (y or 9999) <= 1945), ("Post-war gum and trading cards", lambda y: (y or 0) > 1945)]
sets_html = ""
for label, test in groups:
    names = sorted([n for n in r["top3_per_set"] if test(sets.get(n, {}).get("year"))], key=lambda n: (sets[n].get("year") or 0, n))
    if names:
        sets_html += f'<h3 class="era">{label} <span class="count">{len(names)} sets</span></h3><div class="sets">' + "".join(set_card(n, r["top3_per_set"][n]) for n in names) + "</div>"

pc = r["my_price_check"]
pc_html = ('<p class="empty">None of your listings had at least 3 matching listings (same card and grade) to compare against this week. '
           f'Cards in a set that rises {thr}% or more will show up here as good ones to post.</p>') if not pc else "<table><thead><tr><th>Your listing</th><th>Your price</th><th>Market</th><th>Note</th></tr></thead><tbody>" + "".join(
    f'<tr><td><a href="{e(p["url"])}" target="_blank" rel="noopener">{e(p["title"])}</a></td><td>{money(p["my_price"])}</td><td>{money(p["market_median"])}</td><td>{e(p["flag"])}</td></tr>' for p in pc) + "</tbody></table>"

web = [tuple(w) for w in json.loads(Path(sys.argv[3]).read_text())] if len(sys.argv) > 3 else []
web_html = '<p class="empty">No web notes this week.</p>' if not web else "<ul class='web'>" + "".join(f'<li>{e(t)} <a href="{u}" target="_blank" rel="noopener">{e(s)}</a></li>' for t, s, u in web) + "</ul>"

page = f'''<title>Card Climbers</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Alegreya+SC:wght@500;700&family=Alegreya+Sans:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* Layout: a collector's album page — status strip, climbers, then sets grouped by era, each set as a small album sleeve with its three cards. */
:root{{
  --bg:#eef0ea; --page:#f8f9f5; --ink:#1d2a33; --muted:#5d6a70; --rule:#cdd3cb;
  --accent:#8a5a14; --up:#2f6b3a; --upsoft:#e1eee2; --sleeve:#ffffff;
  --f-display:"Alegreya SC", Georgia, serif;
  --f-body:"Alegreya Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --f-mono:"JetBrains Mono", ui-monospace, Menlo, monospace;
}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{
  --bg:#13181b; --page:#1a2024; --ink:#e3e8e6; --muted:#97a3a6; --rule:#2e383c;
  --accent:#d9a65a; --up:#7fc48b; --upsoft:#1d3022; --sleeve:#20282c; color-scheme:dark}}}}
:root[data-theme="dark"]{{
  --bg:#13181b; --page:#1a2024; --ink:#e3e8e6; --muted:#97a3a6; --rule:#2e383c;
  --accent:#d9a65a; --up:#7fc48b; --upsoft:#1d3022; --sleeve:#20282c; color-scheme:dark}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--ink);font-family:var(--f-body);font-size:16px;line-height:1.5}}
.wrap{{max-width:1080px;margin:0 auto;padding-block:28px 72px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:flex;flex-direction:column;gap:36px}}
h1{{font-family:var(--f-display);font-weight:700;font-size:clamp(2rem,5vw,3rem);line-height:1;margin:0;letter-spacing:.01em;text-wrap:balance}}
h2{{font-family:var(--f-display);font-weight:700;font-size:1.5rem;margin:0 0 4px;text-wrap:balance}}
h3{{margin:0}}
.lede{{color:var(--muted);margin:8px 0 0;max-width:62ch}}
.strip{{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:16px;font-family:var(--f-mono);font-size:.8rem;color:var(--muted)}}
.strip b{{color:var(--ink);font-weight:500}}
.note{{border-left:3px solid var(--accent);padding:10px 14px;background:var(--page);font-size:.95rem;max-width:72ch}}
section{{display:flex;flex-direction:column;gap:12px}}
.sec-sub{{color:var(--muted);margin:0;font-size:.95rem;max-width:68ch}}
.empty{{margin:0;padding:16px;background:var(--page);border:1px dashed var(--rule);border-radius:6px;color:var(--muted)}}
.up{{color:var(--up);font-weight:700;font-variant-numeric:tabular-nums}}
.era{{font-family:var(--f-display);font-size:1.15rem;font-weight:500;color:var(--accent);display:flex;align-items:baseline;gap:10px;margin-top:8px}}
.count{{font-family:var(--f-mono);font-size:.75rem;color:var(--muted)}}
.sets{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}}
.set{{background:var(--page);border:1px solid var(--rule);border-radius:6px;padding:14px;display:flex;flex-direction:column;gap:8px;min-width:0}}
.set header{{display:flex;justify-content:space-between;align-items:baseline;gap:10px}}
.set h3{{font-size:1.05rem;font-weight:700;line-height:1.25;min-width:0}}
.yr{{font-family:var(--f-mono);font-size:.75rem;color:var(--muted);white-space:nowrap}}
.stats{{display:flex;gap:14px;margin:0;font-size:.85rem;color:var(--muted);font-variant-numeric:tabular-nums}}
.stats b{{color:var(--ink);font-weight:500}}
.picks{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}
.pick{{display:flex;flex-direction:column;gap:3px;text-decoration:none;color:inherit;min-width:0}}
.pick img,.pick .noimg,.climbers img,.climbers .noimg{{width:100%;aspect-ratio:3/4;object-fit:contain;background:var(--sleeve);border:1px solid var(--rule);border-radius:3px;max-width:100%}}
.noimg{{display:grid;place-items:center;font-size:.7rem;color:var(--muted)}}
.pick:hover img,.pick:focus-visible img{{border-color:var(--accent)}}
.mk{{display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
.pt{{font-size:.72rem;line-height:1.3;color:var(--muted);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
.pn{{font-family:var(--f-mono);font-size:.72rem}}
.pp{{font-size:.8rem;font-variant-numeric:tabular-nums;color:var(--muted)}}
.basis{{margin:0;font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
.climbers{{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}}
.climbers li{{display:grid;grid-template-columns:64px 1fr auto;gap:12px;align-items:center;background:var(--page);border:1px solid var(--rule);border-radius:6px;padding:8px 12px}}
.cmeta{{display:flex;flex-direction:column;min-width:0}}
.cnum{{display:flex;flex-direction:column;align-items:flex-end;text-align:right}}
.sub{{font-size:.8rem;color:var(--muted);overflow-wrap:anywhere}}
.tbl{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:.9rem;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--rule);vertical-align:top}}
th{{font-weight:500;color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}}
.web{{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:10px;max-width:72ch}}
a{{color:var(--accent)}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
footer{{color:var(--muted);font-size:.85rem;border-top:1px solid var(--rule);padding-top:14px;max-width:72ch}}
</style>
<div class="wrap">
<header>
  <h1>Card Climbers</h1>
  <p class="lede">Weekly price watch for vintage, antique and tobacco cards on eBay, for cromcomicscollectibles. A climber is a card or set whose median asking price rose {thr}% or more.</p>
  <div class="strip"><span>Run <b>{gen:%b %d, %Y}</b></span><span>Sets tracked <b>{len(sets)}</b></span><span>Compared with <b>{r["compared_with"] or "no earlier week yet"}</b></span><span>Threshold <b>+{thr}%</b></span></div>
</header>
{"<p class='note'>This is the first week of data, so there's nothing to compare against yet. Climbers start with the Sunday, October 25 run, the first one a full week after this data. For now, each set shows its three highest-priced cards.</p>" if not r["compared_with"] else ""}
<section><h2>Top climbing cards</h2><p class="sec-sub">Up to 20 cards, each with at least 2 listings in both weeks.</p>{climbers_html()}</section>
<section><h2>Climbing sets</h2><div class="tbl">{setclimb_html()}</div></section>
<section><h2>Your listings to reprice or post</h2>{pc_html}</section>
<section><h2>Sets tracked</h2><p class="sec-sub">Picked automatically from eBay each week: the most-listed sets dated 1979 or earlier. Tobacco and pre-war sets are named from the listing titles, because eBay sellers rarely fill in a set name for them. Tap a card to open the listing.</p>{sets_html}</section>
<section><h2>From the web this week</h2>{web_html}</section>
<footer>Prices are eBay Buy It Now asking prices, not sold prices. Before repricing on a top climber, check its sold prices in Terapeak.</footer>
</div>
'''
out.write_text(page)
print(len(page)//1024, "KB")
