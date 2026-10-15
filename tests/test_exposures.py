"""exposures: hmjd prediction, epoch matching, footprint, load(exposures=True), nondetections.

Offline: synthetic image tables with a fixed Earth vector, so no IRSA/WSDB access.
The astropy cross-check is skipped if astropy is missing.
"""

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ztf_lc_bulk import load, nondetections
from ztf_lc_bulk.exposures import edge_distance, match_epochs, predict_hmjd, unit_vector

RVEC = np.array([0.004, -0.003, 0.0015])  # light-days; |r| ~ 0.0052 d ~ 450 s, like Earth's
HALF = 0.43  # deg, half-width of a synthetic quadrant


def images(field=468, rcid=0, fid=1, n=6, t0=60000.0, step_s=40.0, ra=150.0, dec=2.0, **extra):
    """n images of one quadrant, ``step_s`` apart, centred on (ra, dec)."""
    k = np.arange(n)
    df = pd.DataFrame({"field": field, "rcid": rcid, "fid": fid, "expid": 1000 * field + 100 * rcid + k,
                       "mjd_mid": t0 + k * step_s / 86400, "exptime": 30.0, "ra": ra, "dec": dec,
                       "ex": RVEC[0], "ey": RVEC[1], "ez": RVEC[2],
                       "maglimit": 20.0 + 0.1 * k, "limitmag": 20.5 + 0.1 * k, "magzp": 26.0,
                       "magzprms": 0.02, "seeing": 2.0, "airmass": 1.2, "moonillf": 0.5,
                       "infobits": 0, "programid": 1, "n_api_obj": 3})
    c = np.cos(np.radians(dec))  # corners counter-clockwise, as small offsets
    for i, (sa, sd) in enumerate([(1, 1), (-1, 1), (-1, -1), (1, -1)], start=1):
        df[f"ra{i}"] = ra + sa * HALF / c
        df[f"dec{i}"] = dec + sd * HALF
    for k_, v in extra.items():
        df[k_] = v
    return df


def hmjd_on(img_row, ra, dec):
    """Epoch time as stored in the parquet: prediction rounded to 1e-5 d."""
    return np.round(predict_hmjd(img_row["mjd_mid"], RVEC, ra, dec), 5)


def test_predict_hmjd_matches_astropy():
    pytest.importorskip("astropy")
    import astropy.units as u
    from astropy.coordinates import EarthLocation, SkyCoord
    from astropy.time import Time
    from ztf_lc_bulk.exposures import earth_helio_lightdays
    rng = np.random.default_rng(3)
    mjd = rng.uniform(58200, 60900, 50)
    ra, dec = rng.uniform(0, 360, 50), rng.uniform(-30, 90, 50)
    ours = predict_hmjd(mjd, earth_helio_lightdays(mjd), ra, dec)
    t = Time(mjd, format="mjd", scale="utc", location=EarthLocation.from_geocentric(0, 0, 0, u.m))
    ref = mjd + t.light_travel_time(SkyCoord(ra * u.deg, dec * u.deg), kind="heliocentric").to_value(u.day)
    assert np.abs(ours - ref).max() * 86400 < 0.01


def test_match_epochs_picks_right_image_and_rejects():
    exp = pd.concat([images(), images(rcid=1, t0=60000.0)], ignore_index=True)
    # object 0.4 deg from the image centre: light-travel time differs from the centre's by ~1 s
    ra, dec = 150.4, 2.3
    h = np.array([hmjd_on(exp.iloc[k], ra, dec) for k in (0, 3, 5)] + [60000.5])  # last: no image
    idx, dt = match_epochs(exp, [468] * 4, [0] * 4, [1] * 4, [ra] * 4, [dec] * 4, h)
    assert idx.tolist() == [0, 3, 5, -1]
    assert dt[:3].max() < 0.5
    # same times but on rcid 1 -> rows of the second quadrant
    idx1, _ = match_epochs(exp, [468] * 3, [1] * 3, [1] * 3, [ra] * 3, [dec] * 3, h[:3])
    assert idx1.tolist() == [6, 9, 11]
    # a quadrant with no images at all
    idx2, _ = match_epochs(exp, [999], [0], [1], [ra], [dec], h[:1])
    assert idx2.tolist() == [-1]


def test_edge_distance():
    exp = images(n=1)
    d = edge_distance(pd.concat([exp] * 4, ignore_index=True),
                      [150.0, 150.0, 150.0, 150.0], [2.0, 2.0 + HALF - 10 / 3600, 2.0 + HALF + 5 / 3600, 1.0])
    assert d[0] == pytest.approx(HALF * 3600, rel=1e-3)
    # the edge is a great circle between corners, ~0.24" north of dec + HALF at mid-edge
    assert d[1] == pytest.approx(10, abs=0.5)
    assert d[2] == pytest.approx(-5, abs=0.5)
    assert d[3] < -1000


