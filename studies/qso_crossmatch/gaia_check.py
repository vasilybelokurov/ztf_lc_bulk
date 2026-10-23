"""Q2 check: are the ZTF g and r objectid offsets astrometric noise? Reference them to
Gaia DR3 instead of SDSS, for SDSS QSOs with r < 20.5 in ra 180-190, dec 10-30.

Nearest Gaia source per QSO via LATERAL q3c_radial_query ... ORDER BY q3c_dist LIMIT 1
(gaia_nn.sql). Do NOT use q3c_join + DISTINCT ON here: on 2026-10-23 that plan dropped
the q3c index and scanned all 1.8e9 gaia_source rows. This version runs in ~9 s.
Writes sdss_gaia_offsets.parquet and q2_gaia.csv. Needs sdss_real.parquet.
"""
import os
import numpy as np
import pandas as pd
import sqlutilpy as sqlutil

here = os.path.dirname(os.path.abspath(__file__))
d = pd.DataFrame(dict(zip(["qid", "dra_sg", "ddec_sg"],
                          sqlutil.get(open(os.path.join(here, "gaia_nn.sql")).read(),
                                      preamb="set statement_timeout='300s'"))))
d.to_parquet("sdss_gaia_offsets.parquet")
p = pd.read_parquet("sdss_real.parquet")
p = p[(p.sep < 1) & (p.filterid < 3)]
p = p.sort_values(["qid", "filterid", "nepochs"], ascending=[1, 1, 0]).groupby(["qid", "filterid"]).head(1)
g = p[p.filterid == 1].set_index("qid"); r = p[p.filterid == 2].set_index("qid")
x = g.join(r, lsuffix="_g", rsuffix="_r", how="inner").join(d.set_index("qid"), how="inner")
x = x[x.fieldid_g == x.fieldid_r]


def rs(v):
    a = np.percentile(v, [16, 84]); return 0.5 * (a[1] - a[0])


print(f"{len(d)} QSOs with Gaia within 1\"; SDSS - Gaia sigma per axis "
      f"{rs(x.dra_sg):.3f} {rs(x.ddec_sg):.3f} arcsec (N={len(x)})")
rows = []
for lo, hi in [(14, 19.5), (19.5, 20), (20, 20.5)]:
    s = x[x.rsdss_g.between(lo, hi)]
    for ref, (ox, oy) in {"SDSS": (0, 0), "Gaia": (s.dra_sg, s.ddec_sg)}.items():
        gx, gy = s.dra_g - ox, s.ddec_g - oy; rx, ry = s.dra_r - ox, s.ddec_r - oy
        sg = np.hypot(rs(gx), rs(gy)) / np.sqrt(2); sr = np.hypot(rs(rx), rs(ry)) / np.sqrt(2)
        sd = np.hypot(rs(gx - rx), rs(gy - ry)) / np.sqrt(2)
        rows.append(dict(mag=f"{lo}-{hi}", ref=ref, N=len(s), sig_g=sg, sig_r=sr, sig_gminusr=sd,
                         indep=np.hypot(sg, sr), corr=np.corrcoef(np.r_[gx, gy], np.r_[rx, ry])[0, 1]))
t = pd.DataFrame(rows)
print(t.round(3).to_string(index=False))
t.to_csv("q2_gaia.csv", index=False)
