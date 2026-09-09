"""Pure-python tests for the parsing halves (no OctoPrint needed):

    python -m unittest discover tests
"""
import importlib.util
import os
import tempfile
import unittest

# Load the pure modules straight from their files: the package __init__
# imports OctoPrint itself, which the test environment does not have.
_PKG = os.path.join(os.path.dirname(__file__), "..", "octoprint_prusa_preflight")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_PKG, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gcode_meta = _load("gcode_meta")
FirmwareState = _load("firmware").FirmwareState


class TestFirmwareParsing(unittest.TestCase):
    def test_m865_response(self):
        # verbatim capture from a CORE One
        lines = ["name:PETG", "nozzle_temperature:230", "heatbed_temperature:85",
                 "is_abrasive:0", "requires_filtration:0"]
        self.assertEqual(FirmwareState.parse_filament(lines), "PETG")

    def test_m865_nothing_loaded(self):
        self.assertIsNone(FirmwareState.parse_filament(["name:---"]))
        self.assertIsNone(FirmwareState.parse_filament(["name:"]))
        self.assertIsNone(FirmwareState.parse_filament([]))

    def test_m862_1_response(self):
        lines = ["echo:  M862.1 T0 P0.40 A0 F1"]
        nozzles = FirmwareState.parse_nozzles(lines)
        self.assertEqual(nozzles[0]["diameter"], 0.4)
        self.assertFalse(nozzles[0]["hardened"])
        self.assertTrue(nozzles[0]["high_flow"])

    def test_m862_1_multi_tool(self):
        lines = ["echo:  M862.1 T0 P0.40 A0 F1", "echo:  M862.1 T1 P0.60 A1 F0"]
        nozzles = FirmwareState.parse_nozzles(lines)
        self.assertEqual(len(nozzles), 2)
        self.assertEqual(nozzles[1]["diameter"], 0.6)
        self.assertTrue(nozzles[1]["hardened"])


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

    def test_footer_beyond_head_window(self):
        # config block must be found even in a file bigger than the head read
        filler = ("G1 X123.456 Y654.321 E0.12345\n" * 20000)
        path = self._write(filler + "; filament_type = PLA\n; nozzle_diameter = 0.6\n")
        meta = gcode_meta.parse(path)
        self.assertEqual(meta["filament_types"], ["PLA"])
        self.assertEqual(meta["nozzle_diameters"], [0.6])


if __name__ == "__main__":
    unittest.main()
