"""Q3 per QSO: what does the union of all extra objectids (within 1") add to the longest one?

40,000 random SDSS DR16Q QSOs per band (with or without extras). Light curves from
$ZTF_QSO_LC_DIR (see q23.py). Writes q3_merge.parquet.
"""
import os
import numpy as np, pandas as pd, pyarrow.parquet as pq
LC_DIR = os.path.expanduser(os.environ.get("ZTF_QSO_LC_DIR", "~/data/ztf/qso"))
rng = np.random.default_rng(2)
p = pd.read_parquet("sdss_real.parquet"); p = p[(p.sep < 1) & (p.filterid < 3)]
p = p.sort_values(["qid", "filterid", "nepochs", "sep", "objectid"], ascending=[1, 1, 0, 1, 1])
p["rank"] = p.groupby(["qid", "filterid"]).cumcount()
out = []
for f in [1, 2]:
    pf = p[p.filterid == f]
    allq = pf.qid.unique(); qs = rng.choice(allq, 40000, replace=False)   # all QSOs, with or without extras
    s = pf[pf.qid.isin(qs)]
    t = pq.read_table(f"{LC_DIR}/lc_filter{f}_chunk0.parquet",
                      columns=["objectid", "hmjd", "catflags"], filters=[("objectid", "in", list(map(int, s.objectid)))])
    lc = {o: (np.asarray(h), np.asarray(c)) for o, h, c in zip(t["objectid"].to_pylist(),
          t["hmjd"].to_numpy(zero_copy_only=False), t["catflags"].to_numpy(zero_copy_only=False))}
    for q, g in s.groupby("qid"):
        ids = [o for o in g.objectid if o in lc]
        if not ids or g.objectid.iloc[0] not in lc: continue
        h = [lc[o][0][lc[o][1] == 0] for o in ids]
        hp = h[0]; ha = np.unique(np.concatenate(h))
        np_ = np.unique(np.floor(hp - 0.3)).size; na = np.unique(np.floor(ha - 0.3)).size
        out.append(dict(band="gr"[f - 1], qid=q, mag=g.rsdss.iloc[0], n_oid=len(g), ep_p=hp.size, ep_all=ha.size,
                        nights_p=np_, nights_all=na,
                        base_p=np.ptp(hp) if hp.size else 0, base_all=np.ptp(ha) if ha.size else 0))
o = pd.DataFrame(out); o.to_parquet("q3_merge.parquet")
o["epoch_gain"] = o.ep_all / o.ep_p.clip(lower=1) - 1; o["night_gain"] = o.nights_all / o.nights_p.clip(lower=1) - 1
o["base_gain"] = o.base_all - o.base_p
for b, g in o.groupby("band"):
    print(f"band {b}: {len(g)} random QSOs; with >1 objectid {np.mean(g.n_oid > 1):.3f}")
    print("  epoch gain   mean %.3f  median %.3f  p90 %.3f" % (g.epoch_gain.mean(), g.epoch_gain.median(), g.epoch_gain.quantile(.9)))
    print("  night gain   mean %.3f  median %.3f  p90 %.3f  frac>0.25 %.3f  frac>1 %.3f" % (g.night_gain.mean(),
          g.night_gain.median(), g.night_gain.quantile(.9), (g.night_gain > .25).mean(), (g.night_gain > 1).mean()))
    print("  baseline gain >30 d: %.3f ; total nights gained / total nights %.3f" % ((g.base_gain > 30).mean(),
          g.nights_all.sum() / g.nights_p.sum() - 1))
