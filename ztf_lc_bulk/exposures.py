"""Per-exposure (image) metadata for extracted light curves.

The IRSA light-curve parquet files carry only hmjd, mag, magerr, clrcoeff and catflags
per epoch. Image-level quantities come from two IRSA services, combined here into one
table with one row per science image (exposure x CCD-quadrant), keyed by
(field, rcid, fid, expid):

A. Science-image metadata (``ztf.ztf_current_meta_sci``, served by IBE): every image,
   with obsjd (UTC, exposure start), exptime, seeing, airmass, moonillf, maglimit
   (image-header MAGLIM: median mag of PSF-catalog sources at S/N ~ 5), infobits and
   the four image corners. One query per field returns all quadrants and filters.
B. Light-curve API (``nph_light_curves``): limitmag (the light-curve "approximate 5-sigma
   limiting magnitude"; NOT equal to maglimit: on COSMOS limitmag - maglimit has
   median +0.55 mag, rms 0.21), magzp, magzprms, clrcounc, programid. These are image
   values repeated in every light curve, so the light curves of the ``n_api`` objects
   with most epochs in a quadrant (picked from the WSDB index) cover ~98-99% of its
   images. Images not covered have NaN in these columns.

Epoch matching: parquet ``hmjd`` = MJD(UTC, mid-exposure) + heliocentric light-travel
time at the object's position (verified to the 1e-5 d storage rounding). The cache
stores the mid-exposure MJD and Earth's heliocentric position per image, so matching
needs only numpy; building the cache needs astropy.
"""

from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import requests

from . import INDEX_OBJS

IBE_SCI = "https://irsa.ipac.caltech.edu/ibe/search/ztf/products/sci"
IBE_COLS = ["field", "ccdid", "qid", "rcid", "fid", "filtercode", "expid", "pid", "obsjd",
            "exptime", "ra", "dec", "ra1", "dec1", "ra2", "dec2", "ra3", "dec3", "ra4", "dec4",
            "infobits", "seeing", "airmass", "moonillf", "maglimit"]
# moonesb (Moon excess sky brightness) is omitted: 0 in every COSMOS row checked.
API_COLS = ["limitmag", "magzp", "magzprms", "clrcounc", "programid"]
KEY = ["field", "rcid", "fid"]
EXPOSURE_COLS = ["expid", "mjd_mid", "exptime", "maglimit", "limitmag", "magzp", "magzprms",
                 "seeing", "airmass", "moonillf", "infobits", "programid"]
TOL_S = 5.0          # observed max |hmjd - prediction| is 0.44 s; images are >= 39.6 s apart
QUAD_RADIUS = 0.65   # deg; half-diagonal of a CCD quadrant is ~0.61 deg


def _get(url: str, params: dict, retries: int = 5, timeout: float = 300) -> requests.Response:
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(min(60.0, 2.0 * 2 ** attempt))
    raise RuntimeError(f"failed after {retries} attempts: {url} {params}: {last}")


def query_images(field: int) -> pd.DataFrame:
    """All science images of one ZTF field (all CCD-quadrants and filters) from IBE."""
    r = _get(IBE_SCI, {"WHERE": f"field={int(field)}", "COLUMNS": ",".join(IBE_COLS), "ct": "csv"})
    df = pd.read_csv(io.StringIO(r.text))
    return df[df["exptime"] > 0].reset_index(drop=True)


def earth_helio_lightdays(mjd_utc: np.ndarray) -> np.ndarray:
    """Heliocentric ICRS position of Earth (geocentre) in light-days, shape (n, 3)."""
    import astropy.constants as const
    import astropy.units as u
    from astropy.coordinates import get_body_barycentric
    from astropy.time import Time
    t = Time(np.asarray(mjd_utc, float), format="mjd", scale="utc")
    r = (get_body_barycentric("earth", t) - get_body_barycentric("sun", t)).xyz.to_value(u.au)
    return (r * (const.au / const.c).to_value(u.day)).T


def unit_vector(ra, dec) -> np.ndarray:
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def predict_hmjd(mjd_mid, rvec, ra, dec) -> np.ndarray:
    """hmjd an epoch on this image would have for a source at (ra, dec) (broadcasts)."""
    return np.asarray(mjd_mid) + np.sum(np.asarray(rvec) * unit_vector(ra, dec), axis=-1)


