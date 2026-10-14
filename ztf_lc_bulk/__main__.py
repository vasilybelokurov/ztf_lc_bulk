"""Command line interface: ``ztf-lc-bulk`` or ``python -m ztf_lc_bulk``.

Examples
--------
ztf-lc-bulk match   --table vasily.cosmos2020_classic --id id --ra ra --dec dec --out runs/cosmos
ztf-lc-bulk match   --input targets.csv --id name --ra ra --dec dec --out runs/mylist
ztf-lc-bulk extract --out runs/cosmos --workers 4
ztf-lc-bulk verify  --out runs/cosmos
ztf-lc-bulk validate-api --out runs/cosmos --n 40
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _read_targets(path: str, id_col: str, ra_col: str, dec_col: str):
    """Read id/ra/dec from a .parquet, .csv or .fits file."""
    p = Path(path)
    if p.suffix == ".parquet":
        import pyarrow.parquet as pq
        t = pq.read_table(p, columns=[id_col, ra_col, dec_col]).to_pandas()
    elif p.suffix in (".fits", ".fit"):
        from astropy.table import Table
        t = Table.read(p)[[id_col, ra_col, dec_col]].to_pandas()
    else:
        import pandas as pd
        t = pd.read_csv(p, usecols=[id_col, ra_col, dec_col])
    return t[id_col].to_numpy(), t[ra_col].to_numpy(float), t[dec_col].to_numpy(float)


def main(argv=None):
    p = argparse.ArgumentParser(prog="ztf-lc-bulk", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("match", help="q3c crossmatch targets against koposov.ztf_23_objs")
    src = m.add_mutually_exclusive_group(required=True)
    src.add_argument("--table", help="schema-qualified WSDB table")
    src.add_argument("--input", help="local .parquet/.csv/.fits target list (uploaded)")
    m.add_argument("--id", default="id")
    m.add_argument("--ra", default="ra")
    m.add_argument("--dec", default="dec")
    m.add_argument("--radius", type=float, default=1.0, help="arcsec (default 1.0)")
    m.add_argument("--where", default=None, help="extra SQL on the WSDB table (alias c)")
    m.add_argument("--out", required=True, help="run directory")

    e = sub.add_parser("extract", help="fetch parquet files and extract matched light curves")
    e.add_argument("--out", required=True, help="run directory")
    e.add_argument("--match", default=None, help="default: <out>/match.parquet")
    e.add_argument("--workers", type=int, default=4)
    e.add_argument("--mirror", default=None, help="root of a local IRSA lc/ mirror")
    e.add_argument("--keep-raw", action="store_true")
    e.add_argument("--max-files", type=int, default=None)

    v = sub.add_parser("verify", help="consistency checks on extracted output")
    v.add_argument("--out", required=True)
    v.add_argument("--match", default=None)

    a_ = sub.add_parser("validate-api", help="compare a sample with the IRSA LC API")
    a_.add_argument("--out", required=True)
    a_.add_argument("--n", type=int, default=40)
    a_.add_argument("--threads", type=int, default=4)
    a_.add_argument("--seed", type=int, default=1)

    a = p.parse_args(argv)
    out = Path(a.out)
    match_path = (getattr(a, "match", None) or out / "match.parquet")
    if a.cmd == "match":
        from .match import run_match
        if a.table:
            res = run_match(match_path, table=a.table, id_col=a.id, ra_col=a.ra,
                            dec_col=a.dec, radius_arcsec=a.radius, where=a.where)
        else:
            ids, ra, dec = _read_targets(a.input, a.id, a.ra, a.dec)
            res = run_match(match_path, src_id=ids, ra=ra, dec=dec, radius_arcsec=a.radius)
    elif a.cmd == "extract":
        from .pipeline import run_extract
        res = run_extract(match_path, out, workers=a.workers, mirror=a.mirror,
                          keep_raw=a.keep_raw, max_files=a.max_files)
    elif a.cmd == "verify":
        from .pipeline import verify
        res = verify(match_path, out)
    else:
        import pandas as pd
        from .validate import api_spotcheck
        r = api_spotcheck(out, n=a.n, threads=a.threads, seed=a.seed)
        with pd.option_context("display.width", 200, "display.max_rows", 500):
            print(r.pop("table").to_string(index=False))
            extra = r.pop("api_only")
            if len(extra):
                print("\nAPI-only epochs per night (MJD):")
                print(extra.groupby(np.floor(extra["mjd"]).astype(int)).size().to_string())
        res = r
    print(json.dumps(res, indent=2, default=str))
    if isinstance(res, dict) and res.get("pass") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
