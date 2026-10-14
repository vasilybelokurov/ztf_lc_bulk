"""Compare the DR23 and DR24 versions of ZTF light-curve parquet files, whole-file.

For each file name (DR23 index path), both releases are downloaded and compared
over **all** objects in the quadrant, not just matched ones:
- objectids only in DR23 (would break the DR23 index for DR24), only in DR24
  (new objects, unreachable through the DR23 index), and in both;
- for objects in both: position shift, whether every DR23 epoch is present in
  DR24, whether mag on the shared epochs is identical.

Usage
-----
python scripts/compare_release_files.py OUTDIR lc_dr23/0/field000468/ztf_000468_zi_c03_q1_dr23.parquet [...]

Files are downloaded into OUTDIR (kept) so the comparison can be rerun offline.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from ztf_lc_bulk.fetch import download
from ztf_lc_bulk.pipeline import release_filename


def compare(f23: Path, f24: Path) -> dict:
    a = pq.read_table(f23).sort_by("objectid")
    b = pq.read_table(f24).sort_by("objectid")
    oa, ob = a["objectid"].to_numpy(), b["objectid"].to_numpy()
    common = np.intersect1d(oa, ob)
    ia, ib = np.searchsorted(oa, common), np.searchsorted(ob, common)
    ra_a, de_a = a["objra"].to_numpy().astype(float), a["objdec"].to_numpy().astype(float)
    ra_b, de_b = b["objra"].to_numpy().astype(float), b["objdec"].to_numpy().astype(float)
    dpos = np.hypot((ra_a[ia] - ra_b[ib]) * np.cos(np.radians(de_a[ia])), de_a[ia] - de_b[ib]) * 3600
    ha, hb = a["hmjd"].combine_chunks(), b["hmjd"].combine_chunks()
    ma, mb = a["mag"].combine_chunks(), b["mag"].combine_chunks()
    subset = same_mag = 0
    for i, j in zip(ia, ib):
        x = np.round(np.asarray(ha[int(i)].values), 5)
        y = np.round(np.asarray(hb[int(j)].values), 5)
        mx = dict(zip(x, np.asarray(ma[int(i)].values)))
        my = dict(zip(y, np.asarray(mb[int(j)].values)))
        subset += set(mx) <= set(my)
        same_mag += all(mx[t] == my[t] for t in set(mx) & set(my))
    new = np.setdiff1d(ob, oa)
    nb = b["nepochs"].to_numpy()
    return {
        "file": f23.name, "bytes_dr23": f23.stat().st_size, "bytes_dr24": f24.stat().st_size,
        "objects_dr23": int(oa.size), "objects_dr24": int(ob.size), "in_both": int(common.size),
        "only_dr23": int(np.setdiff1d(oa, ob).size), "only_dr24": int(new.size),
        "only_dr24_frac": round(new.size / ob.size, 4),
        "max_position_shift_arcsec": float(dpos.max()) if dpos.size else 0.0,
        "dr23_epochs_all_in_dr24": int(subset), "mag_identical_on_shared_epochs": int(same_mag),
        "median_nepochs_new_objects": float(np.median(nb[np.searchsorted(ob, new)])) if new.size else None,
        "median_nepochs_all_dr24": float(np.median(nb)),
    }


def main(outdir, *names):
    out = Path(outdir)
    rows = []
    for n in names:
        p23, _ = download(release_filename(n, "dr23"), out)
        p24, _ = download(release_filename(n, "dr24"), out)
        rows.append(compare(p23, p24))
        print(json.dumps(rows[-1]), flush=True)
    return rows


if __name__ == "__main__":
    main(*sys.argv[1:])
