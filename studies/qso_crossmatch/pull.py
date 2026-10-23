"""Pull every ZTF DR23 objectid within 5" of each SDSS DR16Q or DESI DR1 QSO, from the
WSDB index koposov.ztf_23_objs, plus a control sample with the QSO positions shifted by
+60" in Dec (same footprint and ZTF depth, no QSO).

Usage (run from the work directory; writes {cat}_{mode}.parquet and {cat}_{mode}.nqso):
    python pull.py sdss real ; python pull.py sdss shift
    python pull.py desi real ; python pull.py desi shift      # then: python desi_dedup.py

Timing on 2026-10-23: SDSS 12 min each, DESI 20 / 11 min. The probe list is sorted by
q3c_ang2ipix so that neighbouring probes reuse index and heap pages (cold, unsorted:
7.3 ms per probe; dense, sorted: 0.18 ms). EXPLAIN shows a Bitmap Index Scan on
ztf_23_objs_q3c_ang2ipix_idx per probe.
"""
import sys, time
import pandas as pd
import sqlutilpy as sqlutil

cat, mode = sys.argv[1], sys.argv[2]
shift = 60 / 3600. if mode == "shift" else 0.
src = {
    # SDSS DR16Q (Lyke et al. 2020): SDSS imaging position, PSF g and r
    "sdss": "select thing_id as qid, ra, dec + {s} as dec, z, psfmag[2] as gsdss, psfmag[3] as rsdss "
            "from sdssdr16qso.main where thing_id > 0",
    # DESI DR1 redshift catalogue: spectype QSO, zwarn 0, one row per targetid (zcat_primary).
    # Position and g, r from the Legacy Surveys photometry (photometry.targetid is unique).
    "desi": "select z.targetid as qid, p.ra, p.dec + {s} as dec, z.z, "
            "case when p.flux_g > 0 then 22.5 - 2.5 * log(p.flux_g::float8) else 'NaN'::float8 end as gsdss, "
            "case when p.flux_r > 0 then 22.5 - 2.5 * log(p.flux_r::float8) else 'NaN'::float8 end as rsdss "
            "from desi_dr1.zpix z join desi_dr1.photometry p using (targetid) "
            "where z.zcat_primary and z.spectype = 'QSO' and z.zwarn = 0",
}[cat]
q = f"""
with q as materialized (select * from ({src.format(s=shift)}) a order by q3c_ang2ipix(ra, dec))
select q.qid, q.z, q.gsdss, q.rsdss, o.objectid, o.filterid, o.fieldid, o.rcid, o.nepochs,
       q3c_dist(q.ra, q.dec, o.objra, o.objdec) * 3600. as sep,
       (o.objra - q.ra) * cos(radians(q.dec)) * 3600. as dra, (o.objdec - q.dec) * 3600. as ddec
from q, koposov.ztf_23_objs o
where q3c_join(q.ra, q.dec, o.objra, o.objdec, 5. / 3600)"""

t0 = time.time()
cols = ["qid", "z", "gsdss", "rsdss", "objectid", "filterid", "fieldid", "rcid", "nepochs", "sep", "dra", "ddec"]
df = pd.DataFrame(dict(zip(cols, sqlutil.get(q))))
n = sqlutil.get(f"select count(*) from ({src.format(s=0)}) a")[0][0]   # QSOs in the input list
df.to_parquet(f"{cat}_{mode}.parquet")
open(f"{cat}_{mode}.nqso", "w").write(str(n))
print(cat, mode, len(df), "pairs", n, "qsos", round(time.time() - t0), "s")
