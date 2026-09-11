"""Pure-python tests for the parsing halves (no OctoPrint needed):

    python -m unittest discover tests
"""
import importlib.util
import os
import tempfile
import unittest

# Load the pure modules straight from their files: the package __init__
# imports OctoPrint itself, which the test environment does not have.
_PKG = os.path.join(os.path.dirname(__file__), "..", "octoprint_reality_check")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_PKG, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gcode_meta = _load("gcode_meta")
firmware = _load("firmware")


class TestFirmwareParsing(unittest.TestCase):
    def test_m865_response(self):
        # verbatim capture from a CORE One
        self.assertEqual(firmware.parse_filament_line("name:PETG"), "PETG")

    def test_m865_nothing_loaded(self):
        self.assertIsNone(firmware.parse_filament_line("name:---"))
        self.assertIsNone(firmware.parse_filament_line("name:"))
        self.assertIsNone(firmware.parse_filament_line("nozzle_temperature:230"))

    def test_m862_1_response(self):
        nozzle = firmware.parse_nozzle_line("echo:  M862.1 T0 P0.40 A0 F1")
        self.assertEqual(nozzle["tool"], 0)
        self.assertEqual(nozzle["diameter"], 0.4)
        self.assertFalse(nozzle["hardened"])
        self.assertTrue(nozzle["high_flow"])

    def test_m862_1_other_tool(self):
        nozzle = firmware.parse_nozzle_line("echo:  M862.1 T1 P0.60 A1 F0")
        self.assertEqual(nozzle["tool"], 1)
        self.assertEqual(nozzle["diameter"], 0.6)
        self.assertTrue(nozzle["hardened"])

    def test_answer_res_ignore_ok_and_noise(self):
        # regression for the 0.1.0 clobber: stray oks and other traffic
        # around a cancelled print must not look like answers
        for noise in ("ok", "ok N123", "T:230.0 /230.0 B:85.0 /85.0",
                      "echo:busy: processing", "NORMAL MODE: Percent done: 3"):
            self.assertIsNone(firmware.FILAMENT_ANSWER_RE.match(noise.strip()))
            self.assertIsNone(firmware.NOZZLE_ANSWER_RE.search(noise))


class TestFailureKeepsState(unittest.TestCase):
    """A timed-out query must keep the previous cache, not clobber it."""

    class _DeadPrinter:
        def is_operational(self): return True
        def is_printing(self): return False
        def is_paused(self): return False
        def is_pausing(self): return False
        def is_cancelling(self): return False
        def commands(self, *a, **k): pass  # sends, but nothing ever answers

    def test_timeout_keeps_previous_value(self):
        import logging
        firmware.RESPONSE_TIMEOUT = 0.05  # don't wait 10s in tests
        state = firmware.FirmwareState(self._DeadPrinter(), logging.getLogger("t"))
        state._filaments[0] = "PETG"
        state._nozzles[0] = {"diameter": 0.4, "hardened": False, "high_flow": True}
        state._cache_time = 1.0
        self.assertFalse(state.refresh())          # nothing answered
        self.assertEqual(state.filament(0), "PETG")  # previous value survives
        self.assertEqual(state.nozzle(0)["diameter"], 0.4)


class TestQueryCorrelation(unittest.TestCase):
    """OctoPrint's real order is: sending hook, serial write, sent hook - and
    the reply is read on another thread, so it can land before the sent
    hook runs.  The window must already be open by then."""

    class _InstantPrinter(TestFailureKeepsState._DeadPrinter):
        state = None

        def commands(self, command, tags=None):
            self.state.on_gcode_sending(None, "sending", command, None, None, tags=tags)
            # the printer answers before the send thread reaches "sent"
            if command.startswith("M865"):
                self.state.on_gcode_received(None, "name:PLA")
            else:
                self.state.on_gcode_received(None, "echo:  M862.1 T0 P0.60 A0 F0")
            self.state.on_gcode_received(None, "ok")

    def test_instant_reply_is_captured(self):
        import logging
        firmware.RESPONSE_TIMEOUT = 0.05  # a regression fails fast, not in 10s
        printer = self._InstantPrinter()
        state = firmware.FirmwareState(printer, logging.getLogger("t"))
        printer.state = state
        self.assertTrue(state.refresh())
        self.assertEqual(state.filament(0), "PLA")
        self.assertEqual(state.nozzle(0)["diameter"], 0.6)


