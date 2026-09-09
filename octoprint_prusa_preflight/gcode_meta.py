"""Extract the slicer's declared requirements from a gcode file.

PrusaSlicer writes its config block as comments at the END of the file
(thumbnails bloat the head), so read the tail generously plus a little of
the head and regex both.  The fields of interest::

    ; filament_type = PETG          (";"-separated per tool)
    ; nozzle_diameter = 0.4         (","-separated per tool)
    ; filament used [mm] = 1234.5   (","-separated; 0 = tool unused)
"""

import re
from typing import Any, Dict, List, Optional

HEAD_BYTES = 8 * 1024
TAIL_BYTES = 256 * 1024

_FILAMENT_RE = re.compile(r"^;\s*filament_type\s*=\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_NOZZLE_RE = re.compile(r"^;\s*nozzle_diameter\s*=\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_USED_RE = re.compile(r"^;\s*filament used \[mm]\s*=\s*(.+)$", re.MULTILINE | re.IGNORECASE)


def _read_head_tail(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        f.seek(0, 2)
        size = f.tell()
        if size > HEAD_BYTES + TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            tail = f.read(TAIL_BYTES)
        else:
            f.seek(HEAD_BYTES)
            tail = f.read()
    return (head + b"\n" + tail).decode("utf-8", errors="replace")


def _split(raw: str, sep: str) -> List[str]:
    return [part.strip() for part in raw.strip().split(sep)]


def parse(path: str) -> Dict[str, Any]:
    """Return {filament_types, nozzle_diameters, filament_used} lists (or
    None per field when the file does not declare it)."""
    text = _read_head_tail(path)

    filament_types: Optional[List[str]] = None
    match = _FILAMENT_RE.search(text)
    if match:
        filament_types = _split(match.group(1), ";")

    nozzle_diameters: Optional[List[float]] = None
    match = _NOZZLE_RE.search(text)
    if match:
        try:
            nozzle_diameters = [float(v) for v in _split(match.group(1), ",")]
        except ValueError:
            nozzle_diameters = None

    filament_used: Optional[List[float]] = None
    match = _USED_RE.search(text)
    if match:
        try:
            filament_used = [float(v) for v in _split(match.group(1), ",")]
        except ValueError:
            filament_used = None

    return {
        "filament_types": filament_types,
        "nozzle_diameters": nozzle_diameters,
        "filament_used": filament_used,
    }


def tool_used(meta: Dict[str, Any], tool: int) -> bool:
    """A tool with 0 mm of declared filament use is not part of the print."""
    used = meta.get("filament_used")
    if used is None or tool >= len(used):
        return True  # unknown -> assume used, so checks still run
    return used[tool] > 0
