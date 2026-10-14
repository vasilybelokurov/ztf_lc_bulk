# ztf_lc_bulk

Bulk extraction of **ZTF DR23 or DR24** light curves for a target list. The tool
crossmatches the targets in WSDB against Sergey Koposov's index of the IRSA DR23
parquet release (`koposov.ztf_23_objs` / `koposov.ztf_23_files`, objectid → file).
It then downloads only the files that are needed, from DR23 or DR24
(`--release dr24`), and keeps only the matched objects.

- Use it for lists of more than ~100 targets.
- For a handful of objects, or when you need sharp/chi/limitmag/airmass, use the IRSA
  light-curve API instead.
- **DR24 limitation:** objects are located through the DR23 index, so DR24 output
  holds the full DR24 light curves of objects that already existed in DR23. Objects
  new in DR24 are silently absent. In COSMOS they are 0.9–7.1% of the objects per
  file; all have ≤ 5 epochs and they are faint. See
  [Can the DR23 index be relied on for DR24?](#can-the-dr23-index-be-relied-on-for-dr24).

## Install

Requirements:
- Python ≥ 3.10.
- A WSDB account. The index tables `koposov.ztf_23_objs` and `koposov.ztf_23_files`
  are readable by the role `dbuser`, which standard WSDB accounts belong to (checked
  2026-10-14). If you get a permission error, ask Sergey Koposov.
- Free disk space for the files in flight. Each raw file is deleted after extraction,
  so this is about `workers × 30 MB` plus the output.

```bash
source ~/Work/venvs/.venv/bin/activate                 # or your own env
pip install -e ~/Work/Code/ztf_lc_bulk                # developers: editable
# pip install git+https://github.com/vasilybelokurov/ztf_lc_bulk.git@v0.2.0   # users
python -m pytest ~/Work/Code/ztf_lc_bulk -q           # offline tests, ~3 s
```

## Usage

### Command line
```bash
# 1. match: a WSDB table ...
ztf-lc-bulk match --table vasily.cosmos2020_classic --id id --ra ra --dec dec \
                  --radius 1.0 --out runs/cosmos
#    ... or a local .parquet/.csv/.fits list (uploaded as a temporary table)
ztf-lc-bulk match --input targets.csv --id name --ra ra --dec dec --out runs/mylist
# 2. extract (resumable: rerun to retry failed files), 3. verify, 4. optional API check
ztf-lc-bulk extract --out runs/cosmos --workers 4      # --mirror /path/to/lc  if you have one
                                                       # --max-gb N  to allow > 50 GB
ztf-lc-bulk verify  --out runs/cosmos
ztf-lc-bulk validate-api --out runs/cosmos --n 40
```

- **Check the size before extracting.** `match` prints `n_files`, `download_gb_est` and
  `download_hours_at_1MBps`. The download is that many **whole** files: DR23 files have
  a median size of 10.5 MB and a mean of 45 MB, and IRSA served 0.7–1.1 MB/s when
  measured.
- `extract` logs the same estimate for the files still to fetch (files found in
  `--mirror` cost nothing). It **refuses to start above 50 GB** unless you pass
  `--max-gb`. In Python, `run_extract(..., max_gb=None)` has no limit.
- File sizes come from `ztf_lc_bulk/data/dr23_file_bytes.parquet` (all 177,218 files).
  It agrees with HTTP Content-Length to 0.2% in total and ±1.5% per file.
- `--where` adds an SQL condition on a WSDB table, using the alias `c`, e.g.
  `--where "c.lp_mass_med < 9.5"`.

### Python
```python
import ztf_lc_bulk as z

z.run_match("runs/x/match.parquet", table="vasily.nsa_v1_0_1", id_col="nsaid")
z.run_match("runs/y/match.parquet", src_id=ids, ra=ra, dec=dec, radius_arcsec=1.0)
z.run_extract("runs/x/match.parquet", "runs/x", workers=4)
assert z.verify("runs/x/match.parquet", "runs/x")["pass"]

df = z.load("runs/x", primary=True)                  # one row per (src_id, band)
lc = z.load("runs/x", src_ids=[123], long=True)      # one row per epoch
```

`load(run_dir, src_ids=None, bands=None, catflags0=True, primary=False, min_epochs=1, long=False)`:

- `catflags0` keeps only epochs with catflags == 0.
- `min_epochs` drops (src_id, objectid) pairs with fewer epochs after the cuts.
- `primary` keeps one objectid per (src_id, band). The ranking is: most epochs after
  cuts, then longest baseline, then smallest separation, then smallest objectid. This
  follows `ztf_qso_parquet/select_primary_lightcurves.py`.
- Without `primary`, every objectid within the radius is kept.

## Output layout (`--out` run directory)

| Path | Content |
|---|---|
| `match.parquet` | One row per (target, ZTF objectid) pair within the radius |
| `lc/file_<fileid>.parquet` | One row per ZTF objectid; one file per IRSA input file; zstd |
| `manifest.jsonl` | One JSON line per file attempt (append-only) |
| `raw/` | Files being downloaded (`*.part` until complete); empty after a clean run |

### `match.parquet`
| Column | Type | Meaning |
|---|---|---|
| `src_id` | as input | Target id |
| `objectid` | int64 | ZTF DR objectid. It is unique per band and field/CCD/quadrant, so g and r ids never coincide |
| `filterid` | int16 | 1 = g, 2 = r, 3 = i |
| `fieldid`, `rcid` | int16 | ZTF field; readout channel (0–63) = 4 × (CCD − 1) + quadrant − 1 |
| `fileid`, `filename` | int, str | Index file id; path relative to `https://irsa.ipac.caltech.edu/data/ZTF/lc/` |
| `nepochs` | int16 | Number of epochs according to the index |
| `sep_arcsec` | float64 | Separation between target and ZTF object position (arcsec) |

### `lc/*.parquet` (and `load()` output)
| Column | Type | Unit / meaning |
|---|---|---|
| `objectid`, `filterid`, `fieldid`, `rcid` | int | As above |
| `objra`, `objdec` | float32 | ZTF object position (deg, J2000) |
| `nepochs` | int64 | Length of each list column |
| `hmjd` | list[float64] | Heliocentric MJD (days), **sorted ascending**, stored rounded to 1e-5 d |
| `mag`, `magerr` | list[float32] | PSF-fit magnitude and its error, as delivered by IRSA (mag) |
| `clrcoeff` | list[float32] | Colour coefficient of the photometric calibration |
| `catflags` | list[int32] | Per-epoch quality bits; 0 = clean. 32768 (bit 15) is the most common flag |

`load()` adds `src_id`, `band`, `sep_arcsec` and `baseline_days` (last − first hmjd,
after cuts). In nested form the list columns are numpy arrays.

### `manifest.jsonl`
Each line records: `fileid`, `filename`, `status` (`ok` or `failed`), `n_requested`,
`n_written`, `n_missing`, `n_epochs`, `bytes_downloaded`, `t_fetch`, `t_extract`,
`time`, and `error` on failure.

## How it works
1. **match**: one materialized-CTE `q3c_join` of the target list against
   `ztf_23_objs`, joined to `ztf_23_files`. A single join beats batched queries
   (~30 s vs 30–60 min for 2.6M matches in `ztf_qso_parquet`).
2. **extract**: the unique objectids are grouped by file. For each file the tool:
   - uses the mirror if one is given, otherwise downloads it over HTTPS. Downloads go to
     `*.part` and are renamed only if the size matches Content-Length; HTTP
     408/425/429/5xx are retried with backoff (2·2^k s, cap 60 s, Retry-After honoured);
     404 fails immediately;
   - reads only the matched objectids with pyarrow;
   - sorts epochs by hmjd (the raw arrays are not time-ordered);
   - writes the output via tmp + rename, then deletes the raw file.

   A rerun skips files whose output exists and retries failed ones.
3. **verify**: compares the output with the match table (see Tests).

## Tests

```bash
python -m pytest -q                 # offline: 20 tests, no network
python -m pytest -m network -v      # WSDB + IRSA: 2 tests, ~15 s, downloads ~3 MB
```

| File | Test | What it checks |
|---|---|---|
| `test_offline.py` | `sort_epochs_matches_naive` | Vectorised per-row time sort equals numpy argsort row by row, for all list columns |
| | `sort_epochs_on_sliced_table` | Correct when the Arrow list offsets do not start at 0 |
| | `extract_selects_and_reports_missing` | Only requested ids are returned, unknown ids are reported, and mag stays paired with hmjd after sorting |
| | `plan_groups_unique_objects` | Grouping by file; objects matched by two targets are extracted once |
| | `pipeline_end_to_end_resume_and_failure` | Synthetic mirror of 3 files plus 1 unreachable file. 3 ok and 1 failed (no crash); a rerun skips the 3 and retries the failure; mirror files are never deleted; `verify` passes |
| | `file_size_table` | Shipped size table has all 177,218 files and totals 7.97 TB; two files agree with IRSA Content-Length to < 2%; duplicate file ids are counted once |
| | `extract_refuses_large_download` | Above `max_gb`, extract raises before any download; the same files in a mirror count as 0 GB and extraction proceeds |
| | `release_filename` | DR23 index paths map to the DR24 tree (`lc_dr24/…_dr24.parquet`); unknown releases are rejected |
| | `run_dir_cannot_mix_releases` | A run directory records its release in `run.json`; extracting another release into it raises |
| `test_fetch.py` | `ok` | Download, and no second request if the file exists |
| | `retry_then_ok_and_retry_after` | 503 → backoff 2 s, 503 with Retry-After: 7 → waits 7 s, then success |
| | `short_read_is_retried` | A truncated body is rejected and retried, with no `.part` left behind |
| | `404_not_retried_and_no_file_left` | Fatal status: one request, no file |
| | `gives_up_after_retries` | Persistent 500 → exactly `retries` requests, then `FetchError` |
| `test_load.py` | `filter_epochs_basic` | Epoch mask rebuilds the lists and nepochs |
| | `load_raw_no_cuts` | Join to targets, band names, baseline, an objectid shared by two targets |
| | `load_catflags_and_min_epochs` | catflags cut; an object with all epochs flagged is dropped |
| | `load_primary` | Primary = most clean epochs |
| | `load_primary_tiebreak_baseline` | The ranking changes as expected when the cuts change |
| | `load_long` | Epoch-level output, row counts per object |
| `test_network.py` | `local_upload_equals_table_match` | Uploaded-list match gives exactly the same pairs as the WSDB-table match (COSMOS 1′ box) |
| | `extract_verify_load_and_api` | Real IRSA download → `verify` passes → `load(primary)` gives one row per (src, band) → API comparison passes |

**`verify` checks** (run on every real extraction): every matched objectid is present
exactly once, with no extra rows; `nepochs` equals the length of all five list columns;
hmjd is non-decreasing; filterid/fieldid/rcid agree with the index. nepochs must
equal the index value for DR23 and be at least that value for DR24.

**`validate-api`**: for a sample of objects (half random, half drawn from the 2,000
longest light curves), it queries
`nph_light_curves?ID=<oid>&COLLECTION=ztf_<release>` and matches epochs by time. An object passes if every parquet epoch has an API epoch within
1e-5 d and mag, magerr and catflags agree within 1e-5. API-only epochs are reported
separately.

## Validation run: COSMOS2020 Classic (2026-10-14)

All ZTF DR23 light curves within 1″ of every source in COSMOS2020 Classic
(WSDB table `vasily.cosmos2020_classic`, 1,720,700 sources over a 3.4 deg² bounding
box) were extracted in **about 11 minutes** in total. Almost all of that time was
download.

| Step | Result |
|---|---|
| match (1″) | 1,720,700 targets → 186,359 pairs, 88,676 sources, 185,079 objectids, 36 files; 16 s |
| extract | 36/36 files, 423.7 MB downloaded in 644 s (0.66 MB/s, 4 workers); 14,439,542 epochs; output 144 MB. The size estimate was 0.424 GB |
| verify | pass. catflags == 0 for 95.6% of epochs |
| validate-api (40 objects) | 40/40 pass. All 19,216 parquet epochs found; \|Δt\| ≤ 0.42 s, \|Δmag\| ≤ 5e-8 |
| `load(primary=True)` | 150,374 light curves for 87,110 sources (g 31,877, r 79,061, i 39,436); 1.2 s |

**Coverage.** Only 5.2% of COSMOS sources (88,676) have a ZTF object within 1″,
because most COSMOS sources are far fainter than the ZTF single-epoch limit (about
20.5–21 mag). After the catflags == 0 cut, with one primary light curve per source
and band:

| Band | Sources | Median clean epochs | 90th percentile | Sources with ≥ 100 epochs |
|---|---|---|---|---|
| g | 31,877 | 9 | 252 | 7,871 |
| r | 79,061 | 11 | 582 | 19,348 |
| i | 39,436 | 10 | 55 | 0 |

22,329 sources have light curves in all of g, r and i, and 38,335 in r only. Most
light curves are short: these are faint objects near the limit, detected on the
better nights only.

**Files.** The files used were field 468 (g, r, i; 9 quadrants each) and field 1462
(r; 9 quadrants). Each quadrant file covers about 0.74 deg², and 30.4% of the
609,226 objects in them were needed.

## DR24: COSMOS2020 Classic (2026-10-14)

The same match table was extracted again with `--release dr24` into `runs/cosmos_dr24`:

```bash
cp runs/cosmos/match.parquet runs/cosmos_dr24/
ztf-lc-bulk extract --out runs/cosmos_dr24 --release dr24 --workers 4
ztf-lc-bulk verify  --out runs/cosmos_dr24
ztf-lc-bulk validate-api --out runs/cosmos_dr24 --n 40        # queries ztf_dr24
python scripts/compare_releases.py runs/cosmos runs/cosmos_dr24
```

| Step | DR23 | DR24 |
|---|---|---|
| Download | 423.7 MB, 644 s (0.66 MB/s) | 476.8 MB, 679 s (0.70 MB/s); the estimate was 0.51 GB |
| Light curves / epochs | 185,079 / 14,439,542 | 185,079 / 16,343,555 (+13%) |
| Last epoch (MJD) | 60613 (2024 Oct) | 60961 (2025 Sep) |
| verify | pass | pass |
| validate-api (40 objects) | 40/40; 38 API-only epochs (0.2%) | 40/40; **0** API-only epochs; \|Δt\| ≤ 0.43 s, \|Δmag\| ≤ 5e-8 |
| catflags == 0 | 95.6% | 91.5% |
| Output | 144 MB | 163 MB |

**DR23 → DR24 per epoch** (all 185,079 objects, from `scripts/compare_releases.py`):
- Every DR23 epoch is present in DR24 with identical time, mag and clrcoeff.
- **magerr was recomputed for 99.999% of epochs.** The median ratio DR24/DR23 is
  0.998, with 1–99% range 0.989–1.107 and a maximum change of 0.012 mag. By band
  the median ratio is g 1.005, r 0.997, i 1.024.
- **catflags changed for 3,215 epochs (0.02%)**, almost all 0 → 32768, between MJD
  60296 and 60318 (Dec 2023–Jan 2024).
- **1,904,013 epochs were added.** Of these, 584k on nights MJD 60707–60709 (Jan 2025)
  are all flagged 32768: about 235k epochs per night, apparently a high-cadence
  COSMOS sequence. The `catflags == 0` cut removes them, which explains the lower
  clean fraction.
- DR24 also adds epochs inside the DR23 time span (MJD 60248–60613). This is
  consistent with the DR23 parquet lacking some epochs that the DR23 API had.

After `load(primary=True)` (catflags == 0):

| Band | Sources DR23 → DR24 | Median epochs | 90th percentile | ≥ 100 epochs | Median baseline (d) |
|---|---|---|---|---|---|
| g | 31,877 → 31,920 | 9 → 10 | 252 → 289 | 7,871 → 8,297 | 1,620 → 2,219 |
| r | 79,061 → 79,111 | 11 → 12 | 582 → 611 | 19,348 → 19,688 | 1,752 → 1,889 |
| i | 39,436 → 39,531 | 10 → 13 | 55 → 72 | 0 → 0 | 827 → 1,207 |

### Can the DR23 index be relied on for DR24?

**Short answer:** yes, for every object that already existed in DR23: you get its
complete, correct DR24 light curve. No, for objects that are new in DR24: they are
silently absent. In the files tested, the new objects are faint and almost all have
a single epoch.

**How DR24 is reached.** WSDB indexes DR23 only. `--release dr24` takes each matched
DR23 objectid and its DR23 file path and reads the file of the same field, band, CCD
and quadrant from `lc_dr24/` instead (`release_filename()`). This is valid only if
DR24 keeps the DR23 objectids in the same files.

**What was tested, and how.**

1. *Whole-file comparison of three COSMOS quadrants*
   (`scripts/compare_release_files.py`). Both releases of each file were downloaded,
   and **all** objects in the quadrant were compared, not just matched ones. An
   object is "new" if its objectid appears in the DR24 file but not in the DR23
   file; each file lists every object of its quadrant, so this is a complete count.

   | File | DR23 objects | DR24 objects | Only in DR23 | Only in DR24 (new) | Position shift | DR23 epochs all in DR24 | mag identical |
   |---|---|---|---|---|---|---|---|
   | `ztf_000468_zi_c03_q1` | 12,214 | 12,946 | 0 | 732 (5.7% of DR24) | 0″ | 12,214 / 12,214 | 12,214 / 12,214 |
   | `ztf_001462_zr_c10_q2` | 15,279 | 15,419 | 0 | 140 (0.9%) | 0″ | 15,279 / 15,279 | 15,279 / 15,279 |
   | `ztf_000468_zg_c08_q4` | 13,611 | 14,644 | 0 | 1,033 (7.1%) | 0″ | 13,611 / 13,611 | 13,611 / 13,611 |

   The new objects are short and faint:

   | File | New objects | With 1 epoch | With ≤ 5 epochs | With ≥ 10 epochs | Median mag |
   |---|---|---|---|---|---|
   | `ztf_000468_zg_c08_q4` | 1,033 | 90% | 100% | 0 | 21.27 |
   | `ztf_000468_zi_c03_q1` | 732 | 92% | 100% | 0 | 20.49 |
   | `ztf_001462_zr_c10_q2` | 140 | 99% | 100% | 0 | 20.46 |

   For comparison, the median over all objects in these DR24 files is 3–9 epochs.

2. *Full COSMOS run* (36 files, 185,079 matched objectids): every DR23 objectid was
   found in its DR24 file (`n_missing = 0`), every DR23 epoch is in DR24 with identical
   time and mag (`scripts/compare_releases.py`), and 40 of 40 objects agree exactly
   with the IRSA `ztf_dr24` API, which had no extra epochs.

**What can be relied on:**
- **Objects present in DR23 get their full DR24 light curves.** Shown in 1 and 2.
- **A breakdown of the index would be loud, not silent.** If a DR23 objectid were ever
  missing from its DR24 file (renumbered, moved or dropped), `extract` would count it
  in `missing` / `n_missing` (also in `manifest.jsonl`) and `verify` would fail. On
  COSMOS this count was 0 in all 36 files.

**What cannot be relied on:**
- **Objects new in DR24 are silently missing.** A target whose only ZTF counterpart
  first appears in DR24 gets no light curve and no warning.
- **The fraction of targets affected is not measured.** The 0.9–7.1% above counts new
  objects in whole quadrants (~0.74 deg² each), not within 1″ of targets. Given that
  the new objects have ≤ 5 epochs and median mag 20.5–21.3, the loss for light-curve
  work is expected to be small. That is an inference from three files, not a
  measurement.
- **Only COSMOS was tested:** 3 files compared in full, plus 36 files for the matched
  objects. Other fields, especially crowded ones, have not been checked.

**Recommendation.**
- Light curves of known, reasonably bright objects (variability studies): use DR24
  through the DR23 index.
- Completeness-sensitive work, such as a census down to the ZTF limit, or targets
  near the limit: the new DR24 objects matter. Either use DR23 consistently, or
  measure the loss first. Re-run with `--keep-raw`, match the targets against
  all objects in the downloaded DR24 files, and count targets that gain a new
  counterpart. That measurement has not been done yet.

## Known issues and caveats
- **The API's `ztf_dr23` collection has 0.2% more epochs than the DR23 parquet
  release**: 38 of about 19,250 in the sample, all at MJD 60248–60374 (Nov 2023–Mar
  2024) and 22 on night 60352. DR24 parquet and the `ztf_dr24` API agree exactly.
- **Throughput.** IRSA serves about 0.7–1.1 MB/s, and 4 workers were no faster than one
  `curl`. The download size is set by sky area, not by the number of sources: a whole
  file must be fetched to read any object in it. All of DR23 is 177,218 files and
  7.97 TB. All NSA v1_0_1 galaxies (641,409) would need 83,894 files, 1.48 TB:
  about 17 days at 1 MB/s.
- **DR24 relies on the DR23 index** and so silently misses objects new in DR24. If
  the index ever broke for an existing object, `verify` would fail (see
  [Can the DR23 index be relied on for DR24?](#can-the-dr23-index-be-relied-on-for-dr24)).
  The DR24 size estimate scales the DR23 sizes by 1.2 (measured per-file ratios:
  1.10–1.31; COSMOS total 1.125), so treat it as approximate. Including the new
  objects would need a DR24 index, or the IRSA HATS release on S3, best run from AWS
  us-east-1.
- **Remote partial reads do not help.** Each file is a single parquet row group, so
  DuckDB httpfs cannot skip rows.
- **`sqlutilpy.get(..., driver='duckdb')` scrambles list columns in sqlutilpy ≤ 0.27.**
  This is fixed in 0.28.0, which we verified on two DR23 files: 1–62 epochs, single
  and multi-row queries, ragged lengths. This package reads parquet with pyarrow and
  does not use the duckdb driver. If you query the files yourself with it, use
  sqlutilpy ≥ 0.28.
- **objectids are per band and field.** Several per source are normal, with
  complementary epochs and zero-point offsets between fields (median ~0.06 mag). Use
  `primary=True` or model the offsets; do not concatenate them blindly.

## Claude Code
`.claude/skills/ztf-lc/SKILL.md` tells Claude when and how to use this package instead of
writing new API downloaders. To use it from any project:
`ln -s ~/Work/Code/ztf_lc_bulk/.claude/skills/ztf-lc ~/.claude/skills/ztf-lc`.
