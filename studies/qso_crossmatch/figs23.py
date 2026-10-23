"""Q2/Q3 figures (fig_q2.png, fig_q3.png) and the Q3 index-level numbers for SDSS and DESI.
Needs {sdss,desi}_real.parquet, q2_astrometry.csv, q2_gaia.csv, q2_lc_resid.csv,
q3_lc.parquet, q3_merge.parquet."""
import numpy as np, pandas as pd, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
BLUE, ORANGE, AQUA, YEL, MAG, INK, MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#0b0b0b", "#8a8984"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED,
                     "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": "#e6e5e0", "grid.linewidth": 0.6, "lines.linewidth": 2})
def prim_of(cat, R=1.0):
    p = pd.read_parquet(f"{cat}_real.parquet"); p = p[(p.sep < R) & (p.filterid < 3)]
    p = p.sort_values(["qid", "filterid", "nepochs", "sep", "objectid"], ascending=[1, 1, 0, 1, 1])
    p["rank"] = p.groupby(["qid", "filterid"]).cumcount(); return p
out = {}
for cat in ["sdss", "desi"]:
    p = prim_of(cat); pr = p[p["rank"] == 0]
    g = pr[pr.filterid == 1].set_index("qid"); r = pr[pr.filterid == 2].set_index("qid")
    gr = g.join(r, lsuffix="_g", rsuffix="_r", how="inner")
    gr["dgr"] = np.hypot(gr.dra_g - gr.dra_r, gr.ddec_g - gr.ddec_r); gr["same"] = gr.fieldid_g == gr.fieldid_r
    pp = p.merge(pr[["qid", "filterid", "objectid", "fieldid", "rcid", "nepochs"]], on=["qid", "filterid"], suffixes=("", "_p"))
    ex = pp[pp["rank"] > 0].copy()
    ex["kind"] = np.where(ex.fieldid != ex.fieldid_p, "other field",
                 np.where(ex.rcid == ex.rcid_p, "same field + quadrant", "same field, other quadrant"))
    near = p.sort_values(["qid", "filterid", "sep"]).groupby(["qid", "filterid"]).head(1)
    m = near.merge(pr, on=["qid", "filterid"], suffixes=("_n", "_l")); dis = m.objectid_n != m.objectid_l
    nqb = pr.groupby("filterid").size()
    print(f"\n{cat.upper()}: g&r both matched {len(gr)}, same field {gr.same.mean():.3f}; g-r sep p50/p90/p99 "
          f"{np.round(np.percentile(gr.dgr, [50, 90, 99]), 3)}; same-field only {np.round(np.percentile(gr.dgr[gr.same], [50, 90, 99]), 3)}")
    print("  frac QSO-band with extra oids:", (ex.groupby("filterid").qid.nunique() / nqb).round(3).to_dict())
    print("  extras by kind:", ex.groupby("kind").size().to_dict())
    print("  extra nepochs / primary (median):", (ex.nepochs / ex.nepochs_p).groupby(ex.kind).median().round(3).to_dict())
    print(f"  nearest==longest {1 - dis.mean():.3f}; when not: nearest has median {np.median(m.nepochs_n[dis] / m.nepochs_l[dis]):.3f} "
          f"of the epochs (p90 {np.percentile(m.nepochs_n[dis] / m.nepochs_l[dis], 90):.2f}); sep nearest {m.sep_n[dis].median():.3f}\" "
          f"longest {m.sep_l[dis].median():.3f}\"")
    out[cat] = dict(gr=gr, ex=ex, m=m, dis=dis)

# ---- Q2 figure
q2 = pd.read_csv("q2_astrometry.csv"); res = pd.read_csv("q2_lc_resid.csv")
fig, ax = plt.subplots(1, 3, figsize=(15, 4.4))
bins = np.arange(0, 1.01, 0.02)
for cat, col in [("sdss", BLUE), ("desi", ORANGE)]:
    d = out[cat]["gr"]; d = d[d.same]
    ax[0].hist(d.dgr, bins=bins, histtype="step", lw=2, color=col, density=True,
               label=f"{'SDSS DR16Q' if cat=='sdss' else 'DESI DR1'} (same field), median {d.dgr.median():.2f}\"")
ax[0].set(xlabel="separation between g and r objectid positions [arcsec]", ylabel="probability density",
          title="g vs r objectid positions (longest per band)")
