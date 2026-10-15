"""Bulk extraction of ZTF DR23 light curves from the IRSA parquet release.

Pipeline
--------
1. ``match``   : one q3c crossmatch of a target list against the WSDB index
                 ``koposov.ztf_23_objs`` (objectid -> file), producing a match table.
2. ``extract`` : for every parquet file that contains matched objects, obtain
                 the file (local mirror or HTTPS download from IRSA), read only
                 the matched objectids with pyarrow, sort epochs in time, and
                 write one output parquet per input file. Resumable.
3. ``verify``  : completeness and consistency checks on the output.
"""

__version__ = "0.3.0"

IRSA_BASE_URL = "https://irsa.ipac.caltech.edu/data/ZTF/lc/"
INDEX_OBJS = "koposov.ztf_23_objs"
INDEX_FILES = "koposov.ztf_23_files"
FILTER_NAMES = {1: "g", 2: "r", 3: "i"}

# Public API (imported after the constants above, which the submodules use)
from .match import match_local, match_wsdb_table, run_match, save_match, summarize_match  # noqa: E402
from .pipeline import run_extract, verify  # noqa: E402
from .load import load, select_primary, filter_epochs  # noqa: E402
from .exposures import build_exposures, read_exposures, nondetections  # noqa: E402

__all__ = ["match_local", "match_wsdb_table", "run_match", "save_match", "summarize_match",
           "run_extract", "verify", "load", "select_primary", "filter_epochs",
           "build_exposures", "read_exposures", "nondetections"]
