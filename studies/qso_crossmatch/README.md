# QSO crossmatch study (SDSS DR16Q, DESI DR1 × ZTF DR23 objectids)

Results and figures: main README, section
[Crossmatching QSO catalogues](../../README.md#crossmatching-qso-catalogues-radius-gr-offsets-extra-objectids).

## Inputs
- WSDB: `sdssdr16qso.main`, `desi_dr1.zpix`, `desi_dr1.photometry`, `koposov.ztf_23_objs`,
  `gaia_dr3.gaia_source`.
- For the light-curve tests: DR23 light curves of all objectids within 1″ of DR16Q QSOs,
  one parquet per band, `$ZTF_QSO_LC_DIR/lc_filter{1,2}_chunk0.parquet` (default
  `~/data/ztf/qso`), with the IRSA columns (`objectid, hmjd, mag, catflags, ...`). Any
  extraction of those objectids works, e.g. `ztf_lc_bulk` with a 1″ match.

## Run (from an empty work directory; outputs land there)
```bash
source ~/Work/venvs/.venv/bin/activate
D=~/Work/Code/ztf_lc_bulk/studies/qso_crossmatch
python $D/pull.py sdss real;  python $D/pull.py sdss shift     # ~12 min each
python $D/pull.py desi real;  python $D/pull.py desi shift     # ~20 / 11 min
python $D/desi_dedup.py                                        # drops 8,997 duplicate targetids
python $D/q1_aperture.py      # Q1 tables, fig_q1_aperture.png, fig_q1_mag.png
python $D/q23.py 1.0          # Q2/Q3 statistics and light-curve tests (SDSS)
python $D/q3b.py              # Q3: what merging all objectids adds
python $D/gaia_check.py       # Q2: offsets against Gaia DR3 (~10 s)
python $D/figs23.py           # fig_q2.png, fig_q3.png, Q3 numbers for SDSS and DESI
```
Total ≈ 1 h, dominated by the WSDB pulls. Pair tables: SDSS 3.7M real / 0.53M shifted
rows; DESI 6.3M / 1.07M.

## WSDB notes
- Every query was checked with `EXPLAIN`: the plan must show a Bitmap Index Scan on the
  q3c index of the large table for each probe.
- `gaia_check.py` uses `LATERAL (q3c_radial_query ... ORDER BY q3c_dist LIMIT 1)`.
  A `q3c_join` with `DISTINCT ON ... ORDER BY q3c_dist` made the planner scan all
  1.8×10⁹ rows of `gaia_source` instead.
- Probe lists are sorted by `q3c_ang2ipix(ra, dec)`.
