"""Read selected objectids from one ZTF parquet file; sort epochs in time."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

SCALAR_COLS = ["objectid", "filterid", "fieldid", "rcid", "objra", "objdec", "nepochs"]
LIST_COLS = ["hmjd", "mag", "magerr", "clrcoeff", "catflags"]


def sort_epochs(tab: pa.Table, key: str = "hmjd") -> pa.Table:
    """Sort every list column of each row by ``key`` (DR23 arrays are not time-ordered)."""
    if tab.num_rows == 0:
        return tab
    keycol = tab.column(key).combine_chunks()
    lengths = pc.list_value_length(keycol).to_numpy()
    offsets = pa.array(np.concatenate([[0], np.cumsum(lengths)]).astype(np.int32))
    row = np.repeat(np.arange(len(lengths)), lengths)
    vals = keycol.flatten().to_numpy()
    order = np.lexsort((vals, row))  # within each row, by time
    idx = pa.array(order)
    new = {}
    for name in tab.column_names:
        col = tab.column(name)
        if name in LIST_COLS:
            col = col.combine_chunks()
            if not np.array_equal(pc.list_value_length(col).to_numpy(), lengths):
                raise ValueError(f"list column {name} has lengths inconsistent with {key}")
            flat = col.flatten().take(idx)  # flatten() honours slice offsets
            new[name] = pa.ListArray.from_arrays(offsets, flat)
        else:
            new[name] = col
    return pa.table(new)


def extract_file(path, objectids) -> tuple[pa.Table, np.ndarray]:
    """Read rows for ``objectids`` from one DR23 light-curve parquet file.

    Returns
    -------
    (table, missing): table has one row per objectid with list-valued epoch
    columns sorted by hmjd; ``missing`` lists requested ids not found.
    """
    want = np.unique(np.asarray(objectids, dtype=np.int64))
    tab = pq.read_table(path, columns=SCALAR_COLS + LIST_COLS,
                        filters=[("objectid", "in", want.tolist())])
    tab = sort_epochs(tab)
    got = tab.column("objectid").to_numpy()
    missing = np.setdiff1d(want, got)
    n = pc.list_value_length(tab.column("hmjd")).to_numpy()
    bad = np.flatnonzero(n != tab.column("nepochs").to_numpy())
    if bad.size:
        raise ValueError(f"{path}: nepochs != len(hmjd) for objectid {got[bad[:5]]}")
    return tab, missing
