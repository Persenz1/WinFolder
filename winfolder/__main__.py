# -*- coding: utf-8 -*-
"""Allow `python -m winfolder` and `pythonw -m winfolder`."""
from __future__ import annotations

from winfolder.app import main

if __name__ == "__main__":
    raise SystemExit(main())
