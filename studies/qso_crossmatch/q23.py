"""Q2 (g vs r objectid positions) and Q3 (extra objectids inside the match radius).

SDSS DR16Q QSOs; ZTF DR23 index pairs within R (default 1"); light curves from a DR23
parquet extraction of all objectids within 1" of DR16Q QSOs (one file per band,
$ZTF_QSO_LC_DIR/lc_filter{1,2}_chunk0.parquet, default ~/data/ztf/qso; built by the
ztf_qso_parquet project; columns as in the IRSA files).

Usage: python q23.py [R_arcsec]
Writes q2_astrometry.csv, q2_lc.parquet, q2_lc_resid.csv, q3_lc.parquet.
"""
import os, sys
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

R = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
LC_DIR = os.path.expanduser(os.environ.get("ZTF_QSO_LC_DIR", "~/data/ztf/qso"))
rng = np.random.default_rng(1)
mbins = [(14, 19), (19, 20), (20, 20.5), (20.5, 21), (21, 23)]


def rsig(x):
    """Robust sigma: half the 16-84 percentile range."""
    q = np.nanpercentile(x, [16, 84]); return 0.5 * (q[1] - q[0])


def read_lc(path, ids):
    t = pq.read_table(path, columns=["objectid", "hmjd", "mag", "catflags"],
                      filters=[("objectid", "in", list(map(int, ids)))])
    return {int(o): (np.asarray(h), np.asarray(m), np.asarray(c)) for o, h, m, c in
            zip(t["objectid"].to_pylist(), t["hmjd"].to_numpy(zero_copy_only=False),
                t["mag"].to_numpy(zero_copy_only=False), t["catflags"].to_numpy(zero_copy_only=False))}


p = pd.read_parquet("sdss_real.parquet")
p = p[(p.sep < R) & (p.filterid < 3)].copy()
# "longest" = most epochs; ties: smaller separation, then smaller objectid
p = p.sort_values(["qid", "filterid", "nepochs", "sep", "objectid"], ascending=[1, 1, 0, 1, 1])
p["rank"] = p.groupby(["qid", "filterid"]).cumcount()
prim = p[p["rank"] == 0]

# ---------------- Q2: positions of the g and r objectids ----------------
g = prim[prim.filterid == 1].set_index("qid"); r = prim[prim.filterid == 2].set_index("qid")
gr = g.join(r, lsuffix="_g", rsuffix="_r", how="inner")
gr["dgr"] = np.hypot(gr.dra_g - gr.dra_r, gr.ddec_g - gr.ddec_r)
gr["same_field"] = gr.fieldid_g == gr.fieldid_r
gr["mag"] = gr.rsdss_g
print(f"Q2: QSOs with g and r within {R}\": {len(gr)}; same field {gr.same_field.mean():.3f}; "
      f"g-r separation p50/p90/p99 {np.round(np.percentile(gr.dgr, [50, 90, 99]), 3)}")
q2 = []
for lo, hi in mbins:
    s = gr[gr.mag.between(lo, hi) & gr.same_field]
    sg = np.hypot(rsig(s.dra_g), rsig(s.ddec_g)) / np.sqrt(2)
    sr = np.hypot(rsig(s.dra_r), rsig(s.ddec_r)) / np.sqrt(2)
    sd = np.hypot(rsig(s.dra_g - s.dra_r), rsig(s.ddec_g - s.ddec_r)) / np.sqrt(2)
    q2.append(dict(mag=f"{lo}-{hi}", N=len(s), sig_g=sg, sig_r=sr, sig_gminusr=sd, sig_expected_indep=np.hypot(sg, sr),
                   corr_g_r_offsets=np.corrcoef(np.r_[s.dra_g, s.ddec_g], np.r_[s.dra_r, s.ddec_r])[0, 1],
                   dgr_med=s.dgr.median(), dgr_p90=s.dgr.quantile(.9)))
q2 = pd.DataFrame(q2); print(q2.round(3).to_string(index=False)); q2.to_csv("q2_astrometry.csv", index=False)