def top_objects(quads: pd.DataFrame, n: int, db: str = "wsdb") -> pd.DataFrame:
    """The ``n`` objectids with most (DR23) epochs per quadrant; one WSDB query.

    ``quads`` needs columns field, rcid, fid, ra, dec (quadrant centre).
    """
    import sqlutilpy
    if len(quads) == 0:
        return pd.DataFrame(columns=KEY + ["objectid", "nepochs"])
    vals = ",".join(f"({int(r.field)},{int(r.rcid)},{int(r.fid)},{float(r.ra)!r},{float(r.dec)!r})"
                    for r in quads.itertuples())
    q = f"""select q.field, q.rcid, q.fid, z.objectid, z.nepochs
        from (values {vals}) as q(field, rcid, fid, ra, dec)
        cross join lateral (
            select objectid, nepochs from {INDEX_OBJS}
            where q3c_radial_query(objra, objdec, q.ra, q.dec, {QUAD_RADIUS})
              and fieldid = q.field and rcid = q.rcid and filterid = q.fid
            order by nepochs desc, objectid limit {int(n)}) z"""
    f, rc, fi, oid, ne = sqlutilpy.get(q, db=db)
    return pd.DataFrame({"field": f, "rcid": rc, "fid": fi, "objectid": oid, "nepochs": ne})


def api_meta(objs: pd.DataFrame, collection: str, threads: int = 4) -> pd.DataFrame:
    """Image columns ``API_COLS`` per (field, rcid, fid, expid) from the LC API light
    curves of ``objs`` (columns field, rcid, fid, objectid). Also returns ``n_api_obj``:
    how many of the queried light curves contain the image."""
    from .validate import fetch_api
    with ThreadPoolExecutor(threads) as ex:  # IRSA fails at >=16 concurrent requests
        lcs = list(ex.map(partial(fetch_api, collection=collection), objs["objectid"]))
    parts = []
    for (_, o), lc in zip(objs.iterrows(), lcs):
        if len(lc) == 0:
            continue
        p = lc[["expid"] + API_COLS].copy()
        p["field"], p["rcid"], p["fid"] = int(o.field), int(o.rcid), int(o.fid)
        parts.append(p)
    if not parts:
        return pd.DataFrame(columns=KEY + ["expid"] + API_COLS + ["n_api_obj"])
    a = pd.concat(parts, ignore_index=True)
    g = a.groupby(KEY + ["expid"], sort=False)
    out = g[API_COLS].first()
    spread = (g["limitmag"].max() - g["limitmag"].min()).max()
    if spread > 1e-4:  # image values must agree between light curves of one image
        raise RuntimeError(f"limitmag differs between objects on the same image (max {spread})")
    out["n_api_obj"] = g.size()
    return out.reset_index()


def run_quadrants(run_dir) -> pd.DataFrame:
    """Unique (field, rcid, fid) present in a run's extracted light curves."""
    t = ds.dataset(Path(run_dir) / "lc", format="parquet").to_table(
        columns=["fieldid", "rcid", "filterid"]).to_pandas()
    q = t.drop_duplicates().rename(columns={"fieldid": "field", "filterid": "fid"})
    return q.astype(int).sort_values(KEY).reset_index(drop=True)


