import zipfile, time, pandas as pd
z = zipfile.ZipFile("raw/RTMLZHBSPP_2025.zip"); f = z.open(z.namelist()[0])
t0=time.time(); s = pd.read_excel(f, sheet_name=None, engine="calamine"); df = pd.concat(s.values())
print(f"calamine read {time.time()-t0:.1f}s rows={len(df)}")
print(df.dtypes)