ax[0].legend(frameon=False, fontsize=8)
x = [18.5, 19.5, 20.25, 20.75, 21.5]
gq = pd.read_csv("q2_gaia.csv"); gq = gq[gq.ref == "Gaia"]; xg = [19.0, 19.75, 20.25]
ax[1].plot(x, q2.sig_gminusr, "o-", color=BLUE, label="σ of g – r position difference (all SDSS QSOs)")
ax[1].plot(xg, gq.sig_g, "^:", color=AQUA, lw=1.5, label="σ_g: g objectid vs Gaia DR3")
ax[1].plot(xg, gq.sig_r, "v:", color=MAG, lw=1.5, label="σ_r: r objectid vs Gaia DR3")
ax[1].set(xlabel="SDSS r [mag]", ylabel="arcsec (per axis)", title="Offsets are ZTF centroid noise", ylim=(0, 0.25))
ax[1].legend(frameon=False, fontsize=8)
xc = [0.05, 0.15, 0.25, 0.4, 0.625, 0.875]
ax[2].errorbar(xc, res.dcorr_med, yerr=res.dcorr_err, fmt="o-", color=BLUE, capsize=3,
               label="Δ corr(g, r) same-night, vs QSOs of same mag")
ax[2].errorbar(xc, res.dcol_med, yerr=res.dcol_err, fmt="s--", color=ORANGE, capsize=3,
               label="Δ mean colour (ZTF g–r minus SDSS g–r) [mag]")
ax[2].axhline(0, color=MUTED, lw=1)
for i, n in enumerate(res.N): ax[2].text(xc[i], 0.035, f"{n}", ha="center", fontsize=7.5, color=MUTED)
ax[2].set(xlabel="separation between g and r objectid positions [arcsec]", ylabel="difference from same-magnitude QSOs",
          title="Effect on the light curves (SDSS)", ylim=(-0.08, 0.05))
ax[2].legend(frameon=False, fontsize=8, loc="lower left")
fig.tight_layout(); fig.savefig("fig_q2.png", dpi=150)

# ---- Q3 figure
mg = pd.read_parquet("q3_merge.parquet"); mg["night_gain"] = mg.nights_all / mg.nights_p.clip(lower=1) - 1
q3 = pd.read_parquet("q3_lc.parquet")
fig, ax = plt.subplots(1, 4, figsize=(19, 4.4))
kinds = ["other field", "same field + quadrant", "same field, other quadrant"]
for k, (cat, col) in enumerate([("sdss", BLUE), ("desi", ORANGE)]):
    c = out[cat]["ex"].kind.value_counts().reindex(kinds).fillna(0)
    ax[0].barh(np.arange(3) + 0.38 * k, c.values, height=0.36, color=col, label="SDSS DR16Q" if cat == "sdss" else "DESI DR1")
    for i, v in enumerate(c.values): ax[0].text(v, i + 0.38 * k, f" {int(v):,}", va="center", fontsize=8, color=INK)
ax[0].set_xscale("log"); ax[0].set_yticks(np.arange(3) + 0.19, kinds); ax[0].invert_yaxis()
ax[0].set(xlabel="extra objectids within 1\" (g + r)", title="What the extra objectids are"); ax[0].legend(frameon=False, fontsize=8)
for cat, col in [("sdss", BLUE), ("desi", ORANGE)]:
    m, dis = out[cat]["m"], out[cat]["dis"]
    ax[1].hist(np.log10((m.nepochs_n[dis] / m.nepochs_l[dis]).clip(1e-3)), bins=np.linspace(-3, 0, 61), histtype="step",
               lw=2, color=col, density=True, label=f"{'SDSS' if cat=='sdss' else 'DESI'}: nearest ≠ longest in {100*dis.mean():.0f}%")
ax[1].set(xlabel="log10( epochs of nearest / epochs of longest )", ylabel="density",
          title="If you took the nearest objectid instead"); ax[1].legend(frameon=False, fontsize=8)
oo = q3[q3.kind == "other field"]
ax[2].hist(oo.offset.dropna(), bins=np.arange(-0.3, 0.301, 0.01), color=BLUE)
ax[2].set(xlabel="mag(extra) – mag(longest), same-night epochs [mag]", ylabel="objectids (SDSS, r)",
          title=f"Same QSO: zero-point offset median {oo.offset.median():+.3f} mag")
for b, col in [("g", AQUA), ("r", BLUE)]:
    s = mg[mg.band == b]
    ax[3].hist(np.clip(s.night_gain, 0, 1.5), bins=np.arange(0, 1.51, 0.025), histtype="step", lw=2, color=col,
               label=f"{b}: median +{100*s.night_gain.median():.1f}%, mean +{100*s.night_gain.mean():.1f}%")
ax[3].set_yscale("log"); ax[3].set(xlabel="extra distinct nights from merging / nights of longest",
                                   ylabel="QSOs (SDSS, 40k per band)", title="What merging all objectids adds")
ax[3].legend(frameon=False, fontsize=8)
fig.tight_layout(); fig.savefig("fig_q3.png", dpi=150)
