#!/usr/bin/env python3
"""AutoBangumi entry with CD2 extension (no upstream main.py changes)."""

import logging
import os
import sys
from pathlib import Path

APP_DIR = Path(os.environ.get("AB_APP_DIR", "/app"))
CD2_BACKEND = Path("/extensions/cd2/backend")
HFZY_BACKEND = Path("/extensions/hfzy/backend")

# AutoBangumi core (module.*) must be importable before bootstrap patches it.
os.chdir(APP_DIR)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

for backend in (HFZY_BACKEND, CD2_BACKEND):
    if backend.is_dir() and str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("cd2.entry")

try:
    from cd2.bootstrap import install, is_installed

    install()
    if is_installed():
        logger.info("CD2 bootstrap OK — extension API routes registered")
    else:
        logger.error(
            "CD2 bootstrap did not complete; config save at "
            "/api/v1/extensions/cd2/config will return 404"
        )
except Exception:
    logger.exception("CD2 bootstrap crashed")

try:
    from hfzy.bootstrap import install as install_hfzy
    from hfzy.bootstrap import is_installed as hfzy_installed

    install_hfzy()
    if hfzy_installed():
        logger.info("HFZY bootstrap OK — misc feature routes registered")
    else:
        logger.error("HFZY bootstrap did not complete")
except Exception:
    logger.exception("HFZY bootstrap crashed")

if __name__ == "__main__":
    import runpy

    runpy.run_path(str(APP_DIR / "main.py"), run_name="__main__")
