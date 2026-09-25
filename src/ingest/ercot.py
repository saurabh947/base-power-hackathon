"""ERCOT public data -> Postgres (milestone M1). Also the Cloud Run job `fleet-sim-ingest`.

    python -m src.ingest.ercot --years 2024 2025

Loads, for each ERCOT (America/Chicago) calendar year:
- native_load: hourly native load by weather zone plus the ERCOT total
- fuel_mix:    15-min generation by fuel (MW) and the generation shares
- rt_spp:      RT 15-min settlement point prices for all 15 hubs and load zones (LZ, never LZEW)
- da_spp:      DA hourly settlement point prices for the same 15 points
Load and fuel mix go first, so each RT row is written once with its demand_mw and fuel_mix.

The job is stateless: files are downloaded into a temp dir that is deleted
afterwards. It needs only src.db settings (DATABASE_URL locally, or
DB_HOST/DB_NAME/DB_USER/DB_PASSWORD on Cloud SQL). It runs src.db.migrate()
first and is idempotent: a re-run replaces each (dataset, year) slice and
writes nothing when the files are unchanged. At the end it prints the gap
report (src.ingest.report), writes it to docs/notes/data.md when that
folder exists, and exits 1 unless the M1 self-check passes.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from src.ingest import parse, report, sources, store

DATASETS = report.DATASETS  # load order: native_load, fuel_mix, rt_spp, da_spp


@dataclass
class Step:
    dataset: str
    year: int
    download_s: float = 0.0
    parse_s: float = 0.0
    load_s: float = 0.0
    megabytes: float = 0.0
    rows: int = 0
    result: str = ""
    ok: bool = False


def ingest_one(conn, dataset: str, year: int, workdir: Path, http, pages: dict, reuse: bool,
               downloads: dict) -> Step:
    """Download, parse and load one (dataset, year). Returns its timings."""
    step = Step(dataset, year)
    remote = sources.find(dataset, year, http, pages)
    key = (remote.url, remote.name)
    if key in downloads:  # e.g. FuelMixReport_PreviousYears.zip serves several years
        dl = downloads[key]
    else:
        dl = downloads[key] = sources.download(remote, workdir, http, reuse=reuse)
        step.download_s = dl.seconds
    step.megabytes = dl.bytes / 1e6

    t0 = time.perf_counter()
    if dataset in ("rt_spp", "da_spp"):
        _, data = parse.read_zip_member(dl.path)
        frame = parse.parse_spp(parse.read_workbook(data), "RT" if dataset == "rt_spp" else "DA")
        step.rows = len(frame)
    elif dataset == "native_load":
        _, data = parse.read_zip_member(dl.path) if dl.path.suffix.lower() == ".zip" else (None, dl.path.read_bytes())
        frame = parse.parse_native_load(parse.read_workbook(data))
        step.rows = int(frame.size)
    else:
        if remote.member:
            _, data = parse.read_zip_member(dl.path, remote.member)
        else:
            data = dl.path.read_bytes()
        frame = parse.parse_fuel_mix(parse.read_workbook(data))
        shares = parse.fuel_shares(parse.fuel_mw_wide(frame))
        step.rows = len(frame)
    step.parse_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    if dataset == "rt_spp":
        counts = store.upsert_prices(conn, frame, "RT", year)
    elif dataset == "da_spp":
        counts = store.upsert_prices(conn, frame, "DA", year)
    elif dataset == "native_load":
        counts = store.upsert_native_load(conn, frame, year)
    else:
        counts = store.upsert_generation(conn, frame, shares, year)
    step.load_s = time.perf_counter() - t0
    source_name = remote.name + (f" [{remote.member}]" if remote.member else "")
    store.record_file(conn, dataset=dataset, year=year, source_url=remote.url, source_name=source_name,
                      published_at=dl.published_at, sha256=dl.sha256, size=dl.bytes, rows_loaded=counts.staged,
                      download_s=step.download_s, parse_s=step.parse_s, load_s=step.load_s)
    step.result = str(counts) + (" (cached file)" if dl.cached else "")
    step.ok = True
    return step


def timing_table(steps: list[Step]) -> str:
    lines = ["| dataset | year | MB | download s | parse s | load s | result |", "|---|---|---:|---:|---:|---:|---|"]
    for s in steps:
        lines.append(f"| {s.dataset} | {s.year} | {s.megabytes:.1f} | {s.download_s:.1f} | {s.parse_s:.1f} "
                     f"| {s.load_s:.1f} | {s.result} |")
    per_year = {}
    for s in steps:
        t = per_year.setdefault(s.year, [0.0, 0.0, 0.0])
        t[0] += s.download_s
        t[1] += s.parse_s
        t[2] += s.load_s
    lines += ["", "| year | download s | parse s | load s | total s |", "|---|---:|---:|---:|---:|"]
    for year, (d, p, ld) in sorted(per_year.items()):
        lines.append(f"| {year} | {d:.1f} | {p:.1f} | {ld:.1f} | {d + p + ld:.1f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from src.config import load_raw_config
    from src.db import connect, migrate

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, nargs="+", help="ERCOT years to load (default: config data.years)")
    ap.add_argument("--datasets", nargs="+", choices=DATASETS, help="default: all four")
    ap.add_argument("--cache-dir", type=Path,
                    help="DEV ONLY: keep downloads here and reuse files already present (default: a fresh temp dir)")
    ap.add_argument("--no-notes", action="store_true", help="do not write the gap report into docs/notes/data.md")
    args = ap.parse_args(argv)
    years = args.years or list(load_raw_config().get("data", {}).get("years", [2024, 2025]))
    datasets = [d for d in DATASETS if d in (args.datasets or DATASETS)]

    t_start = time.perf_counter()
    applied = migrate()
    print(f"migrations applied: {', '.join(applied) or 'nothing new'}", flush=True)
    steps: list[Step] = []
    http, pages, downloads = sources.session(), {}, {}
    with connect(autocommit=True) as conn, tempfile.TemporaryDirectory(prefix="ercot-ingest-") as tmp:
        workdir = args.cache_dir or Path(tmp)
        for year in years:
            for dataset in datasets:
                try:
                    step = ingest_one(conn, dataset, year, workdir, http, pages, bool(args.cache_dir), downloads)
                except Exception as exc:  # keep going; the self-check fails below
                    traceback.print_exc()
                    step = Step(dataset, year, result=f"FAILED: {type(exc).__name__}: {exc}")
                steps.append(step)
                print(f"{dataset} {year}: download {step.download_s:.1f}s, parse {step.parse_s:.1f}s, "
                      f"load {step.load_s:.1f}s | {step.result}", flush=True)
            if "rt_spp" in datasets:
                print(f"RT context refresh {year}: {store.enrich_rt(conn, year)} rows updated", flush=True)
        rep = report.build(conn, years, datasets)
    failed = [s for s in steps if not s.ok]
    rep.checks.insert(0, report.Check("every file downloaded, parsed and loaded", not failed,
                                      "; ".join(f"{s.dataset} {s.year}: {s.result}" for s in failed)))
    timings = timing_table(steps)
    md = rep.markdown()
    print("\n## This run: timings\n\n" + timings + f"\n\nwall time {time.perf_counter() - t_start:.1f}s\n")
    print(md)
    if not args.no_notes:
        run_md = (f"Last `python -m src.ingest.ercot --years {' '.join(map(str, years))}` run; "
                  f"wall time {time.perf_counter() - t_start:.1f}s.\n\n{timings}\n")
        if report.write_notes_section(run_md, "ingest-timings") and report.write_notes_section(md, "gap-report"):
            print(f"(written to {report.NOTES_PATH.relative_to(report.REPO_ROOT)})")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
