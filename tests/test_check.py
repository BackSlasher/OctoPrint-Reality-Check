"""The gate's pass/block verdicts, with OctoPrint faked out:

    python -m unittest discover tests

The plugin package imports OctoPrint, Flask and markupsafe; any the test
environment lacks get a minimal stand-in so _check itself can run.
"""
import html
import logging
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    return module


def _install_stubs():
    try:
        import flask  # noqa: F401
    except ImportError:
        _stub("flask", jsonify=lambda **kw: kw, abort=lambda code: code)
    try:
        import markupsafe  # noqa: F401
    except ImportError:
        _stub("markupsafe", escape=lambda s: html.escape(str(s)))
    try:
        import octoprint.plugin  # noqa: F401
    except ImportError:
        _stub("octoprint")
        _stub("octoprint.plugin", **{name: type(name, (), {}) for name in (
            "StartupPlugin", "SettingsPlugin", "AssetPlugin", "TemplatePlugin",
            "SimpleApiPlugin", "EventHandlerPlugin")})
        _stub("octoprint.events", Events=type("Events", (), dict(
            PRINT_CANCELLED="PrintCancelled", PRINT_DONE="PrintDone",
            PRINT_FAILED="PrintFailed", CONNECTED="Connected")))
        _stub("octoprint.filemanager",
              FileDestinations=type("FileDestinations", (), dict(LOCAL="local")))
        _stub("octoprint.util", RepeatedTimer=object)


_install_stubs()
sys.path.insert(0, _ROOT)
import octoprint_reality_check as plugin_mod  # noqa: E402
from octoprint_reality_check.firmware import FirmwareState  # noqa: E402

TWO_TOOLS = ("; filament used [mm] = 100.0, 50.0\n"
             "; filament_type = PETG;PLA\n"
             "; nozzle_diameter = 0.4,0.4\n")


class _Settings:
    def __init__(self, values):
        self._values = values

    def get_boolean(self, path):
        return bool(self._values[path[0]])

    def get_int(self, path):
        return self._values[path[0]]


