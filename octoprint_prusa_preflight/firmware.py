"""Query the printer's own idea of its hardware over serial.

Prusa Buddy firmware knows what filament is loaded (``M865 I<tool>``, the
state its own file-print preview validates against) and which nozzle is
fitted (``M862.1 Q``, from EEPROM).  Both are ignored for serial-streamed
prints -- the firmware's checks only run when it opens a file itself -- so
OctoPrint has to ask and compare.

OctoPrint has no request/response primitive for serial.  The query is
correlated through two hooks: the ``gcode.sent`` hook spots our tagged
command leaving the wire and opens a capture window, and the
``gcode.received`` hook closes it when a line MATCHES THE EXPECTED ANSWER
(``name:`` for M865, ``M862.1 T..`` for M862.1 Q).  Completion is by
content, deliberately not by ``ok``: if a refresh runs while the comm
layer is still draining in-flight commands (e.g. right after a cancelled
print), their stray ``ok`` acknowledgements would close an ok-based window
before the answer arrived.  That exact race shipped in 0.1.0 and clobbered
good state.

Failure semantics, learned from the same incident: a query that times out
KEEPS the previous value -- only a successful answer may change the cache,
and only an explicit empty name (``name:---``) means "nothing loaded".

Deadlock note: OctoPrint calls the ``gcode.queuing`` hook (where the
pre-print gate runs) from its send loop -- the same loop that would have to
transmit a query.  The gate therefore only reads this cache, refreshed on
connect, after prints, and on a timer while the printer idles.

Expected responses::

    M865 I0        ->  name:PETG            (plus parameter lines)
    M862.1 Q T0    ->  echo:  M862.1 T0 P0.40 A0 F1
"""

import logging
import re
import threading
import time
from typing import Any, Dict, Optional

QUERY_TAG = "plugin:prusa_preflight:query"
RESPONSE_TIMEOUT = 10.0

# firmware spellings of "nothing loaded"
_EMPTY_NAMES = {"", "---", "none", "?"}

FILAMENT_ANSWER_RE = re.compile(r"^name:(.*)$", re.IGNORECASE)
NOZZLE_ANSWER_RE = re.compile(r"M862\.1\s+T(\d+)\s+P([\d.]+)\s+A(\d)\s+F(\d)")


def parse_filament_line(line: str) -> Optional[str]:
    """``name:PETG`` -> ``PETG``; an empty marker -> None (nothing loaded)."""
    match = FILAMENT_ANSWER_RE.match(line.strip())
    if not match:
        return None
    name = match.group(1).strip()
    if name.lower() in _EMPTY_NAMES:
        return None
    return name


def parse_nozzle_line(line: str) -> Optional[Dict[str, Any]]:
    match = NOZZLE_ANSWER_RE.search(line)
    if not match:
        return None
    return {
        "tool": int(match.group(1)),
        "diameter": float(match.group(2)),
        "hardened": match.group(3) == "1",
        "high_flow": match.group(4) == "1",
    }


class FirmwareState:
    """Cached loaded-filament and nozzle state, polled from the firmware."""

    def __init__(self, printer: Any, logger: logging.Logger, tool_count: int = 1) -> None:
        self._printer = printer
        self._logger = logger
        self._tool_count = tool_count

        self._query_lock = threading.Lock()  # one in-flight query at a time
        self._state_lock = threading.Lock()  # guards cache + capture state
        self._window_open = False
        self._answer_re: Optional[re.Pattern] = None
        self._answer_line: Optional[str] = None
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
                self._answer_line = None

    def on_gcode_received(self, comm_instance, line, *args, **kwargs) -> str:
        try:
            with self._state_lock:
                if self._window_open and self._answer_re is not None:
                    stripped = line.strip()
                    if stripped and self._answer_re.search(stripped):
                        self._answer_line = stripped
                        self._window_open = False
                        self._response_done.set()
        except Exception:
            self._logger.exception("Error matching query response line")
        return line

    # ------------------------------------------------------------------ #
    #  queries                                                            #
    # ------------------------------------------------------------------ #

    def _query(self, command: str, answer_re: re.Pattern) -> Optional[str]:
        """Send one command; return the line matching answer_re, or None on
        timeout.  None means "no answer", never "empty answer"."""
        with self._query_lock:
            self._response_done.clear()
            with self._state_lock:
                self._window_open = False
                self._answer_re = answer_re
                self._answer_line = None
            try:
                self._printer.commands(command, tags={QUERY_TAG})
            except Exception as e:
                self._logger.warning(f"Could not send {command}: {e}")
                return None
            if not self._response_done.wait(RESPONSE_TIMEOUT):
                self._logger.warning(f"{command} went unanswered within "
                                     f"{RESPONSE_TIMEOUT}s; keeping previous state")
                with self._state_lock:
                    self._window_open = False
                    self._answer_re = None
                return None
            with self._state_lock:
                self._answer_re = None
                return self._answer_line

    # ------------------------------------------------------------------ #
    #  cache                                                              #
    # ------------------------------------------------------------------ #

    def refresh(self) -> bool:
        """Poll the firmware; only while operational and idle.  A failed
        query keeps the previous value.  Never call from the comm thread
        (see module docstring)."""
        try:
            is_cancelling = getattr(self._printer, "is_cancelling", lambda: False)
            if not self._printer.is_operational() or self._printer.is_printing() \
                    or self._printer.is_paused() or self._printer.is_pausing() \
                    or is_cancelling():
                return False
        except Exception:
            return False

        any_success = False

        for tool in range(self._tool_count):
            line = self._query(f"M865 I{tool}", FILAMENT_ANSWER_RE)
            if line is not None:
                any_success = True
                with self._state_lock:
                    self._filaments[tool] = parse_filament_line(line)

            line = self._query(f"M862.1 Q T{tool}", NOZZLE_ANSWER_RE)
            if line is not None:
                nozzle = parse_nozzle_line(line)
                if nozzle is not None:
                    any_success = True
                    with self._state_lock:
                        self._nozzles[nozzle.pop("tool")] = nozzle

        if any_success:
            with self._state_lock:
                self._cache_time = time.monotonic()
            self._logger.debug(f"Firmware state refreshed: "
                               f"filaments={self._filaments} nozzles={self._nozzles}")
        return any_success

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
