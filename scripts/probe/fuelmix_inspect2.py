import pandas as pd
s = pd.read_excel("raw/IntGenbyFuel2025.xlsx", sheet_name=None, engine="calamine")
months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
for m in months:
    d = s[m]; print(m, d.shape, "dates", d.Date.min(), "->", d.Date.max(), "ndates", d.Date.nunique(), "settle", d["Settlement Type"].value_counts().to_dict())
nov = s["Nov"]; print("Nov extra cols:", [c for c in nov.columns if c not in s["Oct"].columns])
mar = s["Mar"]; r = mar[(mar.Date.astype(str).str.startswith("2025-03-09")) & (mar.Fuel=="Nuclear")]
print("Mar 9 nuclear cols 1:45..3:15:", r[["1:45","2:00","2:15","2:30","2:45","3:00","3:15"]].to_string())
r = nov[(nov.Date.astype(str).str.startswith("2025-11-02")) & (nov.Fuel=="Nuclear")]
print("Nov 2 nuclear around 1-2am:", r[[c for c in nov.columns if c[:2] in ("1:","2:") or "*" in str(c) or "x" in str(c).lower()]].to_string())
print("Nov 3 nuclear extra cols:", nov[(nov.Date.astype(str).str.startswith("2025-11-03")) & (nov.Fuel=="Nuclear")][[c for c in nov.columns if c not in s["Oct"].columns]].to_string())
print(s["Summary"].head(12).to_string())
