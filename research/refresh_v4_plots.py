"""Refresh plots for the latest non-replay v4/live arena run."""

from __future__ import annotations

import sys

from research import refresh_plots


def main() -> int:
    sys.argv = [sys.argv[0], "--profile", "v4", *sys.argv[1:]]
    return refresh_plots.main()


if __name__ == "__main__":
    raise SystemExit(main())
