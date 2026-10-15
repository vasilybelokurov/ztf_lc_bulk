---
name: ztf-lc
description: Get ZTF DR23 or DR24 light curves for a target list (thousands to millions of sources) with the ztf_lc_bulk package, which crossmatches in WSDB against koposov.ztf_23_objs and extracts from the IRSA parquet release. Use whenever a task needs ZTF light curves, ZTF photometry time series, ZTF objectids for a catalogue, or asks to "download ZTF", "get ZTF lightcurves", "ZTF variability" for more than a handful of objects. Use it instead of writing a new IRSA nph_light_curves API downloader.
---

# ztf-lc: ZTF light curves via ztf_lc_bulk

Package: `~/Work/Code/ztf_lc_bulk`, installed editable in `~/Work/venvs/.venv`.
Read its `README.md` for the full output schema and test descriptions.

## Decide the route first
- **More than ~100 targets, or a dense field**: use this package (bulk parquet).
- **A handful of objects, or you need sharp/chi per epoch**: use the IRSA
  API (`nph_light_curves`, pin `COLLECTION`, at most 4–8 concurrent requests;
  ≥16 gives ~100% failures).
- **Galaxies whose nucleus varies**: catalogue PSF light curves are unreliable. You need
  ZFPS forced photometry, which is slow (weeks).

## Workflow
```bash
source ~/Work/venvs/.venv/bin/activate
# 1. match: a WSDB table, or a local file (.parquet/.csv/.fits) uploaded as a temp table
ztf-lc-bulk match --table schema.table --id id --ra ra --dec dec --radius 1.0 --out runs/NAME
ztf-lc-bulk match --input targets.csv --id name --ra ra --dec dec --out runs/NAME
# 2. Read download_gb_est / download_hours_at_1MBps in the match summary and tell
#    the user before extracting (extract refuses > 50 GB unless --max-gb is given)
# 3. extract (resumable; rerun to retry failures), then verify
ztf-lc-bulk extract --out runs/NAME --workers 4      # add --release dr24 for DR24 (new run dir)
ztf-lc-bulk verify  --out runs/NAME
ztf-lc-bulk validate-api --out runs/NAME --n 20     # optional: compare with IRSA API
ztf-lc-bulk exposures --out runs/NAME               # optional: per-image metadata (~1-2 min/field)
#   add --whole-fields for every quadrant of the run's fields; README 'Per-exposure metadata' explains routes A/B
```
```python
import ztf_lc_bulk as z
df = z.load("runs/NAME", primary=True)            # one row per (src_id, band); array columns
lc = z.load("runs/NAME", src_ids=[...], long=True) # one row per epoch
lc = z.load("runs/NAME", long=True, exposures=True) # + expid, limitmag, maglimit, magzp, seeing, airmass, ...
nd = z.nondetections("runs/NAME", columns=["limitmag"])  # images covering an object without a detection
```
`load` defaults: catflags==0 only, min_epochs=1. Primary objectid per (src_id, band) =
most clean epochs, then longest baseline, then smallest separation.

## Facts to respect
- Releases: **DR23** (default) or **DR24** (`--release dr24`). Objects are always located
  through the DR23 index. DR23 objects get complete, correct DR24 light curves (a broken
  index would make `verify` fail). Objects NEW in DR24 are silently absent: 0.9–7.1% of
  objects per COSMOS file, all with ≤ 5 epochs and median mag 20.5–21.3. Fine for
  variability of known objects; for completeness-sensitive work tell the user, and see the
  README section "Can the DR23 index be relied on for DR24?".
- `hmjd` is heliocentric MJD, rounded to 1e-5 d (0.86 s).
- objectids are per band and per field/CCD/quadrant. A source typically has about 2 per
  band, with complementary epochs and field-to-field zero-point offsets of ~0.06 mag
  (median). Do not merge them silently; use `primary=True` or model the offsets.
- The parquet has only hmjd/mag/magerr/clrcoeff/catflags. Image metadata (limitmag,
  maglimit, magzp, seeing, airmass, expid) comes from `ztf-lc-bulk exposures` (IBE image
  metadata + 3 API light curves per quadrant); never fetch it per object. sharp and chi
  are per detection and come only from the API.
- **Faint sources (fainter than ~17.5 mag) and moonlight:** catalog magnitudes are biased
  bright on bright-sky images. Use `lc = z.add_brightsky_columns(z.load(run, long=True,
  exposures=True, catflags0=False))`, keep `catflags == 0 & ~near_limit`, use `mag_corr`.
  Expect to lose most epochs of sources fainter than ~19.5 mag; tell the user. README
  section "Moonlight and bright sky".
- `limitmag` (light-curve/API) and `maglimit` (image header, IBE) are different: limitmag
  is ~0.55 mag deeper (rms 0.21). Say which one you use.
- **Download size is set by sky area, not source count**: each field × band × quadrant
  file (~0.74 deg²) must be downloaded whole. IRSA serves ~0.7–1.1 MB/s, so estimate
  time before starting large runs and tell the user.
- Arrays in the raw IRSA files are not time-sorted; this package sorts them.
- `sqlutilpy.get(..., driver='duckdb')` scrambles list columns in sqlutilpy ≤ 0.27
  (fixed in 0.28.0). If you query the parquet files that way, check the version first.
- The API's ztf_dr23 collection has ~0.2% more epochs (Nov 2023–Mar 2024) than the
  parquet release. This is expected; it is not a bug in the extraction.