class TestCheck(unittest.TestCase):
    def _gcode(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _plugin(self, filaments=None, nozzles=None, known=True,
                stale_filaments=(), stale_nozzles=(), **settings):
        plugin = plugin_mod.RealityCheckPlugin()
        values = plugin.get_settings_defaults()
        values.update(settings)
        plugin._settings = _Settings(values)
        plugin._logger = logging.getLogger("reality_check_test")
        plugin._identifier = "reality_check"
        plugin._plugin_manager = mock.Mock()
        plugin._printer = mock.Mock()
        firmware = FirmwareState(None, plugin._logger)
        firmware._filaments.update(filaments or {})
        firmware._nozzles.update(nozzles or {})
        firmware._filament_fresh.update(
            {t: t not in stale_filaments for t in (filaments or {})})
        firmware._nozzle_fresh.update(
            {t: t not in stale_nozzles for t in (nozzles or {})})
        if known:
            firmware._cache_time = 1.0
        plugin._firmware = firmware
        return plugin

    def _levels(self, plugin):
        return [c.args[1]["type"]
                for c in plugin._plugin_manager.send_plugin_message.call_args_list]

    @staticmethod
    def _nozzle(diameter=0.4):
        return {"diameter": diameter, "hardened": False, "high_flow": False}

    def test_all_verified_passes_in_both_modes(self):
        path = self._gcode(TWO_TOOLS)
        for paranoid in (False, True):
            plugin = self._plugin({0: "PETG", 1: "PLA"},
                                  {0: self._nozzle(), 1: self._nozzle()},
                                  fail_closed=paranoid)
            self.assertTrue(plugin._check(path))
            self.assertEqual(self._levels(plugin), ["success"])

    def test_mismatch_blocks_in_both_modes(self):
        path = self._gcode(TWO_TOOLS)
        for paranoid in (False, True):
            plugin = self._plugin({0: "PETG", 1: "PETG"},
                                  {0: self._nozzle(), 1: self._nozzle()},
                                  fail_closed=paranoid)
            self.assertFalse(plugin._check(path))
            self.assertEqual(self._levels(plugin), ["error"])

    def test_nothing_loaded_in_used_tool(self):
        path = self._gcode(TWO_TOOLS)
        nozzles = {0: self._nozzle(), 1: self._nozzle()}
        self.assertTrue(self._plugin({0: "PETG", 1: None}, nozzles)._check(path))
        self.assertFalse(self._plugin({0: "PETG", 1: None}, nozzles,
                                      fail_closed=True)._check(path))

    def test_tool_never_polled(self):
        # profile extruder count left at 1: tool 1 has no cached answer
        path = self._gcode(TWO_TOOLS)
        self.assertTrue(self._plugin({0: "PETG"}, {0: self._nozzle()})._check(path))
        self.assertFalse(self._plugin({0: "PETG"}, {0: self._nozzle()},
                                      fail_closed=True)._check(path))

    def test_unused_tool_not_required(self):
        path = self._gcode("; filament used [mm] = 100.0, 0.0\n"
                           "; filament_type = PETG;PLA\n"
                           "; nozzle_diameter = 0.4,0.4\n")
        plugin = self._plugin({0: "PETG"}, {0: self._nozzle()}, fail_closed=True)
        self.assertTrue(plugin._check(path))

    def test_no_printer_answer(self):
        path = self._gcode(TWO_TOOLS)
        self.assertTrue(self._plugin(known=False)._check(path))
        self.assertFalse(self._plugin(known=False, fail_closed=True)._check(path))

    def test_unreadable_file(self):
        self.assertTrue(self._plugin()._check(None))
        self.assertFalse(self._plugin(fail_closed=True)._check(None))

    def test_missing_metadata(self):
        path = self._gcode("G1 X1 Y1\n")
        plugin = self._plugin({0: "PETG"}, {0: self._nozzle()})
        self.assertTrue(plugin._check(path))
        self.assertEqual(self._levels(plugin), ["info"])
        self.assertFalse(self._plugin({0: "PETG"}, {0: self._nozzle()},
                                      fail_closed=True)._check(path))

    def test_checks_disabled_is_not_unverifiable(self):
        path = self._gcode("G1 X1 Y1\n")
        plugin = self._plugin(fail_closed=True, check_filament=False,
                              check_nozzle=False)
        self.assertTrue(plugin._check(path))

    def test_warn_only_lets_paranoid_block_through(self):
        path = self._gcode(TWO_TOOLS)
        plugin = self._plugin(known=False, fail_closed=True, warn_only=True)
        self.assertTrue(plugin._check(path))
        self.assertEqual(self._levels(plugin), ["error"])

    def test_gate_error_fails_open_or_closed(self):
        comm = mock.Mock()
        comm.isSdFileSelected.return_value = False
        comm._currentFile.getFilename.return_value = "/x.gcode"
        for paranoid, expected in ((False, None), (True, (None,))):
            plugin = self._plugin(fail_closed=paranoid)
            plugin._printer.is_cancelling.return_value = False
            plugin._check = mock.Mock(side_effect=RuntimeError("boom"))
            verdict = plugin.gate_queuing(comm, "queuing", "G1 X1", None, "G1",
                                          tags={"source:job"})
            self.assertEqual(verdict, expected)
            self.assertEqual(plugin._printer.cancel_print.called, paranoid)

    def _last_msg(self, plugin):
        return plugin._plugin_manager.send_plugin_message.call_args.args[1]["msg"]

    def test_stale_filament_is_trusted_neither_way(self):
        # the printer was busy (e.g. mid filament swap) and left the latest
        # query unanswered: whether the kept value matches or not, it is
        # unverified - pass with SKIPPED by default, block in paranoid mode
        path = self._gcode(TWO_TOOLS)  # T0 wants PETG
        nozzles = {0: self._nozzle(), 1: self._nozzle()}
        for kept in ("PETG", "PLA"):
            plugin = self._plugin({0: kept, 1: "PLA"}, nozzles, stale_filaments={0})
            self.assertTrue(plugin._check(path))
            self.assertEqual(self._levels(plugin), ["info"])
            self.assertIn("filament (stale", self._last_msg(plugin))
            self.assertFalse(self._plugin({0: kept, 1: "PLA"}, nozzles,
                                          stale_filaments={0},
                                          fail_closed=True)._check(path))

    def test_stale_nozzle_is_trusted_neither_way(self):
        path = self._gcode(TWO_TOOLS)
        filaments = {0: "PETG", 1: "PLA"}
        nozzles = {0: self._nozzle(0.6), 1: self._nozzle()}  # T0 would mismatch
        plugin = self._plugin(filaments, nozzles, stale_nozzles={0})
        self.assertTrue(plugin._check(path))
        self.assertIn("nozzle (stale", self._last_msg(plugin))
        self.assertFalse(self._plugin(filaments, nozzles, stale_nozzles={0},
                                      fail_closed=True)._check(path))

    def test_polling_settles_after_print(self):
        # the printer is still busy finishing right after a print: no
        # immediate refresh, then ~60s of dead ticks (>= 2), then polling
        for event in ("PRINT_DONE", "PRINT_CANCELLED", "PRINT_FAILED"):
            for interval, dead in ((30, 2), (5, 12), (120, 2)):
                plugin = self._plugin(refresh_interval=interval)
                plugin._firmware = mock.Mock()
                plugin.on_event(getattr(plugin_mod.Events, event), {})
                for _ in range(dead):
                    plugin._refresh_tick()
                plugin._firmware.refresh.assert_not_called()
                plugin._refresh_tick()
                plugin._firmware.refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
