with q as materialized (
    select thing_id, ra, dec from sdssdr16qso.main
    where thing_id > 0 and psfmag[3] < 20.5 and ra between 180 and 190 and dec between 10 and 30
    order by q3c_ang2ipix(ra, dec))
select q.thing_id, (g.ra - q.ra) * cos(radians(q.dec)) * 3600 as dra_sg, (g.dec - q.dec) * 3600 as ddec_sg
from q cross join lateral (
    select ra, dec from gaia_dr3.gaia_source s
    where q3c_radial_query(s.ra, s.dec, q.ra, q.dec, 1. / 3600)
    order by q3c_dist(s.ra, s.dec, q.ra, q.dec) limit 1) g
