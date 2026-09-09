"""Query the printer's own idea of its hardware over serial.

Prusa Buddy firmware knows what filament is loaded (``M865 I<tool>``, the
state its own file-print preview validates against) and which nozzle is
fitted (``M862.1 Q``, from EEPROM).  Both are ignored for serial-streamed
prints -- the firmware's checks only run when it opens a file itself -- so
OctoPrint has to ask and compare.

OctoPrint has no request/response primitive for serial.  The query is
correlated through two hooks plus the protocol's lockstep:

* the ``gcode.sent`` hook spots our tagged command leaving the wire and
  opens a capture window;
* serial is strictly ordered, so every ``gcode.received`` line until the
  closing ``ok`` belongs to that command.

Deadlock note: OctoPrint calls the ``gcode.queuing`` hook (where the
pre-print gate runs) from its send loop -- the same loop that would have to
transmit a query.  Waiting on serial from the gate would block the loop
that must answer.  The gate therefore only reads this cache, which is
refreshed on connect, after prints, and on a timer while the printer idles.
Worst case is one refresh interval of staleness; a stale mismatch produces
a spurious prompt, never a bad print.

Expected responses::

    M865 I0        ->  name:PETG            (plus parameter lines)
    M862.1 Q       ->  echo:  M862.1 T0 P0.40 A0 F1
"""

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

QUERY_TAG = "plugin:prusa_preflight:query"
RESPONSE_TIMEOUT = 10.0

# firmware spellings of "nothing loaded"
_EMPTY_NAMES = {"", "---", "none", "?"}

_NOZZLE_RE = re.compile(r"M862\.1\s+T(\d+)\s+P([\d.]+)\s+A(\d)\s+F(\d)")


class FirmwareState:
    """Cached loaded-filament and nozzle state, polled from the firmware."""

    def __init__(self, printer: Any, logger: logging.Logger, tool_count: int = 1) -> None:
        self._printer = printer
        self._logger = logger
        self._tool_count = tool_count

        self._query_lock = threading.Lock()  # one in-flight query at a time
        self._state_lock = threading.Lock()  # guards cache + capture state
        self._window_open = False
        self._captured: List[str] = []
        self._response_done = threading.Event()

        self._filaments: Dict[int, Optional[str]] = {}
        self._nozzles: Dict[int, Dict[str, Any]] = {}
        self._cache_time: Optional[float] = None

    # ------------------------------------------------------------------ #
    #  comm hooks (delegated from the plugin)                             #
    # ------------------------------------------------------------------ #

    def on_gcode_sent(self, comm_instance, phase, cmd, cmd_type, gcode,
                      subcode=None, tags=None, *args, **kwargs) -> None:
        if tags and QUERY_TAG in tags:
            with self._state_lock:
                self._window_open = True
                self._captured = []

    def on_gcode_received(self, comm_instance, line, *args, **kwargs) -> str:
        try:
            with self._state_lock:
                if self._window_open:
                    stripped = line.strip()
                    lowered = stripped.lower()
                    if lowered == "ok" or lowered.startswith("ok "):
                        self._window_open = False
                        self._response_done.set()
                    elif stripped:
                        self._captured.append(stripped)
        except Exception:
            self._logger.exception("Error capturing query response line")
        return line

    # ------------------------------------------------------------------ #
    #  queries                                                            #
    # ------------------------------------------------------------------ #

    def _query(self, command: str) -> Optional[List[str]]:
        """Send one command, return the response lines up to its ok."""
        with self._query_lock:
            self._response_done.clear()
            with self._state_lock:
                self._window_open = False
                self._captured = []
            try:
                self._printer.commands(command, tags={QUERY_TAG})
            except Exception as e:
                self._logger.warning(f"Could not send {command}: {e}")
                return None
            if not self._response_done.wait(RESPONSE_TIMEOUT):
                self._logger.warning(f"{command} went unanswered - Prusa Buddy "
                                     "firmware with M865/M862.1 support?")
                with self._state_lock:
                    self._window_open = False
                return None
            with self._state_lock:
                return list(self._captured)

    @staticmethod
    def parse_filament(lines: List[str]) -> Optional[str]:
        for line in lines:
            if line.lower().startswith("name:"):
                name = line.split(":", 1)[1].strip()
                if name.lower() in _EMPTY_NAMES:
                    return None
                return name
        return None

    @staticmethod
    def parse_nozzles(lines: List[str]) -> Dict[int, Dict[str, Any]]:
        nozzles = {}
        for line in lines:
            match = _NOZZLE_RE.search(line)
            if match:
                nozzles[int(match.group(1))] = {
                    "diameter": float(match.group(2)),
                    "hardened": match.group(3) == "1",
                    "high_flow": match.group(4) == "1",
                }
        return nozzles

    # ------------------------------------------------------------------ #
    #  cache                                                              #
    # ------------------------------------------------------------------ #

    def refresh(self) -> bool:
        """Poll the firmware; only while operational and idle.  Never call
        from the comm thread (see module docstring)."""
        try:
            if not self._printer.is_operational() or self._printer.is_printing() \
                    or self._printer.is_paused() or self._printer.is_pausing():
                return False
        except Exception:
            return False

        filaments = {}
        for tool in range(self._tool_count):
            lines = self._query(f"M865 I{tool}")
            filaments[tool] = self.parse_filament(lines) if lines else None

        lines = self._query("M862.1 Q")
        nozzles = self.parse_nozzles(lines) if lines else {}

        with self._state_lock:
            self._filaments = filaments
            self._nozzles = nozzles
            self._cache_time = time.monotonic()
        self._logger.debug(f"Firmware state refreshed: filaments={filaments} nozzles={nozzles}")
        return True

    def known(self) -> bool:
        with self._state_lock:
            return self._cache_time is not None

    def cache_age(self) -> Optional[float]:
        with self._state_lock:
            if self._cache_time is None:
                return None
            return time.monotonic() - self._cache_time

    def filament(self, tool: int) -> Optional[str]:
        with self._state_lock:
            return self._filaments.get(tool)

    def nozzle(self, tool: int) -> Optional[Dict[str, Any]]:
        with self._state_lock:
            return self._nozzles.get(tool)

    def snapshot(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "filaments": dict(self._filaments),
                "nozzles": {t: dict(n) for t, n in self._nozzles.items()},
                "age": None if self._cache_time is None else time.monotonic() - self._cache_time,
            }
