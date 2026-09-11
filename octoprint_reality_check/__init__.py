# coding=utf-8
"""Reality Check: block serial prints whose gcode contradicts the printer.

Prusa Buddy printers validate filament/nozzle themselves for file-based
prints (USB, PrusaLink, Connect) but serial-streamed prints bypass those
checks entirely.  OctoPrint sits at the perfect chokepoint: it has the
whole file (comments included) and the serial port.  This plugin holds the
first job command in OctoPrint's gcode-queuing phase, compares the gcode's
assumptions against the printer's reported reality, and cancels on
mismatch (or warns, with warn_only).
"""
from __future__ import absolute_import, annotations

import collections
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional

import flask
import markupsafe
import octoprint.plugin
from octoprint.events import Events
from octoprint.filemanager import FileDestinations
from octoprint.util import RepeatedTimer

from octoprint_reality_check import gcode_meta
from octoprint_reality_check.firmware import FirmwareState

IGNORE_SOLUTION = "Ignore warning (change in plugin settings)"
PARANOID_OFF_SOLUTION = "Turn off paranoid mode (plugin settings)"

# OctoPrint reports Operational once the last lines are acknowledged, while
# the printer is still busy finishing (end-script moves etc.); queries sent
# in the first ~30s after a print went unanswered.  Skip polling this long.
SETTLE_SECONDS = 60


