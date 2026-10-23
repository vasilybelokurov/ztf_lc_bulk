"""Remove DESI DR1 QSOs listed twice under different targetids.

Two entries are the same object if they share a ZTF objectid within 1" and their
positions agree to < 0.1" (they are measured as offsets from the same ZTF position).
On 2026-10-23, 18,728 entries (1.29% of matched) shared a ZTF objectid; 96.6% of those
pairs have separation 0.000" and 98.8% |dz| < 0.01. 8,997 targetids are dropped (the
higher of each pair). Applied to desi_real/desi_shift; originals kept as *_withdups.
"""
import os
import numpy as np
import pandas as pd

for mode in ["real", "shift"]:                  # first run: keep the originals as *_withdups
    if not os.path.exists(f"desi_{mode}_withdups.parquet"):
        os.rename(f"desi_{mode}.parquet", f"desi_{mode}_withdups.parquet")
        os.rename(f"desi_{mode}.nqso", f"desi_{mode}_withdups.nqso")
p = pd.read_parquet("desi_real_withdups.parquet")
c = p[p.sep < 1][["qid", "objectid", "dra", "ddec"]]
m = c.merge(c, on="objectid")
m = m[(m.qid_x < m.qid_y) & (np.hypot(m.dra_x - m.dra_y, m.ddec_x - m.ddec_y) < 0.1)]
drop = np.unique(m.qid_y)                       # keep the lowest targetid of each group
nq = int(open("desi_real_withdups.nqso").read())
print("duplicate targetids dropped:", drop.size)
for mode in ["real", "shift"]:
    d = pd.read_parquet(f"desi_{mode}_withdups.parquet")
    d[~d.qid.isin(drop)].to_parquet(f"desi_{mode}.parquet")
    open(f"desi_{mode}.nqso", "w").write(str(nq - drop.size))
    print(mode, int((~d.qid.isin(drop)).sum()), "pairs,", nq - drop.size, "QSOs")
pd.DataFrame({"targetid": drop}).to_parquet("desi_duplicate_targetids_dropped.parquet")
