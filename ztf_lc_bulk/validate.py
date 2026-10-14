"""Independent validation: compare extracted light curves with the IRSA LC API.

For a sample of extracted objectids, query
``nph_light_curves?ID=<oid>&COLLECTION=ztf_dr23`` and match epochs by time.
An object passes if every parquet epoch has an API epoch within ``TOL_DAYS``
and mag, magerr and catflags agree (|d| < 1e-5 mag). Epochs present only in
the API are counted and reported, not treated as failures: the API's
``ztf_dr23`` collection holds ~0.2% more epochs than the parquet release.
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import requests

API = "https://irsa.ipac.caltech.edu/cgi-bin/ZTF/nph_light_curves"
TOL_DAYS = 1e-5  # parquet hmjd is stored rounded to 1e-5 d (0.86 s)


def fetch_api(oid: int, collection: str = "ztf_dr23", retries: int = 3,
              timeout: float = 120) -> pd.DataFrame:
    """Return the API light curve of one objectid as a DataFrame (parsed by header)."""
    last = None
    for _ in range(retries):
        try:
            r = requests.get(API, params={"ID": int(oid), "COLLECTION": collection,
                                          "FORMAT": "CSV"}, timeout=timeout)
            r.raise_for_status()
            return pd.read_csv(io.StringIO(r.text))
        except Exception as e:  # noqa: BLE001 - retried, then re-raised
            last = e
    raise RuntimeError(f"API failed for {oid}: {last}")


def compare(row, api: pd.DataFrame, extra_log: list | None = None) -> dict:
    """Epoch-match one extracted light curve (dict-like row) to its API light curve."""
    api = api.sort_values("hjd", kind="stable")
    h_api = api["hjd"].to_numpy() - 2400000.5
    h = np.asarray(row["hmjd"])
    out = {"objectid": int(row["objectid"]), "n_parquet": len(h), "n_api": len(api)}
    if len(h_api) == 0:
        out.update(n_parquet_unmatched=len(h), n_api_only=0, ok=len(h) == 0)
        return out
    j = np.searchsorted(h_api, h).clip(0, len(h_api) - 1)
    jm = np.maximum(j - 1, 0)
    best = np.where(np.abs(h_api[j] - h) < np.abs(h_api[jm] - h), j, jm)
    dt = np.abs(h_api[best] - h)
    matched = dt <= TOL_DAYS
    b = best[matched]
    api_only = np.setdiff1d(np.arange(len(h_api)), b)
    out["n_parquet_unmatched"] = int((~matched).sum())
    out["n_api_only"] = int(api_only.size)
    if extra_log is not None:
        for k in api_only:
            extra_log.append({"objectid": out["objectid"], **api.iloc[k][
                ["mjd", "mag", "catflags", "field", "ccdid", "qid", "programid"]].to_dict()})
    if matched.any():
        out["max_dt_s"] = float(dt[matched].max() * 86400)
        out["max_dmag"] = float(np.abs(np.asarray(row["mag"])[matched] - api["mag"].to_numpy()[b]).max())
        out["max_dmagerr"] = float(np.abs(np.asarray(row["magerr"])[matched] - api["magerr"].to_numpy()[b]).max())
    else:
        out["max_dt_s"] = out["max_dmag"] = out["max_dmagerr"] = 0.0
    out["catflags_equal"] = bool(np.array_equal(np.asarray(row["catflags"])[matched],
                                                api["catflags"].to_numpy()[b]))
    out["ok"] = (out["n_parquet_unmatched"] == 0 and out["max_dmag"] < 1e-5
                 and out["max_dmagerr"] < 1e-5 and out["catflags_equal"])
    return out


def api_spotcheck(run_dir, n: int = 40, threads: int = 4, seed: int = 1,
                  collection: str | None = None) -> dict:
    """Compare ``n`` extracted light curves with the API.

    ``collection`` defaults to ``ztf_<release>`` of the run (``run.json``).

    Half the sample is random, half is drawn from the 2,000 longest light curves.
    Returns a summary dict plus per-object results (``table``) and API-only
    epochs (``api_only``) as DataFrames.
    """
    from functools import partial
    from .pipeline import run_release
    collection = collection or f"ztf_{run_release(run_dir)}"
    lc = ds.dataset(Path(run_dir) / "lc", format="parquet").to_table().to_pandas()
    rng = np.random.default_rng(seed)
    n = min(n, len(lc))
    longest = np.argsort(lc["nepochs"].to_numpy())[-2000:]
    idx = np.concatenate([rng.choice(len(lc), n // 2, replace=False),
                          rng.choice(longest, min(n - n // 2, longest.size), replace=False)])
    sample = lc.iloc[idx]
    with ThreadPoolExecutor(threads) as ex:  # IRSA fails at >=16 concurrent requests
        apis = list(ex.map(partial(fetch_api, collection=collection), sample["objectid"]))
    extra: list = []
    res = pd.DataFrame([compare(r, a, extra) for (_, r), a in zip(sample.iterrows(), apis)])
    res["filterid"] = sample["filterid"].to_numpy()
    return {"collection": collection, "n_objects": len(res), "n_pass": int(res["ok"].sum()),
            "parquet_epochs": int(res["n_parquet"].sum()),
            "api_only_epochs": int(res["n_api_only"].sum()),
            "max_dt_s": float(res["max_dt_s"].max()), "max_dmag": float(res["max_dmag"].max()),
            "pass": bool(res["ok"].all()), "table": res, "api_only": pd.DataFrame(extra)}
