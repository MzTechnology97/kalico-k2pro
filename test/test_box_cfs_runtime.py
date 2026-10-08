import pathlib
import struct
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import box_cfs_runtime as runtime
from extras.serial_485 import crc8


ADDR = 1
CMD = runtime.CMD_RFID_DIAG
DEFAULTS_V1 = [255, 100, 200, 155, 255, 80]
DEFAULTS_V2 = [
    255, 100, 200, 155, 255, 80,
    1, 3200, 5000, 30000,
    160, 120, 100, 800, 200, 500, 1000, 5,
    3, 700, 300, 500, 25000, 10000, 10000, 3000,
]


def response(status, payload=b""):
    body = bytes((ADDR, len(payload) + 3, status, CMD)) + bytes(payload)
    return bytes((0xF7,)) + body + bytes((crc8(body[1:]),))


def descriptor(spec):
    if spec.value_type == "bool":
        ptype = runtime.TYPE_BOOL
    elif spec.maxval <= 0xff:
        ptype = runtime.TYPE_U8
    elif spec.maxval <= 0xffff:
        ptype = runtime.TYPE_U16
    else:
        ptype = runtime.TYPE_U32
    flags = runtime.FLAG_WRITABLE | runtime.FLAG_IDLE_ONLY | spec.flags
    kind = (
        runtime.KIND_TABLE_U8 if spec.param_id < 6
        else runtime.KIND_ROOT_U8 if spec.param_id == 6
        else runtime.KIND_SHADOW_U32
    )
    default = DEFAULTS_V2[spec.param_id]
    return bytes((spec.param_id, ptype, flags, kind)) + struct.pack(
        "<III", default, spec.minval, spec.maxval)


class FakeSerial:
    def __init__(self, api=2, busy=False):
        self.api = api
        self.busy = busy
        self.values = list(DEFAULTS_V2)
        self.mask = 0
        self.requests = []

    def cmd_send_data_with_response(self, body, timeout=1.0):
        body = bytes(body)
        self.requests.append(body)
        assert body[0] == ADDR
        assert body[2] == 0xFF
        assert body[3] == CMD
        sub = body[4]

        if sub == runtime.SUB_CONFIG_V2_INFO:
            if self.api < 2:
                return response(runtime.STATUS_BAD_REQUEST)
            payload = bytes((
                runtime.CONFIG_V2_VERSION,
                len(runtime.PARAMETERS),
                runtime.CONFIG_V2_DESCRIPTOR_SIZE,
                1,
            )) + struct.pack("<I", self.mask)
            return response(runtime.STATUS_OK, payload)

        if sub == runtime.SUB_CONFIG_V2_GET:
            if self.api < 2:
                return response(runtime.STATUS_BAD_REQUEST)
            param_id = body[5]
            payload = bytes((param_id, 0, 0, 0)) + struct.pack(
                "<I", self.values[param_id])
            return response(runtime.STATUS_OK, payload)

        if sub == runtime.SUB_CONFIG_V2_DESCRIBE:
            if self.api < 2:
                return response(runtime.STATUS_BAD_REQUEST)
            return response(
                runtime.STATUS_OK,
                descriptor(runtime.PARAM_BY_ID[body[5]]))

        if sub == runtime.SUB_CONFIG_V2_SET:
            if self.api < 2:
                return response(runtime.STATUS_BAD_REQUEST)
            if self.busy:
                return response(runtime.STATUS_STOCK_BUSY)
            param_id = body[5]
            value = struct.unpack_from("<I", body, 6)[0]
            spec = runtime.PARAM_BY_ID[param_id]
            if not spec.minval <= value <= spec.maxval:
                return response(runtime.STATUS_BAD_REQUEST)
            self.values[param_id] = value
            self.mask |= 1 << param_id
            payload = bytes((param_id, 0, 0, 0)) + struct.pack("<I", value)
            return response(runtime.STATUS_OK, payload)

        if sub == runtime.SUB_CONFIG_V2_RESET:
            if self.api < 2:
                return response(runtime.STATUS_BAD_REQUEST)
            if self.busy:
                return response(runtime.STATUS_STOCK_BUSY)
            param_id = body[5]
            if param_id == 0xff:
                self.values[:] = DEFAULTS_V2
                self.mask = 0
                value = 0
            else:
                self.values[param_id] = DEFAULTS_V2[param_id]
                self.mask &= ~(1 << param_id)
                value = self.values[param_id]
            payload = bytes((param_id, 0, 0, 0)) + struct.pack("<I", value)
            return response(runtime.STATUS_OK, payload)

        if sub == runtime.SUB_CONFIG_INFO:
            return response(
                runtime.STATUS_OK,
                bytes((runtime.CONFIG_VERSION, runtime.CONFIG_COUNT))
                + bytes(self.values[:6]))

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
            if param_id == 0xff:
                self.values[:6] = DEFAULTS_V1
            else:
                self.values[param_id] = DEFAULTS_V1[param_id]
            return response(runtime.STATUS_OK)

        raise AssertionError("unexpected subcommand 0x%02x" % sub)