def build_exposures(run_dir, n_api: int = 3, threads: int = 4, whole_fields: bool = False,
                    log=print) -> dict:
    """Build ``run_dir/exposures.parquet``: one row per image of every quadrant (CCD-quadrant
    x filter) holding a light curve of the run, or, with ``whole_fields``, of every
    quadrant of the run's fields (API columns then cost ``n_api`` calls per extra quadrant).

    IBE results are cached per field in ``run_dir/exposures_ibe/`` (resumable).
    """
    from .pipeline import run_release
    run_dir = Path(run_dir)
    quads = run_quadrants(run_dir)
    cache = run_dir / "exposures_ibe"
    cache.mkdir(exist_ok=True)
    imgs = []
    for field in sorted(quads["field"].unique()):
        p = cache / f"field_{field:06d}.parquet"
        if not p.exists():
            t0 = time.time()
            df = query_images(field)
            df.to_parquet(p, index=False)
            log(f"IBE field {field}: {len(df)} images in {time.time() - t0:.0f} s")
        imgs.append(pd.read_parquet(p))
    img = pd.concat(imgs, ignore_index=True)
    if not whole_fields:
        img = img.merge(quads, on=KEY)
    img["mjd_mid"] = img["obsjd"] - 2400000.5 + img["exptime"] / 2 / 86400
    img[["ex", "ey", "ez"]] = earth_helio_lightdays(img["mjd_mid"].to_numpy())

    centres = img.groupby(KEY, as_index=False)[["ra", "dec"]].median()
    objs = top_objects(centres, n_api)
    collection = f"ztf_{run_release(run_dir)}"
    t0 = time.time()
    meta = api_meta(objs, collection, threads=threads)
    log(f"LC API: {len(objs)} light curves ({collection}) in {time.time() - t0:.0f} s")
    img = img.merge(meta, on=KEY + ["expid"], how="left")
    img["n_api_obj"] = pd.to_numeric(img["n_api_obj"]).fillna(0).astype(int)
    img = img.sort_values(KEY + ["mjd_mid"]).reset_index(drop=True)
    img.to_parquet(run_dir / "exposures.parquet", index=False)
    summary = {"n_images": len(img), "n_quadrants": len(centres), "n_run_quadrants": len(quads),
               "whole_fields": whole_fields, "n_api_objects": len(objs),
               "frac_with_limitmag": float(img["limitmag"].notna().mean()), "collection": collection}
    (run_dir / "exposures.json").write_text(json.dumps(summary, indent=2))
    return summary


def read_exposures(run_dir) -> pd.DataFrame:
    p = Path(run_dir) / "exposures.parquet"
    if not p.exists():
        raise FileNotFoundError(f"{p} not found; run `ztf-lc-bulk exposures --out {run_dir}` first")
    return pd.read_parquet(p)


def match_epochs(exp: pd.DataFrame, field, rcid, fid, ra, dec, hmjd,
                 tol_s: float = TOL_S) -> tuple[np.ndarray, np.ndarray]:
    """Row index into ``exp`` for every epoch (-1 if none within ``tol_s``) and |dt| [s].

    All inputs are flat per-epoch arrays. Candidates are found with the image-centre
    prediction (accurate to ~5 s across a quadrant), then the nearest of the three
    neighbouring images is chosen with the exact prediction at the object's position.
    """
    field, rcid, fid = (np.asarray(x, int) for x in (field, rcid, fid))
    ra, dec, hmjd = (np.asarray(x, float) for x in (ra, dec, hmjd))
    idx = np.full(hmjd.size, -1)
    dts = np.full(hmjd.size, np.inf)
    ekey = exp[KEY].to_numpy(int)
    rvec_all = exp[["ex", "ey", "ez"]].to_numpy()
    pred_c_all = predict_hmjd(exp["mjd_mid"].to_numpy(), rvec_all, exp["ra"].to_numpy(), exp["dec"].to_numpy())
    groups = pd.DataFrame({"f": field, "r": rcid, "b": fid}).groupby(["f", "r", "b"]).indices
    for (f, r, b), sel in groups.items():
        rows = np.flatnonzero((ekey[:, 0] == f) & (ekey[:, 1] == r) & (ekey[:, 2] == b))
        if rows.size == 0:
            continue
        rows = rows[np.argsort(pred_c_all[rows])]
        pc_ = pred_c_all[rows]
        j = np.searchsorted(pc_, hmjd[sel])
        cand = np.clip(np.stack([j - 2, j - 1, j, j + 1]), 0, rows.size - 1)  # (4, nsel)
        ri = rows[cand]
        pred = predict_hmjd(exp["mjd_mid"].to_numpy()[ri], rvec_all[ri], ra[sel], dec[sel])
        d = np.abs(pred - hmjd[sel]) * 86400
        k = d.argmin(axis=0)
        best = d[k, np.arange(sel.size)]
        ok = best <= tol_s
        idx[sel[ok]] = ri[k, np.arange(sel.size)][ok]
        dts[sel] = best
    return idx, dts


