import gridstatus, json, time
from gridstatus.ercot import HISTORICAL_RTM_LOAD_ZONE_AND_HUB_PRICES_RTID as RT, HISTORICAL_DAM_LOAD_ZONE_AND_HUB_PRICES_RTID as DA
e = gridstatus.Ercot()
for name, rid in [("RT", RT), ("DA", DA)]:
    docs = e._get_documents(report_type_id=rid)
    print(name, rid, "n docs", len(docs))
    for d in sorted(docs, key=lambda d: d.publish_date)[-6:]:
        print("  ", d.constructed_name, d.publish_date, d.url)
