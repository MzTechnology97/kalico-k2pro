"""Load the K2 Pro profiles in Klippy batch mode, without the printer.

The K2 MCUs run Creality firmware, so the data dictionaries come from the
reference printer (test/dictionaries/k2pro, SAVE_MCU_DICTIONARIES). This
catches an upstream change that breaks a K2 module or the profile before it
reaches a printer, like the duplicate "probe" registration of the 2026-10-25
sync.
"""

import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DICTS = ROOT / "test" / "dictionaries" / "k2pro"
# config/k2/reference/ is a partial snapshot for the docs, not loadable
CONFIG = ROOT / "config" / "k2" / "printer.cfg"

# The profile opens the RS-485 bus and talks to the T113: on a printer host
# Klippy would use the real devices next to the running Klipper.
pytestmark = pytest.mark.skipif(
    os.path.exists("/dev/k2-rs485"), reason="running on a K2-OpenHost host"
)


def test_k2_profile_loads(tmp_path):
    home = tmp_path / "home"
    # macros/: the save_variables file of START_PRINT lives there
    for sub in ("config/macros", "gcodes", "logs"):
        (home / "printer_data" / sub).mkdir(parents=True)
    gcode = tmp_path / "test.gcode"
    gcode.write_text("G4 P100\n")
    args = [
        sys.executable,
        "-m",
        "klippy",
        str(CONFIG),
        "-i",
        str(gcode),
        "-o",
        str(tmp_path / "out.serial"),
        "-v",
        "-d",
        str(DICTS / "mcu.dict"),
        "-d",
        "nozzle_mcu=%s" % (DICTS / "nozzle_mcu.dict",),
        "-d",
        "rpi=%s" % (DICTS / "rpi.dict",),
    ]
    proc = subprocess.run(
        args,
        cwd=ROOT,
        env=dict(os.environ, HOME=str(home)),
        capture_output=True,
        text=True,
        timeout=300,
    )
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, log[-6000:]
    assert "Traceback" not in log, log[-6000:]