def test_parameter_catalog_matches_v314_layout():
    assert len(runtime.PARAMETERS) == 26
    assert [p.param_id for p in runtime.PARAMETERS] == list(range(26))
    assert runtime.PARAM_BY_NAME["hub_forward_speed"].default == 100
    assert runtime.PARAM_BY_NAME["feeding_timeout_ms"].default == 25000
    assert runtime.PARAM_BY_NAME["odometer_stall_timeout_ms"].default == 500
    assert runtime.PARAM_BY_NAME["rfid_measure_speed"].flags & runtime.FLAG_RFID_SENSITIVE


def test_v2_probe_and_describe():
    serial = FakeSerial(api=2)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    info = driver.probe()
    assert info["version"] == 2
    assert info["count"] == 26
    desc = driver.describe_v2(22)
    assert desc["default"] == 25000
    assert desc["min"] == 1000
    assert desc["max"] == 120000
    assert desc["flags"] & runtime.FLAG_WRITABLE


def test_v2_get_set_and_reset_u32():
    serial = FakeSerial(api=2)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    assert driver.get_v2(8) == 5000
    assert driver.set_v2(8, 6500) == 6500
    assert driver.get_v2(8) == 6500
    assert driver.info_v2()["override_mask"] & (1 << 8)
    assert driver.reset_v2(8) == 5000
    assert driver.get_v2(8) == 5000
    assert not (driver.info_v2()["override_mask"] & (1 << 8))


def test_v2_reset_all_clears_overrides():
    serial = FakeSerial(api=2)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    driver.set_v2(1, 123)
    driver.set_v2(22, 30000)
    assert driver.info_v2()["override_mask"]
    assert driver.reset_v2() == 0
    assert driver.info_v2()["override_mask"] == 0
    assert serial.values == DEFAULTS_V2


def test_v1_probe_fallback_and_legacy_write():
    serial = FakeSerial(api=1)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    info = driver.probe()
    assert info["version"] == 1
    assert info["values"] == tuple(DEFAULTS_V1)
    driver.set_v1(1, 123)
    assert driver.info_v1()[1] == 123
    driver.reset_v1(1)
    assert driver.info_v1()[1] == 100


def test_busy_is_propagated_for_v1_and_v2():
    serial = FakeSerial(api=2, busy=True)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    with pytest.raises(runtime.CfsRuntimeBusy):
        driver.set_v2(1, 120)
    with pytest.raises(runtime.CfsRuntimeBusy):
        driver.reset_v2()

    serial = FakeSerial(api=1, busy=True)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    with pytest.raises(runtime.CfsRuntimeBusy):
        driver.set_v1(1, 120)
    with pytest.raises(runtime.CfsRuntimeBusy):
        driver.reset_v1()


def test_v2_set_uses_little_endian_u32_payload():
    serial = FakeSerial(api=2)
    driver = runtime.CfsRuntimeDriver(serial, ADDR)
    driver.set_v2(22, 30000)
    request = serial.requests[-1]
    assert request[4] == runtime.SUB_CONFIG_V2_SET
    assert request[5] == 22
    assert request[6:10] == struct.pack("<I", 30000)
