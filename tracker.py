#!/usr/bin/env python3
"""Thin entry point: `python tracker.py` runs the ArloBit outcome tracker daemon.

Implementation lives in arlobit/tracker.py. Research only — never trades.
"""

from arlobit.tracker import main

if __name__ == "__main__":
    raise SystemExit(main())
