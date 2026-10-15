"""Moonlight / bright-sky handling for catalog light curves: what is salvageable.

Two effects bias catalog photometry when the sky is bright (Moon, twilight, high airmass):

1. Near the detection limit, catalog light curves keep only epochs where noise pushed the
   source over the limit, so faint epochs are biased bright (up to ~0.7 mag). This cannot
   be corrected from detections alone: such epochs are FLAGGED (``near_limit``).
2. Every source gains a small extra flux that grows with sky brightness, ~0.8-0.9 uJy per
   doubling of the sky (plus a -0.2 % per doubling scale term in r). This is CORRECTED
   (``mag_corr``) for g and r.

Validated on COSMOS and Coma (DR24, g and r; coefficients fitted on one field and tested
on the other): see README "Moonlight and bright sky". Needs only ztf_lc_bulk columns
(maglimit, magzp from ``ztf-lc-bulk exposures``); no Moon geometry.

Definitions
-----------
sky_ratio R  : image sky / typical dark sky of the same quadrant and filter (1 = dark,
               ~10 under a full Moon). Sky = (F_lim/5)^2 / (uJy per ADU), the sky counts
               under the PSF implied by the image's 5-sigma limit; typical dark = 25th
               percentile over the run's clean images of that quadrant and filter.
mag_ref      : a light curve's normal magnitude = median over nights of the nightly median
               mag on dark-sky (R < 1.5), clean, not-near-limit epochs (>= 5 nights).
limit_margin : maglimit - mag_ref of each epoch's image.
near_limit   : limit_margin < 1.3 mag (or no image match). Completeness of clean detections
               reaches 99% of its bright-source plateau at margin 1.2-1.4 mag in every
               sky-brightness class.
mag_corr     : flux F' = (F - k log2 R) / (1 + s log2 R) for R > 1, else unchanged.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ZP_UJY = 23.9
NEAR_LIMIT_MARGIN = 1.3
DARK_RATIO = 1.5
# (k [uJy per doubling of sky], s [fraction per doubling]); mean of COSMOS and Coma fits
CORRECTION = {"g": (0.80, 0.0), "r": (0.89, -0.0021)}
KEY = ["field", "rcid", "fid"]


def sky_proxy(maglimit, magzp) -> np.ndarray:
    """Sky counts under the PSF (ADU, up to a constant gain) from the 5-sigma limit."""
    flim = 10 ** (-0.4 * (np.asarray(maglimit, float) - ZP_UJY))
    return (flim / 5) ** 2 / 10 ** (-0.4 * (np.asarray(magzp, float) - ZP_UJY))


def sky_ratio(exp: pd.DataFrame, pct: float = 25.0) -> pd.Series:
    """Sky ratio R for every row of an exposures table (see module docstring).

    Images without magzp use the median magzp of their quadrant and filter.
    """
    zp = exp["magzp"].fillna(exp.groupby(KEY)["magzp"].transform("median"))
    S = pd.Series(sky_proxy(exp["maglimit"], zp), index=exp.index)
    clean = exp["infobits"] == 0
    s0 = S[clean].groupby([exp.loc[clean, k] for k in KEY]).quantile(pct / 100).rename("s0")
    s0 = exp[KEY].join(s0, on=KEY)["s0"]
    return (S / s0).rename("sky_ratio")


def reference_mag(lc: pd.DataFrame, margin: float | None = NEAR_LIMIT_MARGIN,
                  dark_ratio: float = DARK_RATIO, min_nights: int = 5, n_iter: int = 2) -> pd.Series:
    """Normal magnitude per objectid from clean dark-sky epochs (see module docstring).

    ``lc``: long-form light curves with objectid, mag, hmjd, catflags, sky_ratio, maglimit.
    With ``margin`` set, the estimate is iterated ``n_iter`` times, each time dropping
    near-limit epochs (which are biased bright).
    """
    d = lc[(lc["catflags"] == 0) & (lc["sky_ratio"] < dark_ratio)]

    def med(x):
        night = np.floor(x["hmjd"].to_numpy() - 0.3).astype(int)
        pn = x.groupby([x["objectid"].to_numpy(), night])["mag"].median()
        g = pn.groupby(level=0)
        return g.median()[g.size() >= min_nights].rename("mag_ref")

    ref = med(d)
    if margin is not None:
        for _ in range(n_iter):
            m = d["objectid"].map(ref)
            ref = med(d[(d["maglimit"] - m) >= margin])
    return ref


def add_brightsky_columns(lc: pd.DataFrame, margin: float = NEAR_LIMIT_MARGIN,
                          correction: dict | None = None) -> pd.DataFrame:
    """Add sky_ratio-based flags and corrected magnitudes to long-form light curves.

    ``lc`` must come from ``load(..., long=True, exposures=True)`` (columns band, objectid,
    mag, hmjd, catflags, maglimit, sky_ratio). Load with ``catflags0=False`` if you want
    flags on every epoch; the reference uses clean epochs only either way.

    Adds: mag_ref, limit_margin, near_limit (bool), mag_corr. ``mag_corr`` equals ``mag``
    for bands without a calibrated correction (i), for R <= 1, and is NaN if the corrected
    flux is not positive. Light curves with fewer than 5 dark nights get NaN mag_ref and
    near_limit = True.
    """
    corr = CORRECTION if correction is None else correction
    out = lc.copy()
    ref = reference_mag(out, margin=margin)
    out["mag_ref"] = out["objectid"].map(ref)
    out["limit_margin"] = out["maglimit"] - out["mag_ref"]
    out["near_limit"] = ~(out["limit_margin"] >= margin)          # NaN margin -> flagged
    F = 10 ** (-0.4 * (out["mag"].to_numpy(float) - ZP_UJY))
    L = np.log2(np.maximum(out["sky_ratio"].to_numpy(float), 1.0))
    L = np.where(np.isfinite(L), L, 0.0)
    k = out["band"].map(lambda b: corr.get(b, (0.0, 0.0))[0]).to_numpy(float)
    s = out["band"].map(lambda b: corr.get(b, (0.0, 0.0))[1]).to_numpy(float)
    Fc = (F - k * L) / (1 + s * L)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["mag_corr"] = np.where(Fc > 0, ZP_UJY - 2.5 * np.log10(Fc), np.nan)
    return out
