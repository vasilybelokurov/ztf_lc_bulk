"""End-to-end tests against WSDB and IRSA. Skipped by default.

Run:  python -m pytest -m network -v
Needs a WSDB account with read access to koposov.ztf_23_objs / ztf_23_files.
Downloads one DR23 parquet file (~3 MB) and queries the IRSA LC API.
"""

import numpy as np
import pytest

from ztf_lc_bulk import load, match_local, match_wsdb_table, run_extract, save_match, verify
from ztf_lc_bulk.validate import api_spotcheck

pytestmark = pytest.mark.network

# COSMOS: a 30" box around the field centre, a small but realistic target list
RA0, DEC0, HALF = 150.12, 2.21, 30 / 3600


@pytest.fixture(scope="module")
def targets():
    import sqlutilpy
    ids, ra, dec = sqlutilpy.get(
        f"select id, ra, dec from vasily.cosmos2020_classic "
        f"where ra between {RA0 - HALF} and {RA0 + HALF} and dec between {DEC0 - HALF} and {DEC0 + HALF}",
        db="wsdb")
    assert ids.size > 100
    return ids, ra, dec


def test_local_upload_equals_table_match(targets):
    ids, ra, dec = targets
    a = match_local(ids, ra, dec, radius_arcsec=1.0)
    b = match_wsdb_table("vasily.cosmos2020_classic", where=(
        f"c.ra between {RA0 - HALF} and {RA0 + HALF} and c.dec between {DEC0 - HALF} and {DEC0 + HALF}"))
    key = lambda t: sorted(zip(t["src_id"].to_pylist(), t["objectid"].to_pylist()))
    assert a.num_rows > 0 and key(a) == key(b)
    assert np.all(a["sep_arcsec"].to_numpy() <= 1.0 + 1e-9)


def test_extract_verify_load_and_api(targets, tmp_path):
    ids, ra, dec = targets
    m = match_local(ids, ra, dec, radius_arcsec=1.0)
    # keep it small: only the i-band file(s), the smallest
    m = m.filter(m["filterid"].to_numpy() == 3)
    save_match(m, tmp_path / "match.parquet")
    s = run_extract(tmp_path / "match.parquet", tmp_path, workers=2, log=lambda *_: None)
    assert s["failed"] == 0 and s["missing"] == 0
    v = verify(tmp_path / "match.parquet", tmp_path)
    assert v["pass"], v
    df = load(tmp_path, primary=True)
    assert df.groupby(["src_id", "band"]).size().max() == 1
    r = api_spotcheck(tmp_path, n=6, threads=3)
    assert r["pass"], r["table"]


def test_exposures_end_to_end(targets, tmp_path):
    """build_exposures on a small i-band run: every epoch matches an image, limitmag
    present on all clean epochs and equal to the LC API's value per epoch."""
    from ztf_lc_bulk import build_exposures, nondetections
    from ztf_lc_bulk.validate import fetch_api
    ids, ra, dec = targets
    m = match_local(ids, ra, dec, radius_arcsec=1.0)
    m = m.filter(m["filterid"].to_numpy() == 3)
    save_match(m, tmp_path / "match.parquet")
    s = run_extract(tmp_path / "match.parquet", tmp_path, workers=2, log=lambda *_: None)
    assert s["failed"] == 0
    b = build_exposures(tmp_path, log=lambda *_: None)
    assert b["n_images"] > 0
    df = load(tmp_path, long=True, catflags0=False, exposures=True)
    assert (df["expid"] >= 0).all()
    assert df.loc[df["catflags"] == 0, "limitmag"].notna().all()
    for oid in df["objectid"].drop_duplicates().sample(2, random_state=0):
        api = fetch_api(oid, collection="ztf_dr23")
        j = df[df["objectid"] == oid].merge(api[["expid", "limitmag"]], on="expid", suffixes=("", "_api"))
        assert len(j) == (df["objectid"] == oid).sum()
        assert np.allclose(j["limitmag"], j["limitmag_api"], atol=1e-4)
    nd = nondetections(tmp_path, columns=["limitmag"])
    assert (nd["edge_dist"] >= 10).all()
