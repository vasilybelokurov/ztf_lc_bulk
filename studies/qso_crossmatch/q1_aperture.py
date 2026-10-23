"""Q1: optimal crossmatch radius between SDSS/DESI QSOs and ZTF DR23 objectids.

Density profile of ZTF objectids around QSO positions = core (the QSO itself, width set
by astrometric errors) + background. Near a real source the background is NOT the sky
average: ZTF reference catalogues cannot resolve a second source within ~1 PSF FWHM, so
the density at 1.2-4" falls below the sky value (measured with positions shifted 60").
Estimators:
  floor   = real density at 1.5-2.5"  (upper limit on chance pairs at r < 1.5")
  core(R) = real(<R) - floor * pi R^2;  total core = core(2")
  completeness C(R) = core(R)/core(2");  contamination F(R) = floor*pi R^2 / real(<R)
"""
import os, numpy as np, pandas as pd, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
BLUE, ORANGE, AQUA, YEL, MAG, INK, MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#0b0b0b", "#8a8984"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED,
                     "ytick.color": MUTED, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": "#e6e5e0", "grid.linewidth": 0.6, "lines.linewidth": 2})
CATS = [c for c in ["sdss", "desi"] if os.path.exists(f"{c}_shift.parquet")]
LABEL = {"sdss": "SDSS DR16Q", "desi": "DESI DR1"}
e = np.r_[np.arange(0, 1.0, 0.05), np.arange(1.0, 5.01, 0.1)]; mid = 0.5 * (e[1:] + e[:-1]); area = np.pi * np.diff(e ** 2)
Rgrid = np.arange(0.1, 3.001, 0.05)

def analyse(sep_real, sep_shift, nq):
    a = np.histogram(sep_real, e)[0]; b = np.histogram(sep_shift, e)[0]
    fl = (mid > 1.5) & (mid < 2.5); floor = a[fl].sum() / area[fl].sum()       # pairs / arcsec^2 (all QSOs)
    sky = b[mid > 1].sum() / area[mid > 1].sum()
    s = np.sort(sep_real)
    real_lt = np.searchsorted(s, Rgrid)
    core = real_lt - floor * np.pi * Rgrid ** 2
    tot = np.searchsorted(s, 2.0) - floor * np.pi * 4
    return dict(dens=a / area / nq, dens_shift=b / area / nq, floor=floor / nq, sky=sky / nq,
                C=core / tot, F=floor * np.pi * Rgrid ** 2 / np.maximum(real_lt, 1),
                F_naive=sky * np.pi * Rgrid ** 2 / np.maximum(real_lt, 1), core_per_qso=tot / nq)
res, rows = {}, []
for cat in CATS:
    re = pd.read_parquet(f"{cat}_real.parquet"); sh = pd.read_parquet(f"{cat}_shift.parquet")
    nq = int(open(f"{cat}_real.nqso").read())
    for f, band in [(1, "g"), (2, "r")]:
        r = analyse(re.sep[re.filterid == f].values, sh.sep[sh.filterid == f].values, nq)
        res[cat, band] = r
        near = re[re.filterid == f].groupby("qid").sep.min()
        for R in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]:
            i = np.argmin(np.abs(Rgrid - R))
            rows.append(dict(cat=cat, band=band, R=R, completeness=r["C"][i], contamination=r["F"][i],
                             contamination_naive_sky=r["F_naive"][i], f_qso_with_match=(near < R).sum() / nq))
        print(f"{cat} {band}: QSOs {nq}, ZTF objectids of the QSO per QSO {r['core_per_qso']:.3f}; "
              f"density near QSO (1.5-2.5\") {r['floor']*3600**2:.0f}/deg2 vs sky {r['sky']*3600**2:.0f}/deg2 "
              f"(ratio {r['floor']/r['sky']:.2f})")
t = pd.DataFrame(rows); pd.set_option("display.width", 220)
print(t.round(4).to_string(index=False)); t.to_csv("q1_table.csv", index=False)

