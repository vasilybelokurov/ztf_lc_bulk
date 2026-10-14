"""load(): join to targets, catflags cut, primary selection, long format.

Builds a run directory by hand (match.parquet + lc/*.parquet) with known content.
"""

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ztf_lc_bulk import load
from ztf_lc_bulk.load import filter_epochs


def lc_table(rows):
    """rows: list of (objectid, filterid, fieldid, hmjd list, catflags list)."""
    cols = {k: [] for k in ("objectid", "filterid", "fieldid", "rcid", "objra", "objdec", "nepochs",
                            "hmjd", "mag", "magerr", "clrcoeff", "catflags")}
    for oid, fid, field, h, cf in rows:
        cols["objectid"].append(oid); cols["filterid"].append(fid); cols["fieldid"].append(field)
        cols["rcid"].append(0); cols["objra"].append(150.0); cols["objdec"].append(2.0)
        cols["nepochs"].append(len(h)); cols["hmjd"].append(list(map(float, h)))
        cols["mag"].append([18.0 + 0.01 * i for i in range(len(h))])
        cols["magerr"].append([0.02] * len(h)); cols["clrcoeff"].append([0.0] * len(h))
        cols["catflags"].append(cf)
    return pa.table({**cols,
                     "filterid": pa.array(cols["filterid"], pa.int8()),
                     "fieldid": pa.array(cols["fieldid"], pa.int16()),
                     "rcid": pa.array(cols["rcid"], pa.int8()),
                     "objra": pa.array(cols["objra"], pa.float32()),
                     "objdec": pa.array(cols["objdec"], pa.float32()),
                     "hmjd": pa.array(cols["hmjd"], pa.list_(pa.float64())),
                     "mag": pa.array(cols["mag"], pa.list_(pa.float32())),
                     "magerr": pa.array(cols["magerr"], pa.list_(pa.float32())),
                     "clrcoeff": pa.array(cols["clrcoeff"], pa.list_(pa.float32())),
                     "catflags": pa.array(cols["catflags"], pa.list_(pa.int32()))})


@pytest.fixture
def run(tmp_path):
    # src 1: two g objectids (10: 4 epochs, 2 flagged -> 2 clean; 11: 3 clean), one r (20)
    # src 2: shares objectid 11 with src 1 (blend), plus objectid 30 with all epochs flagged
    (tmp_path / "lc").mkdir()
    pq.write_table(lc_table([
        (10, 1, 468, [1, 2, 3, 4], [0, 32768, 0, 32768]),
        (11, 1, 1462, [5, 6, 9], [0, 0, 0]),
    ]), tmp_path / "lc" / "file_000001.parquet")
    pq.write_table(lc_table([
        (20, 2, 468, [1, 50], [0, 0]),
        (30, 2, 468, [7, 8], [32768, 32768]),
    ]), tmp_path / "lc" / "file_000002.parquet")
    pq.write_table(pa.table({
        "src_id": [1, 1, 1, 2, 2], "objectid": [10, 11, 20, 11, 30],
        "filterid": pa.array([1, 1, 2, 1, 2], pa.int16()),
        "sep_arcsec": [0.1, 0.5, 0.2, 0.9, 0.3]}), tmp_path / "match.parquet")
    return tmp_path


def test_filter_epochs_basic():
    t = lc_table([(1, 1, 1, [1, 2, 3], [0, 4, 0]), (2, 1, 1, [4], [4])])
    f = filter_epochs(t, np.array([True, False, True, False]))
    assert f["hmjd"].to_pylist() == [[1.0, 3.0], []]
    assert f["nepochs"].to_pylist() == [2, 0]


def test_load_raw_no_cuts(run):
    df = load(run, catflags0=False)
    assert len(df) == 5
    r = df.set_index(["src_id", "objectid"])
    assert r.loc[(1, 10), "nepochs"] == 4 and r.loc[(1, 10), "band"] == "g"
    assert r.loc[(1, 20), "baseline_days"] == 49.0
    assert list(r.loc[(2, 11), "hmjd"]) == [5.0, 6.0, 9.0]


def test_load_catflags_and_min_epochs(run):
    df = load(run)  # catflags0=True, min_epochs=1 drops objectid 30
    assert set(zip(df.src_id, df.objectid)) == {(1, 10), (1, 11), (1, 20), (2, 11)}
    assert list(df[df.objectid == 10]["hmjd"].iloc[0]) == [1.0, 3.0]


def test_load_primary(run):
    df = load(run, primary=True)
    got = {(s, b): o for s, b, o in zip(df.src_id, df.band, df.objectid)}
    # src 1 g: 11 has 3 clean epochs > 10 with 2 clean
    assert got == {(1, "g"): 11, (1, "r"): 20, (2, "g"): 11}


def test_load_primary_tiebreak_baseline(run):
    # without cuts objectid 10 has 4 epochs > 3 -> wins
    df = load(run, primary=True, catflags0=False, src_ids=[1], bands=["g"])
    assert df.objectid.tolist() == [10]


def test_load_long(run):
    df = load(run, long=True, src_ids=[1])
    assert len(df) == 2 + 3 + 2
    assert df.groupby("objectid").size().to_dict() == {10: 2, 11: 3, 20: 2}
    assert np.issubdtype(df["hmjd"].dtype, np.floating)
