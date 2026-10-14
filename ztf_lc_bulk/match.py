"""Crossmatch a target list against the WSDB index of the ZTF DR23 parquet files."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import INDEX_FILES, INDEX_OBJS

MATCH_COLUMNS = ("src_id", "objectid", "filterid", "fieldid", "rcid", "fileid",
                 "nepochs", "sep_arcsec", "filename")

# One materialized CTE: a single q3c_join is far faster than batched queries
# (ztf_qso_parquet: 2.6M matches in ~30 s vs 30-60 min batched).
_SELECT = """
with x as materialized (
    select c.{id} as src_id, z.objectid, z.filterid, z.fieldid, z.rcid, z.fileid,
           z.nepochs, q3c_dist(c.{ra}, c.{dec}, z.objra, z.objdec) * 3600. as sep_arcsec
    from {table} as c, {objs} as z
    where q3c_join(c.{ra}, c.{dec}, z.objra, z.objdec, {radius_deg}) {where}
)
select x.src_id, x.objectid, x.filterid, x.fieldid, x.rcid, x.fileid, x.nepochs,
       x.sep_arcsec, f.filename
from x join {files} as f on f.fileid = x.fileid
order by f.filename, x.objectid
"""


def _to_table(cols: list[np.ndarray]) -> pa.Table:
    return pa.table({name: np.asarray(c) for name, c in zip(MATCH_COLUMNS, cols)})


def match_wsdb_table(table: str, id_col: str = "id", ra_col: str = "ra",
                     dec_col: str = "dec", radius_arcsec: float = 1.0,
                     where: str | None = None, db: str = "wsdb", **conn) -> pa.Table:
    """Crossmatch a catalogue that already lives in WSDB.

    Parameters
    ----------
    table : str
        Schema-qualified table name, e.g. ``vasily.cosmos2020_classic``.
    id_col, ra_col, dec_col : str
        Column names of the unique source id and J2000 coordinates (deg).
    radius_arcsec : float
        Match radius. Every ZTF object inside it is returned (one row per pair).
    where : str, optional
        Extra SQL condition on the target table, referencing it as ``c``.
    db, **conn
        Passed to ``sqlutilpy.get`` (host, user, ...).

    Returns
    -------
    pyarrow.Table with columns ``MATCH_COLUMNS``, ordered by filename.
    """
    import sqlutilpy

    q = _SELECT.format(id=id_col, ra=ra_col, dec=dec_col, table=table,
                       objs=INDEX_OBJS, files=INDEX_FILES,
                       radius_deg=radius_arcsec / 3600.,
                       where=f"and ({where})" if where else "")
    return _to_table(sqlutilpy.get(q, db=db, **conn))


def match_local(src_id: np.ndarray, ra: np.ndarray, dec: np.ndarray,
                radius_arcsec: float = 1.0, db: str = "wsdb", **conn) -> pa.Table:
    """Crossmatch a local target list, uploaded to WSDB as a temporary table.

    Same output as :func:`match_wsdb_table`; ``src_id`` may be integer or string.
    """
    import sqlutilpy

    q = _SELECT.format(id="src_id", ra="ra", dec="dec", table="ztflcb_targets",
                       objs=INDEX_OBJS, files=INDEX_FILES,
                       radius_deg=radius_arcsec / 3600., where="")
    res = sqlutilpy.local_join(q, "ztflcb_targets",
                               (np.asarray(src_id), np.asarray(ra, dtype=float),
                                np.asarray(dec, dtype=float)),
                               ("src_id", "ra", "dec"), db=db, **conn)
    return _to_table(res)


def save_match(tab: pa.Table, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tab, path)


def summarize_match(tab: pa.Table) -> dict:
    d = {c: tab.column(c).to_numpy() for c in ("src_id", "objectid", "fileid", "nepochs")}
    _, first = np.unique(d["objectid"], return_index=True)
    return {
        "n_pairs": tab.num_rows,
        "n_sources": int(np.unique(d["src_id"]).size),
        "n_objectids": int(first.size),
        "n_files": int(np.unique(d["fileid"]).size),
        "n_epochs_unique_objects": int(d["nepochs"][first].astype(np.int64).sum()),
    }


def run_match(out: str | Path, **kw) -> dict:
    """Match and save. ``kw`` goes to :func:`match_local` if it contains ``ra``,
    otherwise to :func:`match_wsdb_table`. Returns a summary dict."""
    t0 = time.time()
    tab = match_local(**kw) if "ra" in kw else match_wsdb_table(**kw)
    save_match(tab, out)
    s = summarize_match(tab)
    s["seconds"] = round(time.time() - t0, 1)
    return s
