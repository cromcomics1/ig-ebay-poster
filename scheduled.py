"""Read-only: print the store's scheduled (not yet live) listings. Changes nothing on eBay."""
from relist import trading, NS

page, rows = 1, []
while True:
    root, _ = trading("GetMyeBaySelling",
                      "<ScheduledList><Include>true</Include><Pagination><EntriesPerPage>200</EntriesPerPage>"
                      f"<PageNumber>{page}</PageNumber></Pagination></ScheduledList>"
                      "<ActiveList><Include>false</Include></ActiveList>"
                      "<DetailLevel>ReturnAll</DetailLevel>")
    lst = root.find("e:ScheduledList", NS)
    if lst is None:
        break
    for it in lst.findall("e:ItemArray/e:Item", NS):
        rows.append((it.findtext("e:ListingDetails/e:StartTime", "", NS) or it.findtext("e:ScheduleTime", "", NS),
                     it.findtext("e:ItemID", "", NS), it.findtext("e:SKU", "", NS),
                     it.findtext("e:StartPrice", "", NS) or it.findtext("e:BuyItNowPrice", "", NS),
                     it.findtext("e:Title", "", NS)))
    pages = int(lst.findtext("e:PaginationResult/e:TotalNumberOfPages", "1", NS) or 1)
    if page >= pages:
        break
    page += 1
rows.sort()
print(f"SCHEDULED LISTINGS: {len(rows)}")
for r in rows:
    print("ROW\t" + "\t".join(r))
