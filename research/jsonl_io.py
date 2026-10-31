"""Transparent reads for run telemetry stored as .jsonl or .jsonl.gz.

Completed runs under data/reports/live_competitor_tests are archived by
gzipping their bulky telemetry (portfolio_snapshots, leaderboard_history,
daily_metrics_history, loop_metrics). Live runs keep writing plain .jsonl,
so readers have to accept either form: `resolve` picks whichever exists and
the helpers below hide the difference from callers.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import IO, Any, Iterator

_TAIL_CHUNK = 65536


def resolve(path: Path) -> Path:
    """Return the archived .gz sibling when the plain file is absent."""
    path = Path(path)
    if path.exists():
        return path
    gz = path.with_name(path.name + ".gz")
    return gz if gz.exists() else path


def exists(path: Path) -> bool:
    return resolve(path).exists()


def open_text(path: Path, encoding: str = "utf-8") -> IO[str]:
    target = resolve(path)
    if target.suffix == ".gz":
        return gzip.open(target, "rt", encoding=encoding)
    return target.open("rt", encoding=encoding)


def open_binary(path: Path) -> IO[bytes]:
    target = resolve(path)
    if target.suffix == ".gz":
        return gzip.open(target, "rb")
    return target.open("rb")


def iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed objects, skipping blank and malformed lines."""
    if not exists(path):
        return
    with open_text(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def read_rows(path: Path) -> list[dict[str, Any]]:
    return list(iter_rows(path))


def last_line(path: Path) -> str | None:
    """Last non-blank line, read from the tail when the file is not gzipped."""
    target = resolve(path)
    if not target.exists():
        return None
    if target.suffix == ".gz":
        found: str | None = None
        with gzip.open(target, "rt", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip():
                    found = line.rstrip("\n")
        return found
    with target.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        read = 0
        chunk = b""
        while read < size:
            step = min(_TAIL_CHUNK, size - read)
            read += step
            f.seek(size - read)
            chunk = f.read(step) + chunk
            parts = [p for p in chunk.split(b"\n") if p.strip()]
            # A single part may still be truncated unless we reached the start.
            if len(parts) >= 2 or read == size:
                return parts[-1].decode("utf-8", errors="ignore") if parts else None
    return None


def mtime(path: Path) -> float | None:
    try:
        return resolve(path).stat().st_mtime
    except OSError:
        return None
