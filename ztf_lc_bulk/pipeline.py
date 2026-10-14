"""Resumable per-file extraction driver and output verification."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import requests

from .extract import extract_file
from .fetch import resolve
from .match import estimate_download


def _part_path(out_dir: Path, fileid: int) -> Path:
    return out_dir / "lc" / f"file_{fileid:06d}.parquet"


def plan(match: pa.Table) -> list[tuple[int, str, np.ndarray]]:
    """Group unique objectids by file: [(fileid, filename, objectids), ...]."""
    fid = match.column("fileid").to_numpy()
    oid = match.column("objectid").to_numpy()
    fname = np.asarray(match.column("filename").to_pylist())
    order = np.argsort(fid, kind="stable")
    fid, oid, fname = fid[order], oid[order], fname[order]
    cuts = np.flatnonzero(np.diff(fid)) + 1
    return [(int(f[0]), str(n[0]), np.unique(o))
            for f, n, o in zip(np.split(fid, cuts), np.split(fname, cuts), np.split(oid, cuts))]


def _process(fileid, filename, oids, out_dir, raw_dir, mirror, keep_raw, session):
    t0 = time.time()
    dest = _part_path(out_dir, fileid)
    path, nbytes, temporary = resolve(filename, raw_dir, mirror, session=session)
    t1 = time.time()
    tab, missing = extract_file(path, oids)
    tmp = dest.with_suffix(".parquet.tmp")
    pq.write_table(tab, tmp, compression="zstd")
    tmp.rename(dest)
    if temporary and not keep_raw:
        path.unlink(missing_ok=True)
    return {"fileid": fileid, "filename": filename, "status": "ok",
            "n_requested": int(oids.size), "n_written": tab.num_rows,
            "n_missing": int(missing.size), "missing": missing[:20].tolist(),
            "n_epochs": int(pc.sum(tab.column("nepochs")).as_py() or 0),
            "bytes_downloaded": nbytes, "t_fetch": round(t1 - t0, 2),
            "t_extract": round(time.time() - t1, 2)}


class DownloadTooLarge(RuntimeError):
    pass


def run_extract(match_path, out_dir, workers: int = 4, mirror=None,
                keep_raw: bool = False, max_files: int | None = None,
                max_gb: float | None = None, log=print) -> dict:
    """Extract light curves for all files in a match table. Resumable.

    A file is skipped if its output part exists (parts are written via
    tmp + rename, so an existing part is complete). Every attempt is appended
    to ``out_dir/manifest.jsonl``; failures are retried on the next run.

    The expected download of the remaining files is logged before starting
    (files present in ``mirror`` are not counted). If it exceeds ``max_gb``,
    :class:`DownloadTooLarge` is raised and nothing is fetched.
    """
    out_dir = Path(out_dir)
    (out_dir / "lc").mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"
    mirror = Path(mirror) if mirror else None
    jobs = plan(pq.read_table(match_path))
    todo = [j for j in jobs if not _part_path(out_dir, j[0]).exists()]
    if max_files is not None:
        todo = todo[:max_files]
    log(f"{len(jobs)} files in plan, {len(jobs) - len(todo)} already done, "
        f"{len(todo)} to process with {workers} workers")
    to_fetch = [j[0] for j in todo if not (mirror and (mirror / j[1]).exists())]
    est = estimate_download(to_fetch)
    log(f"expected download: {est['n_files']} files, {est['download_gb']:.2f} GB, "
        f"~{est['hours_at_rate']:.1f} h at {est['rate_mbps']:g} MB/s")
    if max_gb is not None and est["download_gb"] > max_gb:
        raise DownloadTooLarge(f"expected download {est['download_gb']:.1f} GB exceeds "
                               f"max_gb={max_gb:g}; raise the limit to proceed")

    lock = threading.Lock()
    manifest = open(out_dir / "manifest.jsonl", "a")
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=max(workers, 10)))
    stats = {"ok": 0, "failed": 0, "bytes": 0, "epochs": 0, "objects": 0, "missing": 0}
    t0 = time.time()
    try:
        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(_process, *j, out_dir, raw_dir, mirror, keep_raw, session): j
                    for j in todo}
            for k, fut in enumerate(as_completed(futs), 1):
                fileid, filename, oids = futs[fut]
                try:
                    rec = fut.result()
                    stats["ok"] += 1
                    stats["bytes"] += rec["bytes_downloaded"]
                    stats["epochs"] += rec["n_epochs"]
                    stats["objects"] += rec["n_written"]
                    stats["missing"] += rec["n_missing"]
                except Exception as e:  # recorded, retried on next run
                    rec = {"fileid": fileid, "filename": filename, "status": "failed",
                           "error": f"{type(e).__name__}: {e}"}
                    stats["failed"] += 1
                rec["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                with lock:
                    manifest.write(json.dumps(rec) + "\n")
                    manifest.flush()
                el = time.time() - t0
                log(f"[{k}/{len(todo)}] {rec['status']:6s} {filename}  "
                    f"{stats['bytes'] / 1e6:8.1f} MB  {stats['bytes'] / 1e6 / max(el, 1e-9):5.2f} MB/s  "
                    f"{el:6.0f} s" + (f"  ERR {rec.get('error')}" if rec["status"] != "ok" else ""))
    finally:
        manifest.close()
    stats["seconds"] = round(time.time() - t0, 1)
    stats["n_files_plan"] = len(jobs)
    return stats


def verify(match_path, out_dir) -> dict:
    """Check the output against the match table.

    Checks: every matched objectid present exactly once; nepochs equals list
    length for every list column; hmjd non-decreasing within each light curve;
    filterid/fieldid/rcid agree with the index.
    """
    m = pq.read_table(match_path)
    mo = m.column("objectid").to_numpy()
    uo, first = np.unique(mo, return_index=True)
    lc = ds.dataset(Path(out_dir) / "lc", format="parquet").to_table()
    oid = lc.column("objectid").to_numpy()
    res = {"n_match_objectids": int(uo.size), "n_lc_rows": lc.num_rows,
           "n_duplicate_rows": int(oid.size - np.unique(oid).size),
           "n_missing": int(np.setdiff1d(uo, oid).size),
           "n_extra": int(np.setdiff1d(oid, uo).size)}
    ne = lc.column("nepochs").to_numpy()
    res["n_len_mismatch"] = int(sum(
        (pc.list_value_length(lc.column(c)).to_numpy() != ne).sum()
        for c in ("hmjd", "mag", "magerr", "clrcoeff", "catflags")))
    h = lc.column("hmjd").combine_chunks()
    v = h.flatten().to_numpy()
    lengths = pc.list_value_length(h).to_numpy()
    row = np.repeat(np.arange(lengths.size), lengths)
    same = row[1:] == row[:-1]
    res["n_unsorted_steps"] = int((np.diff(v)[same] < 0).sum())
    # index metadata vs file content
    pos = np.searchsorted(uo, oid)
    ok = (pos < uo.size)
    pos = np.clip(pos, 0, uo.size - 1)
    idx = first[pos]
    agree = ok
    for c in ("filterid", "fieldid", "rcid", "nepochs"):
        agree &= m.column(c).to_numpy()[idx].astype(np.int64) == lc.column(c).to_numpy().astype(np.int64)
    res["n_index_disagree"] = int((~agree).sum())
    res["n_epochs_total"] = int(ne.sum())
    res["catflags0_frac"] = float((lc.column("catflags").combine_chunks().flatten().to_numpy() == 0).mean())
    res["pass"] = all(res[k] == 0 for k in ("n_duplicate_rows", "n_missing", "n_extra",
                                             "n_len_mismatch", "n_unsorted_steps",
                                             "n_index_disagree"))
    return res
