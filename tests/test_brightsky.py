"""brightsky: sky ratio, reference magnitude, near-limit flag, correction (offline, synthetic)."""

import numpy as np
import pandas as pd
import pytest

from ztf_lc_bulk.brightsky import (CORRECTION, NEAR_LIMIT_MARGIN, ZP_UJY, add_brightsky_columns,
                                   reference_mag, sky_ratio)


def exposures(deltas, field=468, rcid=0, fid=1, base=21.0, magzp=26.0, infobits=0):
    """Images whose sky is brighter than the deepest by Delta mag of limit: R = 10**(0.8 Delta)."""
    d = np.asarray(deltas, float)
    return pd.DataFrame({"field": field, "rcid": rcid, "fid": fid, "maglimit": base - d,
                         "magzp": magzp, "infobits": infobits})


def test_sky_ratio_per_quadrant():
    # 4 dark images (Delta 0) + brighter ones; p25 = dark level in each quadrant
    a = exposures([0, 0, 0, 0, 0.5, 1.0, 1.25], rcid=0, base=21.0)
    b = exposures([0, 0, 0, 0, 0.5, 1.0, 1.25], rcid=1, base=20.0)   # shallower quadrant
    R = sky_ratio(pd.concat([a, b], ignore_index=True))
    expect = 10 ** (0.8 * np.array([0, 0, 0, 0, 0.5, 1.0, 1.25]))
    assert np.allclose(R[:7], expect) and np.allclose(R[7:], expect)    # each quadrant its own dark


def test_sky_ratio_ignores_flagged_images_and_fills_magzp():
    e = exposures([0, 0, 0, 0, 1.0])
    bad = exposures([2.0, 2.0, 2.0, 2.0, 2.0, 2.0], infobits=33554432)  # flagged, very bright sky
    e = pd.concat([e, bad], ignore_index=True)
    R = sky_ratio(e)
    assert R.iloc[0] == pytest.approx(1.0)                               # flagged rows not in p25
    e.loc[4, "magzp"] = np.nan
    assert sky_ratio(e).iloc[4] == pytest.approx(10 ** 0.8)              # median magzp of quadrant


def lc_frame(rows):
    """rows: (objectid, band, night, mag, maglimit, sky_ratio, catflags)."""
    d = pd.DataFrame(rows, columns=["objectid", "band", "night", "mag", "maglimit", "sky_ratio", "catflags"])
    d["hmjd"] = d.night + 0.4
    return d


def test_reference_mag_dark_clean_nights_only():
    rows = [(1, "g", n, 18.0, 21.0, 1.0, 0) for n in range(6)]          # 6 dark nights at 18.0
    rows += [(1, "g", 100 + n, 17.0, 21.0, 5.0, 0) for n in range(10)]  # bright sky: ignored
    rows += [(1, "g", 200 + n, 16.0, 21.0, 1.0, 32768) for n in range(10)]  # flagged: ignored
    rows += [(2, "g", n, 18.0, 21.0, 1.0, 0) for n in range(4)]          # only 4 nights: no ref
    ref = reference_mag(lc_frame(rows))
    assert ref.to_dict() == {1: 18.0}


def test_reference_mag_iterates_away_from_near_limit():
    # dark nights at 19.0 on deep images and 18.5 (biased bright) on shallow ones (limit 19.5)
    rows = [(1, "g", n, 19.0, 21.0, 1.0, 0) for n in range(6)]
    rows += [(1, "g", 50 + n, 18.5, 19.5, 1.0, 0) for n in range(7)]
    d = lc_frame(rows)
    assert reference_mag(d, margin=None).iloc[0] == 18.5               # biased by the shallow nights
    assert reference_mag(d).iloc[0] == 19.0                            # near-limit epochs dropped


def test_near_limit_flag():
    rows = [(1, "g", n, 18.0, 21.0, 1.0, 0) for n in range(6)]
    rows += [(1, "g", 10, 18.0, 18.0 + NEAR_LIMIT_MARGIN - 0.01, 3.0, 0),
             (1, "g", 11, 18.0, 18.0 + NEAR_LIMIT_MARGIN + 0.01, 3.0, 0),
             (2, "g", 0, 19.0, 21.0, 1.0, 0)]                          # no reference -> flagged
    out = add_brightsky_columns(lc_frame(rows))
    assert out.near_limit.tolist() == [False] * 6 + [True, False, True]
    assert out.limit_margin.iloc[0] == pytest.approx(3.0)


def test_correction_round_trip_and_scope():
    k, s = CORRECTION["r"]
    m_true, R = 18.5, np.array([0.5, 1.0, 2.0, 8.0])
    F = 10 ** (-0.4 * (m_true - ZP_UJY))
    L = np.log2(np.maximum(R, 1))
    m_obs = ZP_UJY - 2.5 * np.log10(F * (1 + s * L) + k * L)                # inject the model
    rows = [(1, "r", n, 18.5, 21.0, 1.0, 0) for n in range(6)]
    rows += [(1, "r", 10 + i, m, 21.0, r, 0) for i, (m, r) in enumerate(zip(m_obs, R))]
    rows += [(2, "i", n, 18.5, 21.0, 1.0, 0) for n in range(6)] + [(2, "i", 20, 18.4, 21.0, 8.0, 0)]
    out = add_brightsky_columns(lc_frame(rows))
    r = out[(out.objectid == 1)].iloc[6:]
    assert np.allclose(r.mag_corr, m_true, atol=1e-9)                   # exact inverse
    assert out[(out.objectid == 1)].iloc[:6].mag_corr.tolist() == [18.5] * 6   # R <= 1 untouched
    assert out[out.objectid == 2].mag_corr.tolist() == out[out.objectid == 2].mag.tolist()  # i: none


def test_correction_nan_when_flux_not_positive():
    rows = [(1, "g", n, 18.0, 21.0, 1.0, 0) for n in range(6)] + [(1, "g", 9, 26.0, 21.0, 1000.0, 0)]
    out = add_brightsky_columns(lc_frame(rows))
    assert np.isnan(out.mag_corr.iloc[-1])                               # 0.36 uJy - 8 uJy < 0


def test_read_exposures_adds_sky_ratio(tmp_path):
    from ztf_lc_bulk import read_exposures
    e = exposures([0, 0, 0, 0, 1.0]).assign(expid=range(5))
    e.to_parquet(tmp_path / "exposures.parquet", index=False)
    assert np.allclose(read_exposures(tmp_path).sky_ratio, [1, 1, 1, 1, 10 ** 0.8])