@pytest.fixture
def run(tmp_path):
    """One g object (10) detected on images 0, 2, 3; one r object (20) on image 1 only."""
    exp = pd.concat([images(), images(fid=2, rcid=0, t0=60001.0, n=2)], ignore_index=True)
    exp.loc[5, "n_api_obj"] = 0  # g image 5: not in the release (no epochs anywhere)
    ra, dec = 150.0, 2.0  # lc_table() position
    rows = {10: (1, [hmjd_on(exp.iloc[k], ra, dec) for k in (0, 2, 3)]),
            20: (2, [hmjd_on(exp.iloc[7], ra, dec)])}
    (tmp_path / "lc").mkdir()
    n = [len(h) for _, h in rows.values()]
    pq.write_table(pa.table({
        "objectid": list(rows), "filterid": pa.array([f for f, _ in rows.values()], pa.int8()),
        "fieldid": pa.array([468, 468], pa.int16()), "rcid": pa.array([0, 0], pa.int8()),
        "objra": pa.array([ra, ra], pa.float32()), "objdec": pa.array([dec, dec], pa.float32()),
        "nepochs": n, "hmjd": pa.array([h for _, h in rows.values()], pa.list_(pa.float64())),
        "mag": pa.array([[18.0] * k for k in n], pa.list_(pa.float32())),
        "magerr": pa.array([[0.02] * k for k in n], pa.list_(pa.float32())),
        "clrcoeff": pa.array([[0.0] * k for k in n], pa.list_(pa.float32())),
        "catflags": pa.array([[0] * k for k in n], pa.list_(pa.int32()))}),
        tmp_path / "lc" / "file_000001.parquet")
    pq.write_table(pa.table({"src_id": [1, 1], "objectid": [10, 20],
                             "filterid": pa.array([1, 2], pa.int16()), "sep_arcsec": [0.1, 0.1]}),
                   tmp_path / "match.parquet")
    exp.to_parquet(tmp_path / "exposures.parquet", index=False)
    return tmp_path, exp


def test_load_with_exposures(run):
    path, exp = run
    df = load(path, long=True, exposures=True)
    g = df[df.objectid == 10]
    assert g.expid.tolist() == exp.expid.iloc[[0, 2, 3]].tolist()
    assert np.allclose(g.limitmag, exp.limitmag.iloc[[0, 2, 3]])
    assert df[df.objectid == 20].expid.tolist() == [exp.expid.iloc[7]]
    nested = load(path, exposures=True).set_index("objectid")
    assert list(nested.loc[10, "maglimit"]) == pytest.approx(list(exp.maglimit.iloc[[0, 2, 3]]))
    plain = load(path, long=True)
    assert "expid" not in plain.columns and len(plain) == len(df)


def test_load_unmatched_epoch_gets_minus_one(run):
    path, exp = run
    t = pq.read_table(path / "lc" / "file_000001.parquet").to_pydict()
    t["hmjd"][0][1] += 0.5  # move one epoch away from every image
    pq.write_table(pa.table(t, schema=pq.read_schema(path / "lc" / "file_000001.parquet")),
                   path / "lc" / "file_000001.parquet")
    g = load(path, long=True, exposures=True).query("objectid == 10")
    assert g.expid.tolist()[1] == -1 and np.isnan(g.limitmag.tolist()[1])


def test_nondetections(run):
    path, exp = run
    nd = nondetections(path, columns=["expid"])
    assert (exp.expid.to_numpy()[nd.exp_row] == nd.expid).all()
    # g: images 0..5; detected on 0, 2, 3; image 5 is not in the release -> 1 and 4
    assert sorted(nd[nd.objectid == 10].expid) == exp.expid.iloc[[1, 4]].tolist()
    # r: images 6, 7; detected on 7 -> 6
    assert nd[nd.objectid == 20].expid.tolist() == [exp.expid.iloc[6]]
    # a margin larger than the distance to the edge removes everything
    assert len(nondetections(path, edge_margin=HALF * 3600 + 1)) == 0


def test_unit_vector_norm():
    v = unit_vector(np.array([0.0, 90.0, 250.0]), np.array([0.0, 45.0, -60.0]))
    assert np.allclose(np.linalg.norm(v, axis=-1), 1)


def test_build_exposures_whole_fields(tmp_path, monkeypatch):
    """Scope of build_exposures: run quadrants only (default) vs all quadrants of the fields.
    IRSA/WSDB/astropy calls are replaced by stubs."""
    import ztf_lc_bulk.exposures as E
    field = pd.concat([images(rcid=0), images(rcid=1), images(fid=2, rcid=5)], ignore_index=True)
    ibe = field.drop(columns=["mjd_mid", "ex", "ey", "ez", "limitmag", "magzp", "magzprms",
                              "programid", "n_api_obj"]).assign(obsjd=field.mjd_mid + 2400000.5)
    asked = {}
    monkeypatch.setattr(E, "query_images", lambda f: ibe[ibe.field == f])
    monkeypatch.setattr(E, "earth_helio_lightdays", lambda m: np.tile(RVEC, (len(m), 1)))
    monkeypatch.setattr(E, "top_objects", lambda q, n: asked.setdefault("q", q).assign(objectid=1, nepochs=1))
    monkeypatch.setattr(E, "api_meta", lambda objs, coll, threads: pd.DataFrame(
        columns=E.KEY + ["expid"] + E.API_COLS + ["n_api_obj"]))
    (tmp_path / "lc").mkdir()
    pq.write_table(pa.table({"fieldid": pa.array([468], pa.int16()), "rcid": pa.array([0], pa.int8()),
                             "filterid": pa.array([1], pa.int8())}), tmp_path / "lc" / "file_000001.parquet")
    s = E.build_exposures(tmp_path, log=lambda *_: None)
    assert s["n_quadrants"] == 1 and s["n_images"] == 6 and len(asked.pop("q")) == 1
    s = E.build_exposures(tmp_path, whole_fields=True, log=lambda *_: None)
    assert s["n_quadrants"] == 3 and s["n_run_quadrants"] == 1 and s["n_images"] == 18
    assert len(asked["q"]) == 3
