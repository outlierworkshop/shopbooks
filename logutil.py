"""Central application logger.

The app has broad `except Exception` fallbacks by design (CLAUDE.md invariant 7: AI is optional, every
caller has a non-AI fallback), but a silent swallow is indistinguishable from success — if AI or a bank
feed quietly breaks for a month, nothing records that it ever tried. This module gives one logger that
writes to `<datadir>/logs/shopbooks.log` (rotating) plus the console, so those fallbacks can log *why*
they fell back without changing any behavior.

The log dir is resolved from `db.DATA` — i.e. it follows `SHOPBOOKS_DATA_DIR` exactly like the database,
so tests (which set that to a temp dir) never write logs into the real books.
"""
import logging
from logging.handlers import RotatingFileHandler

import db


def _build():
    log = logging.getLogger("shopbooks")
    if log.handlers:            # already configured (module re-imported)
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        logdir = db.DATA / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(str(logdir / "shopbooks.log"),
                                 maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception:
        pass  # never let logging setup itself break the app
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(ch)
    return log


log = _build()
