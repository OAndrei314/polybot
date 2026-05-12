"""Shared low-level utilities used across all subpackages."""
from __future__ import annotations

import os
from datetime import datetime, timezone


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def tail_lines_bytes(path: str, keep_lines: int, chunk_size: int = 65536) -> bytes:
    """Read the last `keep_lines` lines of a file as raw bytes."""
    if keep_lines <= 0 or not os.path.exists(path):
        return b""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        if pos <= 0:
            return b""
        buf = b""
        line_count = 0
        while pos > 0 and line_count <= keep_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos, os.SEEK_SET)
            buf = f.read(read_size) + buf
            line_count = buf.count(b"\n")
    lines = buf.splitlines()[-keep_lines:]
    return (b"\n".join(lines) + b"\n") if lines else b""
