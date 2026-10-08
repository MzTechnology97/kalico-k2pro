import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_cfs_runtime as runtime
from extras.serial_485 import crc8


ADDR = 1
CMD = runtime.CMD_RFID_DIAG
DEFAULTS = [255, 100, 200, 155, 255, 80]


def response(status, payload=b""):
    body = bytes((ADDR, len(payload) + 3, status, CMD)) + bytes(payload)
    return bytes((0xF7,)) + body + bytes((crc8(body[1:]),))


class FakeSerial:
    def __init__(self, busy=False):
        self.values = list(DEFAULTS)
        self.busy = busy
        self.requests = []

    def cmd_send_data_with_response(self, body, timeout=1.0):
        body = bytes(body)
        self.requests.append(body)
        assert body[0] == ADDR
        assert body[2] == 0xFF
        assert body[3] == CMD
        sub = body[4]
        if sub == runtime.SUB_CONFIG_INFO:
            return response(
                runtime.STATUS_OK,
                bytes((runtime.CONFIG_VERSION, runtime.CONFIG_COUNT))
                + bytes(self.values),
            )
        if sub == runtime.SUB_CONFIG_SET:
            if self.busy:
                return response(runtime.STATUS_STOCK_BUSY)
            param_id, value = body[5], body[6]
            self.values[param_id] = value
            return response(runtime.STATUS_OK, bytes((param_id, value)))
        if sub == runtime.SUB_CONFIG_RESET:
            if self.busy:
                return response(runtime.STATUS_STOCK_BUSY)
            param_id = body[5]
            if param_id == 0xFF:
                self.values[:] = DEFAULTS
            else:
                self.values[param_id] = DEFAULTS[param_id]
            return response(runtime.STATUS_OK)
        raise AssertionError("unexpected subcommand 0x%02x" % sub)


def test_parameter_map_matches_v313_firmware_layout():
    assert runtime.PARAMETERS == (
        ("feeder_forward_speed", 0, 255),
        ("hub_forward_speed", 1, 100),
        ("hub_transition_speed", 2, 200),
        ("hub_insert_speed", 3, 155),
        ("feeder_reverse_speed", 4, 255),
        ("hub_reverse_speed", 5, 80),
    )


def test_info_reads_all_runtime_values():
    serial = FakeSerial()
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    assert driver.info() == tuple(DEFAULTS)
    assert serial.requests[-1][4:] == bytes((runtime.SUB_CONFIG_INFO,))


def test_set_is_volatile_single_parameter_write():
    serial = FakeSerial()
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    driver.set(1, 123)
    assert serial.values[1] == 123
    assert driver.info()[1] == 123
    assert serial.requests[0][4:] == bytes((runtime.SUB_CONFIG_SET, 1, 123))


def test_reset_one_and_all_restore_stock_defaults():
    serial = FakeSerial()
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    driver.set(1, 123)
    driver.set(5, 90)
    driver.reset(1)
    assert serial.values == [255, 100, 200, 155, 255, 90]
    driver.reset()
    assert serial.values == DEFAULTS


def test_set_and_reset_propagate_stock_busy():
    serial = FakeSerial(busy=True)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    with pytest.raises(runtime.CfsRuntimeBusy):
        driver.set(1, 120)
    with pytest.raises(runtime.CfsRuntimeBusy):
        driver.reset()
