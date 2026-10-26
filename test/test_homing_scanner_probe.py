from types import SimpleNamespace

import pytest

from klippy.extras import homing


class CommandError(Exception):
    pass


def _homing(scanner):
    printer = SimpleNamespace(
        lookup_object=lambda name, default=None: (
            scanner if name == "cartographer" else default
        ),
        command_error=CommandError,
    )
    obj = SimpleNamespace(printer=printer)
    obj._scanner_homes_z = lambda: homing.PrinterHoming._scanner_homes_z(obj)
    return obj


def _scanner(register_as_probe):
    return SimpleNamespace(
        config=SimpleNamespace(
            general=SimpleNamespace(register_as_probe=register_as_probe)
        ),
        scan_mode=SimpleNamespace(is_ready=False),
        _check_mcu_disconnected=lambda: True,
    )


def test_no_scanner():
    obj = _homing(None)
    assert homing.PrinterHoming._is_probe_model_ready(obj)
    homing.PrinterHoming._check_scanner_connected(obj)


def test_scanner_homing_z_needs_model_and_link():
    obj = _homing(_scanner(True))
    assert not homing.PrinterHoming._is_probe_model_ready(obj)
    with pytest.raises(CommandError):
        homing.PrinterHoming._check_scanner_connected(obj)


def test_mixed_mode_homes_z_without_the_scanner():
    # register_as_probe: False: prtouch homes Z, the Cartographer only scans
    obj = _homing(_scanner(False))
    assert homing.PrinterHoming._is_probe_model_ready(obj)
    homing.PrinterHoming._check_scanner_connected(obj)