def edge_distance(exp_rows: pd.DataFrame, ra, dec) -> np.ndarray:
    """Distance [arcsec] of (ra, dec) from the nearest edge of the image quadrilateral
    (corners ra1..ra4, taken in order around it); positive inside, negative outside.

    Gnomonic projection about the image centre. On COSMOS every detection has
    distance > -1.5 arcsec (the corners and objra/objdec are not exact to that level).
    """
    def proj(a, d, a0, d0):
        a, d, a0, d0 = map(np.radians, (a, d, a0, d0))
        c = np.sin(d0) * np.sin(d) + np.cos(d0) * np.cos(d) * np.cos(a - a0)
        return (np.cos(d) * np.sin(a - a0) / c,
                (np.cos(d0) * np.sin(d) - np.sin(d0) * np.cos(d) * np.cos(a - a0)) / c)
    a0, d0 = exp_rows["ra"].to_numpy(), exp_rows["dec"].to_numpy()
    px, py = proj(np.asarray(ra, float), np.asarray(dec, float), a0, d0)
    cx, cy = zip(*[proj(exp_rows[f"ra{i}"].to_numpy(), exp_rows[f"dec{i}"].to_numpy(), a0, d0)
                   for i in range(1, 5)])
    area2 = sum(cx[i] * cy[(i + 1) % 4] - cx[(i + 1) % 4] * cy[i] for i in range(4))
    orient = np.sign(area2)  # +1 counter-clockwise
    d = [((cx[(i + 1) % 4] - cx[i]) * (py - cy[i]) - (cy[(i + 1) % 4] - cy[i]) * (px - cx[i]))
         / np.hypot(cx[(i + 1) % 4] - cx[i], cy[(i + 1) % 4] - cy[i]) for i in range(4)]
    return np.degrees(np.min(np.stack(d) * orient, axis=0)) * 3600


def nondetections(run_dir, objectids=None, columns=(), tol_s: float = TOL_S,
                  edge_margin: float = 10.0) -> pd.DataFrame:
    """Images that contain an object's position but have no epoch in its light curve.

    An image counts only if (i) the object is at least ``edge_margin`` arcsec inside
    its footprint (avoids sources partly off the chip) and (ii) the image is in the
    release for that quadrant: it holds an epoch of some extracted object or of an API
    light curve (``n_api_obj > 0``). (ii) excludes images taken after the release
    cut-off or dropped from the release. All epochs are used (no catflags cut).

    Returns one row per (objectid, image): objectid, exp_row (row of
    :func:`read_exposures`), edge_dist (arcsec), plus any exposure ``columns``
    requested. The compact default keeps memory low: COSMOS (185k light curves)
    gives 61M rows.
    """
    exp = read_exposures(run_dir)
    lc = ds.dataset(Path(run_dir) / "lc", format="parquet").to_table(
        columns=["objectid", "fieldid", "rcid", "filterid", "objra", "objdec", "nepochs", "hmjd"]).to_pandas()
    n = lc["nepochs"].to_numpy()
    rep = lambda c: np.repeat(lc[c].to_numpy(), n)  # noqa: E731
    idx, _ = match_epochs(exp, rep("fieldid"), rep("rcid"), rep("filterid"), rep("objra"),
                          rep("objdec"), np.concatenate(lc["hmjd"].to_numpy()) if len(lc) else [], tol_s)
    in_release = exp["n_api_obj"].to_numpy() > 0
    in_release[idx[idx >= 0]] = True
    seen = pd.DataFrame({"objectid": rep("objectid"), "row": idx})
    seen = {k: np.unique(v.to_numpy()) for k, v in seen[seen["row"] >= 0].groupby("objectid")["row"]}
    if objectids is not None:
        lc = lc[lc["objectid"].isin(np.asarray(objectids))]
    quad_rows = pd.DataFrame({"f": exp["field"], "r": exp["rcid"], "b": exp["fid"]})[in_release] \
        .groupby(["f", "r", "b"]).indices
    rel = np.flatnonzero(in_release)
    out_o, out_r, out_d = [], [], []
    for o in lc.itertuples():
        rows = rel[quad_rows.get((o.fieldid, o.rcid, o.filterid), np.array([], int))]
        rows = np.setdiff1d(rows, seen.get(o.objectid, np.array([], int)), assume_unique=True)
        if rows.size == 0:
            continue
        dist = edge_distance(exp.iloc[rows], o.objra, o.objdec)
        keep = dist >= edge_margin
        out_o.append(np.full(keep.sum(), o.objectid)); out_r.append(rows[keep]); out_d.append(dist[keep])
    cat = lambda x, t: np.concatenate(x).astype(t) if x else np.array([], t)  # noqa: E731
    nd = pd.DataFrame({"objectid": cat(out_o, np.int64), "exp_row": cat(out_r, np.int32),
                       "edge_dist": cat(out_d, np.float32)})
    for c in columns:
        nd[c] = exp[c].to_numpy()[nd["exp_row"].to_numpy()]
    return nd
