"""Normalize gcode metadata so AnkerMake/eufyMake ETA is not absurd.

The M5 firmware seeds remaining-time from a Cura-style ``;TIME:<seconds>``
comment near ``G28``. OrcaSlicer usually only writes a human footer like::

    ; estimated printing time (normal mode) = 12m 58s

and ``M73 P0 R12`` (remaining *minutes*). Without ``;TIME:``, the printer
and eufyMake app often show multi-day / +1000h countdowns even for short jobs.

See: https://github.com/OrcaSlicer/OrcaSlicer/issues/9457
     https://www.reddit.com/r/AnkerMake/comments/1l7vcmy/
"""

from __future__ import annotations

import logging as log
import re

_TIME_LINE = re.compile(rb"(?m)^;TIME:\s*\d+\s*$")
_EST_PRINTING = re.compile(
    rb"(?im);\s*estimated printing time(?:\s*\([^)]*\))?\s*=\s*([^\r\n]+)"
)
_LAYER_COUNT_LINE = re.compile(rb"(?m)^;LAYER_COUNT:\s*\d+\s*$")
_TOTAL_LAYER_NUM = re.compile(rb"(?im);\s*total layer number:\s*(\d+)")
_M73_P0 = re.compile(rb"(?im)^M73\s+P0\s+R(\d+)\b")
_G28_LINE = re.compile(rb"(?m)^(G28\b[^\r\n]*\r?\n)")
_TIME_TOKEN = re.compile(r"(\d+)\s*([dhms])", re.I)


def parse_human_duration_to_seconds(text: str) -> int | None:
    """Parse '12m 58s' / '1h 5m' / '2d 3h' into whole seconds."""
    total = 0
    found = False
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for value, unit in _TIME_TOKEN.findall(text or ""):
        found = True
        total += int(value) * mult[unit.lower()]
    if not found:
        return None
    return total


def estimate_print_seconds(data: bytes) -> int | None:
    """Best-effort seconds from Orca footer, else M73 P0 R<minutes>."""
    m = _EST_PRINTING.search(data)
    if m:
        human = m.group(1).decode("ascii", "ignore").strip()
        secs = parse_human_duration_to_seconds(human)
        if secs and secs > 0:
            return secs

    m = _M73_P0.search(data)
    if m:
        minutes = int(m.group(1))
        if minutes > 0:
            return minutes * 60
    return None


def layer_count_from_gcode(data: bytes) -> int | None:
    m = _TOTAL_LAYER_NUM.search(data)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None
    return None


def inject_ankermake_print_meta(data: bytes) -> bytes:
    """
    Ensure ``;TIME:`` (and ``;LAYER_COUNT:`` when known) exist near first G28.

    Idempotent: leaves an existing ``;TIME:`` line alone. Returns original
    bytes unchanged when nothing useful can be derived.
    """
    if not data or b"G28" not in data:
        return data

    already_time = bool(_TIME_LINE.search(data))
    already_layers = bool(_LAYER_COUNT_LINE.search(data))

    seconds = None if already_time else estimate_print_seconds(data)
    layers = None if already_layers else layer_count_from_gcode(data)

    if seconds is None and layers is None:
        return data

    insert_lines: list[bytes] = []
    if seconds is not None:
        insert_lines.append(f";TIME:{seconds}\n".encode("ascii"))
    if layers is not None:
        insert_lines.append(f";LAYER_COUNT:{layers}\n".encode("ascii"))

    if not insert_lines:
        return data

    m = _G28_LINE.search(data)
    if not m:
        return data

    patched = data[: m.start()] + b"".join(insert_lines) + data[m.start() :]
    parts = []
    if seconds is not None:
        parts.append(f";TIME:{seconds}")
    if layers is not None:
        parts.append(f";LAYER_COUNT:{layers}")
    log.info(
        "Injected Anker ETA metadata before G28 (%s) — "
        "printer/eufyMake remaining time should track the slicer estimate",
        ", ".join(parts),
    )
    return patched