# per magnitude (QSO catalogue r): radii enclosing 90/95/99/99.5 % of genuine matches
mrows = []; mbins = [(14, 19), (19, 20), (20, 20.5), (20.5, 21), (21, 21.5), (21.5, 23)]
for cat in CATS:
    re = pd.read_parquet(f"{cat}_real.parquet"); sh = pd.read_parquet(f"{cat}_shift.parquet")
    nqm = re.drop_duplicates("qid").rsdss
    for f, band in [(1, "g"), (2, "r")]:
        for lo, hi in mbins:
            sa = re[(re.filterid == f) & re.rsdss.between(lo, hi)].sep.values
            sb = sh[(sh.filterid == f) & sh.rsdss.between(lo, hi)].sep.values
            if sa.size < 1000: continue
            r = analyse(sa, sb, 1)
            q = {f"R{int(p*1000)/10:g}": Rgrid[np.argmax(r["C"] >= p)] for p in (0.9, 0.95, 0.99, 0.995)}
            i1 = np.argmin(np.abs(Rgrid - 1.0))
            mrows.append(dict(cat=cat, band=band, mag=f"{lo}-{hi}", n_pairs=sa.size, **q,
                              C_1arcsec=r["C"][i1], F_1arcsec=r["F"][i1]))
m = pd.DataFrame(mrows); print(m.round(4).to_string(index=False)); m.to_csv("q1_mag_table.csv", index=False)

fig, ax = plt.subplots(1, 3, figsize=(15, 4.4))
for cat, ls in [(c, l) for c, l in [("sdss", "-"), ("desi", "--")] if c in CATS]:
    for band, col in [("g", AQUA), ("r", BLUE)]:
        r = res[cat, band]
        ax[0].semilogy(mid, r["dens"] * 3600 ** 2, ls, color=col, label=f"{LABEL[cat]} {band}: around QSOs")
        ax[0].semilogy(mid, r["dens_shift"] * 3600 ** 2, ls, color=ORANGE if band == "r" else YEL, lw=1.2,
                       label=f"{LABEL[cat]} {band}: 60\" away (sky)")
        ax[1].plot(Rgrid, 100 * r["C"], ls, color=col, label=f"{LABEL[cat]} {band}")
        ax[2].semilogy(Rgrid, 100 * r["F"], ls, color=col, label=f"{LABEL[cat]} {band}")
ax[0].axvspan(1.2, 4, color="#e6e5e0", alpha=.5, lw=0); ax[0].text(2.6, 2e6, "exclusion zone:\nneighbours not\nresolved by ZTF",
                                                                 ha="center", fontsize=8, color=MUTED)
ax[0].set(xlabel="separation QSO – ZTF objectid [arcsec]", ylabel="ZTF objectids per deg² (per QSO)",
          title="Density of ZTF objectids around QSOs", xlim=(0, 5))
ax[1].set(xlabel="match radius R [arcsec]", ylabel="% of genuine ZTF matches inside R",
          title="Completeness", ylim=(80, 100.5), xlim=(0, 2.5))
ax[2].set(xlabel="match radius R [arcsec]", ylabel="% of pairs inside R that are chance (upper limit)",
          title="Contamination", xlim=(0, 2.5), ylim=(1e-3, 10))
for a in ax[1:]: a.axvline(1.0, color=MUTED, lw=1, ls=":")
ax[0].legend(fontsize=7, frameon=False, loc="upper right"); ax[1].legend(fontsize=7.5, frameon=False, loc="lower right")
ax[2].legend(fontsize=7.5, frameon=False, loc="lower right")
fig.tight_layout(); fig.savefig("fig_q1_aperture.png", dpi=150)

fig, ax = plt.subplots(1, len(CATS), figsize=(6 * len(CATS), 4.2), squeeze=False)
cols = [BLUE, AQUA, YEL, ORANGE, MAG, "#6250d6"]
for k, cat in enumerate(CATS):
    re = pd.read_parquet(f"{cat}_real.parquet"); sh = pd.read_parquet(f"{cat}_shift.parquet")
    for (lo, hi), col in zip(mbins, cols):
        sa = re[(re.filterid == 1) & re.rsdss.between(lo, hi)].sep.values
        if sa.size < 1000: continue
        r = analyse(sa, sh[(sh.filterid == 1) & sh.rsdss.between(lo, hi)].sep.values, 1)
        ax[0, k].plot(Rgrid, 100 * r["C"], color=col, label=f"r = {lo}–{hi}")
    ax[0, k].set(xlabel="match radius R [arcsec]", ylabel="% of genuine matches inside R", xlim=(0, 2), ylim=(70, 100.5),
                 title=f"{LABEL[cat]}, ZTF g (worst band): completeness vs QSO mag")
    ax[0, k].axvline(1, color=MUTED, lw=1, ls=":"); ax[0, k].legend(fontsize=8, frameon=False, loc="lower right")
fig.tight_layout(); fig.savefig("fig_q1_mag.png", dpi=150)
