import gridstatus, requests, time
from gridstatus import ercot as m
e = gridstatus.Ercot()
for name in ["LMPS_BY_SETTLEMENT_POINT_RTID", "SETTLEMENT_POINT_PRICES_AT_RESOURCE_NODES_HUBS_AND_LOAD_ZONES_RTID", "DAM_SETTLEMENT_POINT_PRICES_RTID"]:
    rid = getattr(m, name)
    t0=time.time()
    r = requests.get(f"https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={rid}", timeout=120)
    docs = r.json()["ListDocsByRptTypeRes"]["DocumentList"]
    pubs = sorted(d["Document"]["PublishDate"] for d in docs)
    print(name, rid, "http", r.status_code, "ndocs", len(docs), "oldest", pubs[0], "newest", pubs[-1], f"{time.time()-t0:.1f}s")
