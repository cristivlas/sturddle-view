import os as _os
import sys as _sys
from pathlib import Path as _Path

from platformdirs import user_config_dir as _user_config_dir
from platformdirs import user_data_dir as _user_data_dir
from platformdirs import user_log_dir as _user_log_dir

__version__ = "0.6.0"
__author__ = "Cristian Vlasceanu"
__copyright__ = "2026 Cristian Vlasceanu"
APP_NAME = "sturddle-view"
INSTANCE_ENV = "SV_INSTANCE"


def app_dir_name() -> str:
    """APP_NAME with an optional instance suffix from SV_INSTANCE.

    SV_INSTANCE=2  =>  "sturddle-view-2"
    SV_INSTANCE="" =>  "sturddle-view"  (backwards compat)
    """
    suffix = _os.environ.get(INSTANCE_ENV, "").strip()
    return f"{APP_NAME}-{suffix}" if suffix else APP_NAME


def app_config_dir() -> _Path:
    """This instance's platform user-config dir."""
    return _Path(_user_config_dir(app_dir_name(), appauthor=False))


def app_data_dir() -> _Path:
    """This instance's platform user-data dir."""
    return _Path(_user_data_dir(app_dir_name(), appauthor=False))


def app_log_dir() -> _Path:
    """This instance's platform user-log dir."""
    return _Path(_user_log_dir(app_dir_name(), appauthor=False))


def is_windows() -> bool:
    """True on Windows. Reads sys.platform per call so tests can patch it."""
    return _sys.platform == "win32"