class TestGcodeMeta(unittest.TestCase):
    def _write(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_prusaslicer_footer(self):
        path = self._write(
            "M862.1 P0.4 A0 F1 ; nozzle check\n"
            "G1 X1 Y1\n" * 100 +
            "; filament used [mm] = 1234.5\n"
            "; filament_type = PETG\n"
            "; nozzle_diameter = 0.4\n"
        )
        meta = gcode_meta.parse(path)
        self.assertEqual(meta["filament_types"], ["PETG"])
        self.assertEqual(meta["nozzle_diameters"], [0.4])
        self.assertEqual(meta["filament_used"], [1234.5])
        self.assertTrue(gcode_meta.tool_used(meta, 0))

    def test_multi_tool_fields(self):
        path = self._write(
            "; filament used [mm] = 100.0, 0.0\n"
            "; filament_type = PETG;PLA\n"
            "; nozzle_diameter = 0.4,0.4\n"
        )
        meta = gcode_meta.parse(path)
        self.assertEqual(meta["filament_types"], ["PETG", "PLA"])
        self.assertEqual(meta["nozzle_diameters"], [0.4, 0.4])
        self.assertTrue(gcode_meta.tool_used(meta, 0))
        self.assertFalse(gcode_meta.tool_used(meta, 1))

    def test_missing_fields(self):
        path = self._write("G1 X1 Y1\n")
        meta = gcode_meta.parse(path)
        self.assertIsNone(meta["filament_types"])
        self.assertIsNone(meta["nozzle_diameters"])
        self.assertTrue(gcode_meta.tool_used(meta, 0))

    def test_value_line_straddling_head_boundary(self):
        # regression: a contiguous small file must not get a newline
        # injected at the 8KB head boundary - it tore "0.45" into "0.4"
        filler = "; " + "x" * 8170 + "\n"  # pushes the next line across 8192
        path = self._write(filler + "; nozzle_diameter = 0.45\n"
                                    "; filament_type = PETG\n")
        meta = gcode_meta.parse(path)
        self.assertEqual(meta["nozzle_diameters"], [0.45])

    def test_empty_value_does_not_swallow_next_line(self):
        # regression: \s* spanned the newline, so "; filament_type =" read
        # "; nozzle_diameter = 0.4" as the filament type
        path = self._write("; filament_type =\n"
                           "; nozzle_diameter = 0.4\n"
                           "; filament used [mm] =\n"
                           "; nozzle_diameter = 0.6\n")
        meta = gcode_meta.parse(path)
        self.assertIsNone(meta["filament_types"])
        self.assertEqual(meta["nozzle_diameters"], [0.4])
        self.assertIsNone(meta["filament_used"])

    def test_crlf_line_endings(self):
        path = self._write("; filament_type = PETG;PLA\r\n"
                           "; nozzle_diameter = 0.4,0.6\r\n")
        meta = gcode_meta.parse(path)
        self.assertEqual(meta["filament_types"], ["PETG", "PLA"])
        self.assertEqual(meta["nozzle_diameters"], [0.4, 0.6])

    def test_footer_beyond_head_window(self):
        # config block must be found even in a file bigger than the head read
        filler = ("G1 X123.456 Y654.321 E0.12345\n" * 20000)
        path = self._write(filler + "; filament_type = PLA\n; nozzle_diameter = 0.6\n")
        meta = gcode_meta.parse(path)
        self.assertEqual(meta["filament_types"], ["PLA"])
        self.assertEqual(meta["nozzle_diameters"], [0.6])


if __name__ == "__main__":
    unittest.main()
