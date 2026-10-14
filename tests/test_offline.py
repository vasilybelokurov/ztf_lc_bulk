"""Offline tests: synthetic DR23-like parquet files in a fake local mirror.

Run:  python -m pytest -q tests/test_offline.py
"""

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ztf_lc_bulk.extract import extract_file, sort_epochs
from ztf_lc_bulk.pipeline import plan, run_extract, verify

RNG = np.random.default_rng(42)


def make_file(path, fileid, n_obj, filterid=2, fieldid=468, rcid=8):
    """Write a synthetic DR23-style file with unsorted epochs; return its truth table."""
    oids = fileid * 10**6 + np.arange(n_obj)
    nep = RNG.integers(1, 60, n_obj)
    rows = {k: [] for k in ("hmjd", "mag", "magerr", "clrcoeff", "catflags")}
    for n in nep:
        t = RNG.uniform(58200, 60300, n)  # deliberately unsorted
        rows["hmjd"].append(t)
        rows["mag"].append((18 + 0.001 * (t - 58000)).astype(np.float32))  # mag encodes time
        rows["magerr"].append(np.full(n, 0.02, np.float32))
        rows["clrcoeff"].append(np.zeros(n, np.float32))
        rows["catflags"].append(np.where(RNG.random(n) < 0.05, 32768, 0).astype(np.int32))
    tab = pa.table({
        "objectid": oids, "filterid": np.full(n_obj, filterid, np.int8),
        "fieldid": np.full(n_obj, fieldid, np.int16), "rcid": np.full(n_obj, rcid, np.int8),
        "objra": RNG.uniform(149, 151, n_obj).astype(np.float32),
        "objdec": RNG.uniform(1, 3, n_obj).astype(np.float32), "nepochs": nep.astype(np.int64),
        "hmjd": pa.array(rows["hmjd"], pa.list_(pa.float64())),
        "mag": pa.array(rows["mag"], pa.list_(pa.float32())),
        "magerr": pa.array(rows["magerr"], pa.list_(pa.float32())),
        "clrcoeff": pa.array(rows["clrcoeff"], pa.list_(pa.float32())),
        "catflags": pa.array(rows["catflags"], pa.list_(pa.int32())),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tab, path)
    return tab


@pytest.fixture
def mirror(tmp_path):
    root = tmp_path / "mirror"
    files = {}
    for fid in (11, 12, 13):
        rel = f"lc_dr23/0/field000468/ztf_000468_zr_c{fid:02d}_q1_dr23.parquet"
        files[fid] = (rel, make_file(root / rel, fid, 300))
    return root, files


def make_match(files, frac=0.3, dup_src=True):
    """Match table: a random subset of objects per file, some objects matched twice."""
    cols = {k: [] for k in ("src_id", "objectid", "filterid", "fieldid", "rcid", "fileid",
                            "nepochs", "sep_arcsec", "filename")}
    sid = 0
    for fid, (rel, t) in files.items():
        sel = np.flatnonzero(RNG.random(t.num_rows) < frac)
        if dup_src:
            sel = np.concatenate([sel, sel[:5]])  # two targets on one ZTF object
        for i in sel:
            cols["src_id"].append(sid); sid += 1
            cols["objectid"].append(t["objectid"][i].as_py())
            for c in ("filterid", "fieldid", "rcid", "nepochs"):
                cols[c].append(t[c][i].as_py())
            cols["fileid"].append(fid)
            cols["sep_arcsec"].append(0.2)
            cols["filename"].append(rel)
    return pa.table(cols)


def test_sort_epochs_matches_naive(mirror):
    _, files = mirror
    t = files[11][1]
    s = sort_epochs(t)
    for i in range(t.num_rows):
        h = np.array(t["hmjd"][i].as_py())
        o = np.argsort(h, kind="stable")
        assert np.array_equal(np.array(s["hmjd"][i].as_py()), h[o])
        assert np.array_equal(np.array(s["mag"][i].as_py()), np.array(t["mag"][i].as_py())[o])
        assert np.array_equal(np.array(s["catflags"][i].as_py()), np.array(t["catflags"][i].as_py())[o])


def test_sort_epochs_on_sliced_table(mirror):
    _, files = mirror
    t = files[11][1].slice(17, 40)  # non-zero list offsets
    s = sort_epochs(t)
    for i in range(t.num_rows):
        assert np.array_equal(np.array(s["hmjd"][i].as_py()), np.sort(t["hmjd"][i].as_py()))


def test_extract_selects_and_reports_missing(mirror):
    root, files = mirror
    rel, t = files[12]
    want = t["objectid"].to_numpy()[[3, 50, 299]]
    tab, missing = extract_file(root / rel, np.concatenate([want, [999]]))
    assert sorted(tab["objectid"].to_pylist()) == sorted(want.tolist())
    assert missing.tolist() == [999]
    for i in range(tab.num_rows):
        h = np.array(tab["hmjd"][i].as_py()); m = np.array(tab["mag"][i].as_py())
        assert np.all(np.diff(h) >= 0)
        assert np.allclose(m, 18 + 0.001 * (h - 58000), atol=1e-4)  # values stayed paired


def test_plan_groups_unique_objects(mirror):
    _, files = mirror
    m = make_match(files)
    p = plan(m)
    assert [f for f, _, _ in p] == [11, 12, 13]
    assert sum(o.size for *_, o in p) == np.unique(m["objectid"].to_numpy()).size


def test_pipeline_end_to_end_resume_and_failure(tmp_path, mirror):
    root, files = mirror
    m = make_match(files)
    # add a file that exists neither in the mirror nor at IRSA -> must fail, not crash
    bad = pa.table({k: [v] for k, v in dict(
        src_id=10**6, objectid=1, filterid=2, fieldid=1, rcid=0, fileid=99, nepochs=1,
        sep_arcsec=0.1, filename="lc_dr23/0/field000001/does_not_exist.parquet").items()})
    m = pa.concat_tables([m, bad.cast(m.schema)])
    mp = tmp_path / "match.parquet"; pq.write_table(m, mp)
    out = tmp_path / "out"

    import ztf_lc_bulk.fetch as fetch
    def no_network(*a, **k):
        raise fetch.FetchError("HTTP 404 (fatal) offline test")
    orig = fetch.download; fetch.download = no_network
    try:
        s1 = run_extract(mp, out, workers=3, mirror=root, log=lambda *_: None)
        s2 = run_extract(mp, out, workers=3, mirror=root, log=lambda *_: None)
    finally:
        fetch.download = orig
    assert s1["ok"] == 3 and s1["failed"] == 1
    assert s2["ok"] == 0 and s2["failed"] == 1        # done files skipped, failed retried
    recs = [json.loads(x) for x in open(out / "manifest.jsonl")]
    assert sum(r["status"] == "failed" for r in recs) == 2
    assert all(f.exists() for f in root.rglob("*.parquet"))  # mirror files never deleted

    # verify against the good part of the match table only
    good = m.filter(pa.compute.not_equal(m["fileid"], 99))
    gp = tmp_path / "good.parquet"; pq.write_table(good, gp)
    v = verify(gp, out)
    assert v["pass"], v
    assert v["n_lc_rows"] == np.unique(good["objectid"].to_numpy()).size
