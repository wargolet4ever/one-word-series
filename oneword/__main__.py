"""Lets `python -m oneword` work exactly like the `oneword` command."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
