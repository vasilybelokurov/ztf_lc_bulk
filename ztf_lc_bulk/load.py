"""Read extracted light curves per target, with optional cuts and primary selection."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from . import FILTER_NAMES
from .extract import LIST_COLS

BAND_TO_ID = {v: k for k, v in FILTER_NAMES.items()}


def _rows_of(listcol: pa.Array) -> tuple[np.ndarray, np.ndarray]:
    lengths = pc.list_value_length(listcol).to_numpy(zero_copy_only=False)
    return lengths, np.repeat(np.arange(lengths.size), lengths)


def filter_epochs(tab: pa.Table, mask: np.ndarray) -> pa.Table:
    """Keep epochs where ``mask`` (aligned with the flattened list columns) is True.

    Updates ``nepochs`` to the new per-row length; rows may end up with 0 epochs.
    """
    h = tab.column("hmjd").combine_chunks()
    lengths, row = _rows_of(h)
    new_len = np.bincount(row[mask], minlength=lengths.size)
    offsets = pa.array(np.concatenate([[0], np.cumsum(new_len)]).astype(np.int32))
    keep = pa.array(mask)
    cols = {}
    for name in tab.column_names:
        if name in LIST_COLS:
            flat = tab.column(name).combine_chunks().flatten().filter(keep)
            cols[name] = pa.ListArray.from_arrays(offsets, flat)
        elif name == "nepochs":
            cols[name] = pa.array(new_len.astype(np.int64))
        else:
            cols[name] = tab.column(name)
    return pa.table(cols)


def select_primary(df: pd.DataFrame) -> pd.DataFrame:
    """One ZTF objectid per (src_id, filterid).

    Rank: most epochs (after any cuts), then longest baseline, then smallest
    separation, then smallest objectid (deterministic). This is the rule used in
    ztf_qso_parquet/select_primary_lightcurves.py, with separation and objectid
    added as final tie-breakers.
    """
    order = df.sort_values(["src_id", "filterid", "nepochs", "baseline_days", "sep_arcsec", "objectid"],
                           ascending=[True, True, False, False, True, True], kind="stable")
    return order.groupby(["src_id", "filterid"], sort=False).head(1).sort_index()


def load(run_dir, src_ids=None, bands=None, catflags0: bool = True,
         primary: bool = False, min_epochs: int = 1, long: bool = False,
         match_path=None) -> pd.DataFrame:
    """Load extracted light curves joined to the target ids.

    Parameters
    ----------
    run_dir : path
        Directory given as ``--out`` to ``extract`` (contains ``lc/``).
    src_ids : array-like, optional
        Restrict to these target ids.
    bands : iterable of {"g", "r", "i"}, optional
        Restrict to these bands.
    catflags0 : bool
        Keep only epochs with catflags == 0 (default True).
    primary : bool
        Keep one objectid per (src_id, band); see :func:`select_primary`.
    min_epochs : int
        Drop (src_id, objectid) rows with fewer epochs after cuts.
    long : bool
        Return one row per epoch instead of one row per (src_id, objectid).
    match_path : path, optional
        Defaults to ``run_dir/match.parquet``.

    Returns
    -------
    pandas.DataFrame. Nested form: one row per (src_id, objectid) with columns
    src_id, objectid, band, filterid, fieldid, rcid, sep_arcsec, objra, objdec,
    nepochs, baseline_days and array columns hmjd, mag, magerr, clrcoeff, catflags.
    Long form: one row per epoch with scalar hmjd, mag, ... .
    """
    run_dir = Path(run_dir)
    m = pq.read_table(match_path or run_dir / "match.parquet",
                      columns=["src_id", "objectid", "filterid", "sep_arcsec"])
    if src_ids is not None:
        m = m.filter(pc.is_in(m["src_id"], value_set=pa.array(np.asarray(src_ids))))
    if bands is not None:
        ids = [BAND_TO_ID[b] for b in bands]
        m = m.filter(pc.is_in(m["filterid"], value_set=pa.array(ids, pa.int16()).cast(m["filterid"].type)))
    want = np.unique(m["objectid"].to_numpy())
    lc = ds.dataset(run_dir / "lc", format="parquet").to_table(
        filter=ds.field("objectid").isin(want))
    if catflags0:
        cf = lc.column("catflags").combine_chunks().flatten().to_numpy()
        lc = filter_epochs(lc, cf == 0)

    # attach light curves to (src_id, objectid) pairs; list columns cannot go through
    # Arrow hash joins, so index by objectid instead
    lo = lc.column("objectid").to_numpy()
    order = np.argsort(lo)
    mo = m["objectid"].to_numpy()
    pos = np.searchsorted(lo[order], mo)
    found = (pos < lo.size) & (lo[order][np.clip(pos, 0, max(lo.size - 1, 0))] == mo)
    if not found.all():
        raise ValueError(f"{int((~found).sum())} matched objectids have no extracted light curve; "
                         "run extract/verify first")
    t = lc.take(pa.array(order[pos]))
    h = t.column("hmjd").combine_chunks()
    lengths, _ = _rows_of(h)
    flat = h.flatten().to_numpy()
    start = np.concatenate([[0], np.cumsum(lengths)[:-1]])
    nz = lengths > 0
    baseline = np.zeros(lengths.size)
    baseline[nz] = flat[start[nz] + lengths[nz] - 1] - flat[start[nz]]  # epochs are time-sorted
    t = (t.append_column("src_id", m["src_id"])
          .append_column("sep_arcsec", m["sep_arcsec"])
          .append_column("baseline_days", pa.array(baseline)))

    scalars = ["src_id", "objectid", "filterid", "fieldid", "rcid", "sep_arcsec",
               "objra", "objdec", "nepochs", "baseline_days"]
    keep = np.flatnonzero(t["nepochs"].to_numpy() >= min_epochs)
    t = t.take(pa.array(keep))
    meta = t.select(scalars).to_pandas()
    if primary:
        meta = select_primary(meta)
        t = t.take(pa.array(meta.index.to_numpy()))
        meta = meta.reset_index(drop=True)
    meta.insert(2, "band", meta["filterid"].map(FILTER_NAMES))

    if not long:
        for c in LIST_COLS:
            meta[c] = t.column(c).to_pylist() if t.num_rows == 0 else \
                list(np.split(t.column(c).combine_chunks().flatten().to_numpy(),
                              np.cumsum(t["nepochs"].to_numpy())[:-1]))
        return meta
    rep = t["nepochs"].to_numpy()
    out = meta.loc[meta.index.repeat(rep)].reset_index(drop=True)
    for c in LIST_COLS:
        out[c] = t.column(c).combine_chunks().flatten().to_numpy()
    return out
