import numpy as np, pandas as pd, sys
sys.path.insert(0, "scripts")
from rough_sim import load_5min, step, WIN, START, PKW
s = load_5min("HB_NORTH"); h = s.rolling(WIN, min_periods=WIN)
p25 = h.quantile(.25).shift(1); p90 = h.quantile(.9).shift(1)
y = s[s.index >= START]; price = y.values
sp = np.where(price <= p25[y.index].values, -PKW, np.where(price >= p90[y.index].values, PKW, 0.0))
soc = 0.5; socs = []; revs = []
for p, x in zip(price, sp):
    soc, g, _ = step(soc, x); socs.append(soc); revs.append(g*p/1000)
df = pd.DataFrame({"price": price, "p25": p25[y.index].values, "p90": p90[y.index].values, "sp": sp, "soc": socs, "rev": revs}, index=y.index.tz_convert("US/Central"))
for d in ["2025-04-07", "2025-05-16", "2025-01-15"]:
    x = df.loc[d]; x = x.iloc[::3]  # 15-min
    pk = x.price.idxmax()
    print(f"== {d}: v1 day revenue ${df.loc[d].rev.sum():.2f}; max price {x.price.max():.0f} at {pk.time()}; SoC at that time {x.loc[pk,'soc']:.2f}; p90 {x.loc[pk,'p90']:.1f}")
    w = x[(x.index >= pk - pd.Timedelta('4h')) & (x.index <= pk + pd.Timedelta('30min'))]
    print(w[["price","p25","p90","sp","soc"]].round(2).to_string())
