"""ROUGH single-battery sanity check over real 2025 ERCOT RT SPPs (15-min, ffilled to 5-min).

Physics = spec section 6 read literally:
  energy = setpoint_kw * 5/60, capped by power_kw and SoC bounds [0.20, 1.00]
  charge:    grid draws `energy`, battery stores energy*sqrt(eta)
  discharge: battery gives up `energy`, grid receives energy*sqrt(eta)
Revenue = grid-side delivered_kwh * price/1000 (negative when buying).
"""
import time, numpy as np, pandas as pd
from scipy.optimize import linprog
from scipy import sparse

CAP, PKW, ETA, SOC0, FLOOR, CEIL = 13.5, 5.0, 0.90, 0.5, 0.20, 1.00
DT = 5 / 60
R = np.sqrt(ETA)
EMAX = PKW * DT                 # kWh per tick at full power
WIN = 7 * 288                   # trailing 7 days of 5-min ticks
LOCS = ["HB_NORTH", "HB_HOUSTON", "LZ_HOUSTON", "HB_WEST", "LZ_NORTH"]
START = pd.Timestamp("2025-01-01 06:00", tz="UTC")   # 2025-01-01 00:00 US/Central


def load_5min(loc):
    parts = [pd.read_parquet(f"data/ercot_rt_{y}_hubs.parquet") for y in (2024, 2025)]
    df = pd.concat(parts)
    s = df[df.location == loc].set_index("ts").price_usd_mwh.sort_index()
    grid = pd.date_range(s.index.min(), s.index.max() + pd.Timedelta("10min"), freq="5min")
    return s.reindex(grid).ffill()       # 15-min SPP held for 3 ticks


def step(soc, setpoint_kw):
    """returns new_soc, grid_kwh (+ sold, - bought), cell_kwh_out"""
    e = min(abs(setpoint_kw), PKW) * DT
    if setpoint_kw < 0:                                  # charge
        e = min(e, max(0.0, (CEIL - soc) * CAP / R))     # grid-side energy
        return soc + e * R / CAP, -e, 0.0
    if setpoint_kw > 0:                                  # discharge
        e = min(e, max(0.0, (soc - FLOOR) * CAP))        # cell-side energy
        return soc - e / CAP, e * R, e
    return soc, 0.0, 0.0


def run(prices, setpoints):
    soc, rev, sold, bought, cell_out = SOC0, 0.0, 0.0, 0.0, 0.0
    rev_t = np.zeros(len(prices))
    for i, (p, sp) in enumerate(zip(prices, setpoints)):
        soc, g, c = step(soc, sp)
        rev_t[i] = g * p / 1000
        cell_out += c
        if g > 0: sold += g
        else: bought -= g
    return dict(usd=rev_t.sum(), sold_kwh=sold, bought_kwh=bought,
                fec_nameplate=cell_out / CAP, fec_usable=cell_out / (CAP * (CEIL - FLOOR)),
                avg_sell=(rev_t[rev_t > 0].sum() * 1000 / sold) if sold else np.nan,
                avg_buy=(-rev_t[rev_t < 0].sum() * 1000 / bought) if bought else np.nan,
                end_soc=soc), rev_t


def lp_day(p, cycle_cap=True):
    """Perfect-foresight daily LP (cyclic SoC). vars: c[0..n), d[0..n), s0."""
    n = len(p); r = R
    # objective: maximize sum p/1000*(r*d - c)  -> minimize negative
    cost = np.concatenate([p / 1000, -p / 1000 * r, [0.0]])
    L = sparse.tril(np.ones((n, n)), format="csr")
    # E_t = s0 + L@(r c - d) in [FLOOR*CAP, CEIL*CAP]
    A_up = sparse.hstack([L * r, -L, np.ones((n, 1))])
    A = [A_up, -A_up]
    b = [np.full(n, CEIL * CAP), np.full(n, -FLOOR * CAP)]
    A.append(sparse.hstack([sparse.identity(n) / EMAX, sparse.identity(n) / EMAX, np.zeros((n, 1))]))
    b.append(np.ones(n))                                  # c/EMAX + d/EMAX <= 1 (no simultaneous full both ways)
    if cycle_cap:
        A.append(sparse.csr_matrix(np.concatenate([np.zeros(n), np.ones(n), [0.0]])))
        b.append([CAP * (CEIL - FLOOR)])                  # <= 1 usable cycle per day
    A_eq = sparse.csr_matrix(np.concatenate([np.full(n, r), -np.ones(n), [0.0]]))  # cyclic: net 0
    res = linprog(cost, A_ub=sparse.vstack(A), b_ub=np.concatenate([np.ravel(x) for x in b]),
                  A_eq=A_eq, b_eq=[0.0],
                  bounds=[(0, EMAX)] * (2 * n) + [(FLOOR * CAP, CEIL * CAP)], method="highs")
    assert res.status == 0, res.message
    d = res.x[n:2 * n]
    return -res.fun, d.sum()


