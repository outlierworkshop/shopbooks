"""Logging baseline. Isolation: SHOPBOOKS_DATA_DIR -> temp dir BEFORE importing db (mandatory), which
is exactly what proves the logger writes into the (temp) data dir and never into the real books."""
import os
import tempfile

os.environ["SHOPBOOKS_DATA_DIR"] = tempfile.mkdtemp(prefix="logtest_")

import db          # noqa: E402
import logutil     # noqa: E402

from testutil import ok  # noqa: E402

db.init()
logutil.log.warning("unit-test warning %s", 7)

datadir = os.environ["SHOPBOOKS_DATA_DIR"]
logpath = os.path.join(datadir, "logs", "shopbooks.log")

ok(os.path.exists(logpath), "log file is created under the data dir")
ok(os.path.abspath(logpath).startswith(os.path.abspath(datadir)),
   "log path is INSIDE SHOPBOOKS_DATA_DIR (never the real books)")
ok("unit-test warning 7" in open(logpath, encoding="utf-8").read(), "the warning line is written")

import shutil  # noqa: E402
shutil.rmtree(datadir, ignore_errors=True)
print("\nLOGUTIL TESTS DONE")