class RealityCheckPlugin(octoprint.plugin.StartupPlugin,
                         octoprint.plugin.SettingsPlugin,
                         octoprint.plugin.AssetPlugin,
                         octoprint.plugin.TemplatePlugin,
                         octoprint.plugin.SimpleApiPlugin,
                         octoprint.plugin.EventHandlerPlugin):

    def __init__(self):
        super().__init__()
        self._firmware: Optional[FirmwareState] = None
        self._refresh_timer = None
        self._gate_lock = threading.RLock()
        self._gate_result: Optional[bool] = None
        self._gate_path: Optional[str] = None
        # timer ticks to skip after a print (guarded by _gate_lock)
        self._dead_ticks = 0
        # bounded in-session history for the tab's collapsible event table;
        # the full trail lives in octoprint.log
        self._events = collections.deque(maxlen=20)

    # ------------------------------------------------------------------ #
    #  lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def initialize(self) -> None:
        self._firmware = FirmwareState(self._printer, self._logger,
                                       tool_count_provider=self._profile_tool_count)

    def _profile_tool_count(self) -> int:
        """Tool count comes from OctoPrint's printer profile - the same place
        the rest of the UI learns it (raise it there for a toolchanger)."""
        profile = self._printer_profile_manager.get_current_or_default() or {}
        return int((profile.get("extruder") or {}).get("count") or 1)

    def on_after_startup(self) -> None:
        # The gate runs in the comm send loop and must not wait on serial
        # (deadlock -- see firmware.py), so keep the cache warm while idle.
        self._refresh_timer = RepeatedTimer(self._interval(), self._refresh_tick,
                                            run_first=True)
        self._refresh_timer.start()
        self._logger.info("Reality Check armed")

    def _interval(self) -> int:
        # the template's min=5 is client-side only; a bad stored value must
        # not stall (None) or hammer (0/negative) the serial link
        return max(5, self._settings.get_int(["refresh_interval"]) or 30)

    def on_settings_save(self, data) -> None:
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        # apply without a restart: the timer is rebuilt for a changed
        # interval (under the lock - two concurrent saves must not leave an
        # orphan timer polling forever)
        with self._gate_lock:
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            self._refresh_timer = RepeatedTimer(self._interval(), self._refresh_tick,
                                                run_first=True)
            self._refresh_timer.start()

    def _refresh_tick(self) -> None:
        with self._gate_lock:
            if self._dead_ticks > 0:
                self._dead_ticks -= 1
                return
        if self._firmware is not None:
            self._firmware.refresh()

    def _refresh_async(self) -> None:
        if self._firmware is not None:
            threading.Thread(target=self._firmware.refresh,
                             name="reality_check_refresh", daemon=True).start()

    def on_event(self, event, payload) -> None:
        if event in (Events.PRINT_CANCELLED, Events.PRINT_DONE, Events.PRINT_FAILED):
            with self._gate_lock:
                self._gate_result = None
                self._gate_path = None
                # no refresh now: the printer is still busy finishing; let
                # the timer idle through the settle period (>= 2 ticks)
                self._dead_ticks = max(2, math.ceil(SETTLE_SECONDS / self._interval()))
        elif event == Events.CONNECTED:
            self._refresh_async()

    # ------------------------------------------------------------------ #
    #  comm hooks                                                         #
    # ------------------------------------------------------------------ #

    def on_gcode_sending(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if self._firmware is not None:
            self._firmware.on_gcode_sending(comm_instance, phase, cmd, cmd_type,
                                            gcode, *args, **kwargs)

    def on_gcode_received(self, comm_instance, line, *args, **kwargs):
        if self._firmware is not None:
            return self._firmware.on_gcode_received(comm_instance, line, *args, **kwargs)
        return line

    def gate_queuing(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        """Hold the first job command until the selected gcode passes."""
        tags = kwargs.get("tags") or set()
        if not ({"source:job", "source:file"} & tags):
            return None
        if ({"trigger:cancel", "trigger:comm.cancel"} & tags
                or "script:afterPrintCancelled" in tags):
            return None

        with self._gate_lock:
            path = self._selected_file_path(comm_instance)
            if path != self._gate_path:
                self._gate_path = path
                self._gate_result = None

            if self._gate_result is None:
                try:
                    self._gate_result = self._check(path)
                except Exception:
                    self._logger.exception("Unexpected Reality Check error")
                    self._gate_result = self._cannot_check(
                        "unexpected error (see octoprint.log)",
                        ["Check octoprint.log for the error"])

            if not self._gate_result:
                # OctoPrint ignores cancellation while still STARTING; the
                # hook fires again for the next job command once the state
                # is PRINTING, and the cancel sticks then.
                is_cancelling = getattr(self._printer, "is_cancelling", lambda: False)
                if not is_cancelling():
                    self._printer.cancel_print()
                return (None,)  # suppress this command

        return None

    def _selected_file_path(self, comm_instance) -> Optional[str]:
        is_sd = getattr(comm_instance, "isSdFileSelected", None)
        if is_sd and is_sd():
            return None
        current = getattr(comm_instance, "_currentFile", None)
        if current is not None:
            filename = current.getFilename()
            if filename:
                return filename
        job = self._printer.get_current_job() or {}
        file_info = job.get("file") or {}
        if file_info.get("path") and file_info.get("origin") == FileDestinations.LOCAL:
            return self._file_manager.path_on_disk(FileDestinations.LOCAL, file_info["path"])
        return None

    # ------------------------------------------------------------------ #
    #  the actual check                                                   #
    # ------------------------------------------------------------------ #

    def _check(self, path: Optional[str]) -> bool:
        """True = let the print through."""
        if not path or not os.path.isfile(path):
            return self._cannot_check(
                "could not read the selected gcode (SD-card prints are not checked)",
                ["Print from OctoPrint's local storage"])

        meta = gcode_meta.parse(path)
        if self._firmware is None or not self._firmware.known():
            return self._cannot_check(
                "no answer from the printer yet (M865 unsupported, or not "
                "polled since connect)",
                ["Refresh in the Reality Check tab, then print again"])

        problems: List[Dict[str, Any]] = []
        verified: List[str] = []
        skipped: List[str] = []

        if self._settings.get_boolean(["check_filament"]):
            if not meta["filament_types"]:
                skipped.append("filament (gcode declares no filament_type)")
            for tool, wanted in enumerate(meta["filament_types"] or []):
                if not gcode_meta.tool_used(meta, tool):
                    continue
                if not wanted:
                    skipped.append(f"filament (gcode declares none for tool {tool})")
                    continue
                loaded = self._firmware.filament(tool)
                if not self._firmware.filament_known(tool):
                    skipped.append(f"filament (no answer yet for tool {tool})")
                elif not self._firmware.filament_fresh(tool):
                    # the printer was busy (e.g. mid filament swap): the
                    # value predates it, so trust it neither way
                    skipped.append(f"filament (stale - last query for tool {tool} "
                                   f"went unanswered)")
                elif loaded is None:
                    skipped.append(f"filament (printer reports none loaded in tool {tool})")
                elif wanted.lower() != loaded.lower():
                    problems.append(dict(
                        text=f"gcode is sliced for {wanted} but printer has "
                             f"{loaded} (tool {tool}).",
                        solutions=[f"Slice to {loaded}",
                                   f"Load {wanted} into the printer",
                                   IGNORE_SOLUTION]))
                else:
                    verified.append(f"filament ({loaded})")

        if self._settings.get_boolean(["check_nozzle"]):
            if not meta["nozzle_diameters"]:
                skipped.append("nozzle (gcode declares no nozzle_diameter)")
            for tool, wanted_d in enumerate(meta["nozzle_diameters"] or []):
                if not gcode_meta.tool_used(meta, tool):
                    continue
                nozzle = self._firmware.nozzle(tool)
                if nozzle is None:
                    skipped.append(f"nozzle (no answer for tool {tool})")
                    continue
                if not self._firmware.nozzle_fresh(tool):
                    skipped.append(f"nozzle (stale - last query for tool {tool} "
                                   f"went unanswered)")
                    continue
                if abs(wanted_d - nozzle["diameter"]) > 0.01:
                    problems.append(dict(
                        text=f"gcode expects a {wanted_d}mm nozzle but printer "
                             f"has {nozzle['diameter']}mm (tool {tool}).",
                        solutions=[f"Slice for {nozzle['diameter']}mm",
                                   f"Fit a {wanted_d}mm nozzle",
                                   IGNORE_SOLUTION]))
                else:
                    verified.append(f"nozzle ({nozzle['diameter']}mm)")

        if skipped and self._settings.get_boolean(["fail_closed"]):
            problems.append(dict(
                text="Paranoid mode: could not verify " + ", ".join(skipped) + ".",
                solutions=["Fix the above, refresh in the Reality Check tab, "
                           "then print again",
                           PARANOID_OFF_SOLUTION]))

        if not problems:
            # Only claim what was actually compared.
            parts = []
            if verified:
                parts.append("verified: " + ", ".join(verified))
            if skipped:
                parts.append("SKIPPED: " + ", ".join(skipped))
            if not parts:
                parts.append("nothing to check")
            self._quiet_alert("Reality check passed - " + "; ".join(parts) + ".",
                              "info" if skipped else "success")
            return True

        return self._block(problems)

    def _cannot_check(self, reason: str, solutions: List[str]) -> bool:
        """The print can't be verified at all: fail open by default, closed
        in paranoid mode (fail_closed)."""
        if self._settings.get_boolean(["fail_closed"]):
            return self._block([dict(
                text=f"Paranoid mode: cannot verify this print - {reason}.",
                solutions=solutions + [PARANOID_OFF_SOLUTION])])
        self._quiet_alert(f"Reality Check cannot verify this print - {reason}; "
                          "letting it through.", "info")
        return True

    def _block(self, problems: List[Dict[str, Any]]) -> bool:
        """Pop every problem; False = stop the print (True under warn_only)."""
        warn_only = self._settings.get_boolean(["warn_only"])
        for problem in problems:
            numbered = [f"{i + 1}. {s}" for i, s in enumerate(problem["solutions"])]
            prefix = "(warn only) " if warn_only else ""
            plain = prefix + problem["text"] + " Solutions: " + " ".join(numbered)
            # values in problem text/solutions come from gcode comments and
            # firmware responses - hostile input; PNotify renders text as
            # HTML, so escape everything we interpolate and keep only our
            # own <br> markup
            esc = markupsafe.escape
            html = (str(esc(prefix + problem["text"])) + "<br>Solutions:<br>"
                    + "<br>".join(str(esc(n)) for n in numbered))
            self._alert(plain, "error", html=html)
        return warn_only

    def _alert(self, message: str, level: str = "info",
               html: Optional[str] = None, silent: bool = False) -> None:
        self._logger.info(f"[{level}] {message}")
        with self._gate_lock:  # RLock: also called while the gate holds it
            self._events.append(dict(time=time.time(), level=level, msg=message))
        self._plugin_manager.send_plugin_message(
            self._identifier,
            dict(type=level, msg=message, html=html, silent=silent))

    def _quiet_alert(self, message: str, level: str = "info") -> None:
        """Pass/skip/no-data chatter: popup only when popup_on_pass is set;
        always logged and always in the tab's event table."""
        silent = not self._settings.get_boolean(["popup_on_pass"])
        self._alert(message, level, silent=silent)

    # ------------------------------------------------------------------ #
    #  api                                                                #
    # ------------------------------------------------------------------ #

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return dict(refresh=[])

    def _events_snapshot(self) -> List[Dict]:
        with self._gate_lock:
            return list(self._events)

    def on_api_command(self, command: str, data: Dict):
        if command == "refresh" and self._firmware is not None:
            refreshed = self._firmware.refresh()
            return flask.jsonify(refreshed=refreshed, state=self._firmware.snapshot(),
                                 events=self._events_snapshot())
        return flask.abort(400)

    def on_api_get(self, request):
        if self._firmware is None:
            return flask.jsonify(state=None, events=self._events_snapshot())
        return flask.jsonify(state=self._firmware.snapshot(), events=self._events_snapshot())

    # ------------------------------------------------------------------ #
    #  boilerplate                                                        #
    # ------------------------------------------------------------------ #

    def get_settings_defaults(self):
        return {
            "check_filament": True,
            "check_nozzle": True,
            "warn_only": False,
            # paranoid mode: block (rather than pass) prints that can't be
            # fully verified - no printer answer, nothing loaded, no gcode
            # metadata, unreadable file
            "fail_closed": False,
            "refresh_interval": 30,
            # blocks always pop up; this also pops the pass/skip chatter
            # (everything is always logged + in the tab's event table)
            "popup_on_pass": False,
        }

    def get_template_configs(self):
        return [
            dict(type="tab", name="Reality Check", custom_bindings=True),
            dict(type="settings", name="Reality Check", custom_bindings=False),
        ]

    def get_assets(self):
        return {"js": ["js/reality_check.js"]}

    def get_update_information(self):
        # github_release, not github_commit: comparing a version string
        # against a commit SHA would show every fresh install a perpetual
        # "update available"
        return dict(
            reality_check=dict(
                displayName="Reality Check",
                displayVersion=self._plugin_version,
                type="github_release",
                user="BackSlasher",
                repo="OctoPrint-Reality-Check",
                current=self._plugin_version,
                pip="https://github.com/BackSlasher/OctoPrint-Reality-Check/archive/{target_version}.zip",
            )
        )


__plugin_name__ = "Reality Check"
__plugin_pythoncompat__ = ">=3,<4"


def __plugin_load__() -> None:
    global __plugin_implementation__
    __plugin_implementation__ = RealityCheckPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.gate_queuing,
        "octoprint.comm.protocol.gcode.sending": __plugin_implementation__.on_gcode_sending,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.on_gcode_received,
    }
