__version__ = "0.3.3"
__author__ = "Cristian Vlasceanu"
__copyright__ = "2026 Cristian Vlasceanu"
APP_NAME = "sturddle-view"

import os as _os


def app_dir_name() -> str:
    """APP_NAME with an optional instance suffix from SV_INSTANCE.

    SV_INSTANCE=2  =>  "sturddle-view-2"
    SV_INSTANCE="" =>  "sturddle-view"  (backwards compat)
    """
    suffix = _os.environ.get("SV_INSTANCE", "").strip()
    return f"{APP_NAME}-{suffix}" if suffix else APP_NAME