# light curves of a random subsample (same field, >= 30 epochs in each band)
sub = gr[gr.same_field & (gr.nepochs_g >= 30) & (gr.nepochs_r >= 30)]
sub = sub.loc[rng.choice(sub.index, min(40000, len(sub)), replace=False)]
lcg = read_lc(f"{LC_DIR}/lc_filter1_chunk0.parquet", sub.objectid_g)
lcr = read_lc(f"{LC_DIR}/lc_filter2_chunk0.parquet", sub.objectid_r)
res = []
for q, s in sub.iterrows():
    if s.objectid_g not in lcg or s.objectid_r not in lcr:
        continue
    hg, mg, cg = lcg[s.objectid_g]; hr, mr, cr = lcr[s.objectid_r]
    hg, mg = hg[cg == 0], mg[cg == 0]; hr, mr = hr[cr == 0], mr[cr == 0]
    if hg.size < 20 or hr.size < 20:
        continue
    # pair each g epoch with the nearest r epoch on the same night (|dt| < 0.2 d)
    j = np.clip(np.searchsorted(hr, hg), 1, hr.size - 1)
    j = np.where(np.abs(hr[j - 1] - hg) < np.abs(hr[j] - hg), j - 1, j)
    ok = np.abs(hr[j] - hg) < 0.2
    if ok.sum() < 15:
        continue
    dg = mg[ok] - np.median(mg); dr = mr[j[ok]] - np.median(mr)
    res.append(dict(qid=q, dgr=s.dgr, mag=s.mag, n=int(ok.sum()), corr=np.corrcoef(dg, dr)[0, 1],
                    col_resid=(np.median(mg) - np.median(mr)) - (s.gsdss_g - s.rsdss_g)))
res = pd.DataFrame(res).dropna(subset=["corr", "mag"]); res.to_parquet("q2_lc.parquet")
# compare each QSO with QSOs of the same magnitude (0.1-mag running median), then bin by separation
res["mfine"] = (res.mag * 10).round() / 10
res["dcorr"] = res["corr"] - res.groupby("mfine")["corr"].transform("median")
res["dcol"] = res.col_resid - res.groupby("mfine")["col_resid"].transform("median")
t = res.groupby(pd.cut(res.dgr, [0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0]), observed=True).agg(
    N=("qid", "size"), mag_med=("mag", "median"), dcorr_med=("dcorr", "median"),
    dcorr_err=("dcorr", lambda x: 1.253 * x.std() / np.sqrt(len(x))), dcol_med=("dcol", "median"),
    dcol_err=("dcol", lambda x: 1.253 * x.std() / np.sqrt(len(x))))
print(f"Q2 light curves ({len(res)} QSOs), relative to same-magnitude QSOs:\n", t.round(4).to_string())
t.to_csv("q2_lc_resid.csv")

# ---------------- Q3: extra objectids inside R ----------------
pp = p.merge(prim[["qid", "filterid", "objectid", "fieldid", "rcid", "nepochs", "sep"]],
             on=["qid", "filterid"], suffixes=("", "_p"))
ex = pp[pp["rank"] > 0].copy()
ex["kind"] = np.where(ex.fieldid != ex.fieldid_p, "other field",
             np.where(ex.rcid == ex.rcid_p, "same field + quadrant", "same field, other quadrant"))
# light-curve level, r band, random 30k QSOs with extras: shared epochs, zero-point offset
samp = ex[ex.filterid == 2]
samp = samp[samp.qid.isin(rng.choice(samp.qid.unique(), min(30000, samp.qid.nunique()), replace=False))]
lc = read_lc(f"{LC_DIR}/lc_filter2_chunk0.parquet", np.unique(np.r_[samp.objectid, samp.objectid_p]))
out = []
for _, s in samp.iterrows():
    if s.objectid not in lc or s.objectid_p not in lc:
        continue
    h1, m1, c1 = lc[s.objectid_p]; h2, m2, c2 = lc[s.objectid]
    h1, m1 = h1[c1 == 0], m1[c1 == 0]; h2, m2 = h2[c2 == 0], m2[c2 == 0]
    if h1.size == 0 or h2.size == 0:
        continue
    shared = np.intersect1d(np.round(h1 * 86400), np.round(h2 * 86400)).size
    j = np.minimum(np.clip(np.searchsorted(h1, h2), 1, max(h1.size - 1, 1)), h1.size - 1)
    j = np.where(np.abs(h1[j - 1] - h2) < np.abs(h1[j] - h2), j - 1, j)
    ok = np.abs(h1[j] - h2) < 0.3                       # same night
    out.append(dict(kind=s.kind, mag=s.rsdss, n_p=h1.size, n_x=h2.size, frac_shared=shared / h2.size,
                    offset=np.median(m2[ok] - m1[j[ok]]) if ok.sum() >= 3 else np.nan))
o = pd.DataFrame(out); o.to_parquet("q3_lc.parquet")
o["mbin"] = pd.cut(o.mag, [14, 19, 20, 20.5, 21, 23])
print("\nQ3 extras, r band:\n", o.groupby("kind").agg(N=("mag", "size"), frac_shared_med=("frac_shared", "median"),
      offset_med=("offset", "median"), offset_sig=("offset", rsig)).round(3).to_string())
print(o[o.kind == "other field"].groupby("mbin", observed=True).agg(
    N=("mag", "size"), offset_med=("offset", "median"), offset_sig=("offset", rsig)).round(3).to_string())
