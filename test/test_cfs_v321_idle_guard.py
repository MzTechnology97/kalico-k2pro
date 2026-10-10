#!/usr/bin/env python3
"""Offline-only host safety regression for v3.21; NO printer I/O."""
import importlib.util
import sys
import types
from pathlib import Path

root = Path(__file__).resolve().parents[1]
extras = types.ModuleType("extras")
extras.__path__ = []
proto = types.ModuleType("extras.box_protocol")
proto.ProtocolError = type("ProtocolError", (Exception,), {})
serial = types.ModuleType("extras.serial_485")
serial.build_485_body = lambda *args, **kwargs: b""
sys.modules.update({"extras": extras, "extras.box_protocol": proto,
                    "extras.serial_485": serial})
spec = importlib.util.spec_from_file_location("v321", root / "klippy/extras/box_cfs_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

class Box:
    def __init__(self):
        self.data = {"state": "IDLE", "state_code": 0, "loaded_slot": -1,
                     "operation": {"active": False}}
        self.rfid_read_owner = None
        self.claim_count = 0
        self.release_count = 0
    def get_status(self, eventtime):return self.data
    def acquire_rfid_read(self, owner):
        if self.rfid_read_owner is not None:raise RuntimeError("RFID read busy")
        self.rfid_read_owner = owner
        self.claim_count += 1
    def release_rfid_read(self):
        self.rfid_read_owner = None
        self.release_count += 1
class Object:
    def __init__(self, **status): self.data=status
    def get_status(self, eventtime):return self.data
class Printer:
    def __init__(self, box, sensor, print_stats):
        self.objs={"box":box,"filament_switch_sensor filament_sensor":sensor,"print_stats":print_stats}
    def lookup_object(self, name, default=None):return self.objs.get(name,default)
class Reactor:
    def monotonic(self):return 100.0

box=Box();sensor=Object(filament_detected=False);stats=Object(state="standby")
instance=runtime.BoxCfsRuntime.__new__(runtime.BoxCfsRuntime)
instance.printer=Printer(box,sensor,stats);instance.reactor=Reactor()

def accepted():
    with instance._v321_claim_write({"features":0x97}):
        assert box.rfid_read_owner=="BOX_CFS_RUNTIME_WRITE"
    assert box.rfid_read_owner is None

accepted()
# Fresh empty idle, remembered RFID active slot is *not* a motion indicator.
box.active_slot_raw=1
accepted()
# Any moving/unloaded/unknown state, active command, sensor presence, active
# print job or RFID transaction must fail CLOSED.
cases=[(box.data,"state","PRINT"),(box.data,"state_code",2),
       (box.data,"loaded_slot",1),(box.data["operation"],"active",True),
       (sensor.data,"filament_detected",True),
       (sensor.data,"filament_detected",None),
       (stats.data,"state","printing"),
       (box.data,"state","NO_RESPONSE"),
       (box.data,"state_code",None)]
for obj,key,new in cases:
    old=obj.get(key)
    obj[key]=new
    try:
        with instance._v321_claim_write({"features":0x97}):
            raise AssertionError("Expected refusal, not yield")
    except runtime.CfsRuntimeBusy:
        pass
    else:raise AssertionError((key,new))
    finally:obj[key]=old
box.rfid_read_owner="EXTERNAL_RFID_TASK"
try:
    with instance._v321_claim_write({"features":0x97}):pass
except runtime.CfsRuntimeBusy:pass
else:raise AssertionError("RFID collision not rejected")
assert box.rfid_read_owner=="EXTERNAL_RFID_TASK"
box.rfid_read_owner=None
# A context-managed exception must release the RFID claim.
try:
    with instance._v321_claim_write({"features":0x97}):
        raise ValueError("simulated SET failed")
except ValueError:pass
assert box.rfid_read_owner is None
assert box.claim_count==box.release_count
print("PASS: v3.21 host preflight, all negative states fail-closed, RFID ownership serialized")
