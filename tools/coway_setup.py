#!/usr/bin/env python3
"""Shim: run the stdlib-only setup CLI without installing the package."""

import importlib.util
import pathlib

_p = pathlib.Path(__file__).resolve().parent.parent / "coway_bridge/app/coway_bridge/setup_cli.py"
_s = importlib.util.spec_from_file_location("coway_setup_cli", _p)
_m = importlib.util.module_from_spec(_s)
_s.loader.exec_module(_m)

if __name__ == "__main__":
    raise SystemExit(_m.main())
