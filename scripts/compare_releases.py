"""Compare two runs of the same match table extracted from different releases.

Checks, per objectid present in both runs: every epoch of the older run is in
the newer run (hmjd equal to 1e-5 d) with identical mag, magerr and catflags.
Reports how many epochs the newer release adds and how far it extends in time.

Usage
-----
python scripts/compare_releases.py runs/cosmos runs/cosmos_dr24
"""

import json
import sys

import numpy as np
import pyarrow.compute as pc
import pyarrow.dataset as ds


def load(run):
    t = ds.dataset(f"{run}/lc", format="parquet").to_table()
    return t.sort_by("objectid")


def main(old_run, new_run):
    a, b = load(old_run), load(new_run)
    oa, ob = a["objectid"].to_numpy(), b["objectid"].to_numpy()
    common = np.intersect1d(oa, ob)
    ia, ib = np.searchsorted(oa, common), np.searchsorted(ob, common)
    res = {"objects_old": int(oa.size), "objects_new": int(ob.size), "objects_common": int(common.size)}
    ha, hb = a["hmjd"].combine_chunks(), b["hmjd"].combine_chunks()
    cols = ("mag", "magerr", "catflags")
    A = {c: a[c].combine_chunks() for c in cols}
    B = {c: b[c].combine_chunks() for c in cols}
    n_sub = n_val = 0
    added = []
    last_old, last_new = [], []
    for i, j in zip(ia, ib):
        x = np.asarray(ha[int(i)].values); y = np.asarray(hb[int(j)].values)
        k = np.searchsorted(y, x).clip(0, max(y.size - 1, 0))
        hit = y.size > 0 and np.all(np.abs(y[k] - x) <= 1e-5) if x.size else True
        n_sub += bool(hit)
        if hit and x.size:
            n_val += all(np.array_equal(np.asarray(A[c][int(i)].values), np.asarray(B[c][int(j)].values)[k])
                         for c in cols)
        elif hit:
            n_val += 1
        added.append(y.size - x.size)
        if x.size:
            last_old.append(x[-1])
        if y.size:
            last_new.append(y[-1])
    added = np.array(added)
    res.update({
        "old_epochs_all_in_new": int(n_sub), "values_identical": int(n_val),
        "epochs_old": int(pc.sum(a["nepochs"]).as_py()), "epochs_new": int(pc.sum(b["nepochs"]).as_py()),
        "added_epochs_median": float(np.median(added)), "added_epochs_p90": float(np.percentile(added, 90)),
        "objects_with_no_new_epochs": int((added == 0).sum()),
        "last_hmjd_old_max": float(np.max(last_old)), "last_hmjd_new_max": float(np.max(last_new)),
        "pass": bool(n_sub == common.size and n_val == common.size),
    })
    print(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    sys.exit(0 if main(*sys.argv[1:3])["pass"] else 1)