def main():
    out = []
    for loc in LOCS:
        t0 = time.time()
        s = load_5min(loc)
        hist = s.rolling(WIN, min_periods=WIN)
        p25 = hist.quantile(0.25).shift(1)   # window = previous 7 days, excludes current tick
        p90 = hist.quantile(0.90).shift(1)
        y = s[s.index >= START]
        assert len(y) == 105120 and p25[y.index].notna().all(), (len(y), p25[y.index].isna().sum())
        price = y.values
        local = y.index.tz_convert("US/Central")
        hr = local.hour + local.minute / 60

        # --- Strategy v1 (spec s8) ---
        sp_v1 = np.where(price <= p25[y.index].values, -PKW, np.where(price >= p90[y.index].values, PKW, 0.0))
        v1, rev_v1 = run(price, sp_v1)

        # --- Alt A: discharge 18-21 local, charge fixed 01:00-05:00 local (no foresight) ---
        sp_a = np.where((hr >= 18) & (hr < 21), PKW, np.where((hr >= 1) & (hr < 5), -PKW, 0.0))
        altA, _ = run(price, sp_a)
        # --- Alt B: discharge 18-21, charge in that night's 28 cheapest 5-min ticks in 00:00-06:00 (foresight) ---
        sp_b = np.where((hr >= 18) & (hr < 21), PKW, 0.0)
        ser = pd.Series(price, index=local)
        night = (hr < 6)
        dates = pd.Index(local.date)
        for d in pd.unique(dates):
            idx = np.where((dates == d) & night)[0]
            cheapest = idx[np.argsort(price[idx], kind="stable")[:28]]
            sp_b[cheapest] = -PKW
        altB, _ = run(price, sp_b)
        # --- Alt C (bonus): discharge 18-21, charge fixed 10:00-15:00 local (solar trough) ---
        sp_c = np.where((hr >= 18) & (hr < 21), PKW, np.where((hr >= 10) & (hr < 15), -PKW, 0.0))
        altC, _ = run(price, sp_c)

        # --- Perfect foresight daily LP ---
        pf1 = pfu = 0.0; cyc1 = 0.0
        for d in pd.unique(dates):
            p = price[dates == d]
            v, dd = lp_day(p, True); pf1 += v; cyc1 += dd
            v2, _ = lp_day(p, False); pfu += v2

        # v1 spike-day contribution
        rv = pd.Series(rev_v1, index=local).groupby(local.date).sum().sort_values(ascending=False)
        out.append(dict(loc=loc, v1=v1, altA=altA, altB=altB, altC=altC, pf1=pf1, pf1_fec=cyc1 / CAP, pfu=pfu,
                        top5=rv.head(5), secs=time.time() - t0, v1_top10_share=rv.head(10).sum() / rv.sum()))
        print(f"{loc} done in {time.time()-t0:.1f}s")

    print("\n=== ROUGH: $/battery/year, 2025, 13.5 kWh / 5 kW / RTE 0.90, SoC [0.2,1.0] ===")
    for o in out:
        print(f"\n{o['loc']}")
        for k in ("v1", "altA", "altB", "altC"):
            m = o[k]
            print(f"  {k:5s} ${m['usd']:8.2f}  FEC/yr(nameplate) {m['fec_nameplate']:6.1f}  FEC/yr(usable 10.8kWh) {m['fec_usable']:6.1f}"
                  f"  sold {m['sold_kwh']:7.0f} kWh @ ${m['avg_sell']:.1f}/MWh  bought {m['bought_kwh']:7.0f} kWh @ ${m['avg_buy']:.1f}/MWh  end_soc {m['end_soc']:.2f}")
        print(f"  PF-LP 1 cycle/day cap: ${o['pf1']:8.2f}  (FEC nameplate {o['pf1_fec']:.1f})")
        print(f"  PF-LP uncapped cycles: ${o['pfu']:8.2f}")
        print(f"  v1 top-10 revenue days share of annual: {o['v1_top10_share']*100:.0f}%")
        print("  v1 top-5 days $:", ", ".join(f"{d}:{v:.2f}" for d, v in o["top5"].items()))


if __name__ == "__main__":
    main()
