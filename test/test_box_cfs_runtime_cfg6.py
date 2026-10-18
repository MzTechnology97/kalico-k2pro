"""Regression for strict K2OH CFG6 identity and 15 advanced SET callsites.
Host fixture records writes; it never communicates with a real CFS.
"""
import pytest
from test_box_cfs_runtime_k2oh import K2OH, OCFS, make, contact

CFG6=dict(K2OH,revision="v323-cfg6")
CUSTOM={
"reverse_detooth_enable":"false",
"hub_pullback_timeout_ms":"5500",
"rfid_seek_speed":"180",
"rfid_measure_speed":"150",
"rfid_reposition_speed":"130",
"rfid_seek_move_ms":"1000",
"rfid_seek_settle_ms":"250",
"rfid_post_auth_wait_ms":"1250",
"rfid_pre_measure_settle_ms":"500",
"rfid_neighbor_detect_delay_ms":"500",
"rfid_neighbor_settle_ms":"160",
"feed_recovery_timeout_ms":"3500",
"rfid_neighbor_retry_count":"2",
"reverse_detooth_timeout_ms":"1300",
"load_retry_count":"1",
}
EXPECTED={6:0,9:5500,11:180,12:150,13:130,14:1000,15:250,
          16:1250,17:500,18:500,19:160,21:3500,23:2,24:1300,27:1}
def test_all_15_cfg6_parameters_exposed_from_cfg_startup_only():
    rt,printer,drv=make(dict({"auto_apply":"true"},**CUSTOM),
                        identity=CFG6,features=0xE3)
    drv.values[6]=1  # stock reverse de-tooth enabled until CFG disables it
    contact(printer)
    assert drv.writes==sorted(EXPECTED.items())
    assert rt._cfg6_for_diagnostics(drv.probe())
    status=rt.get_status(0.)
    assert status["live_remaining"] is True
    assert status["identity"]["revision"]=="v323-cfg6"
    assert rt.apply_state.startswith("applied 15 value(s)")
    assert not status.get("ignored_options")

def test_cfg6_also_keeps_original_eight_writable_ids():
    options={"auto_apply":"true","hub_transition_speed":"220",
             "hub_insert_speed":"100","hub_reverse_speed":"150",
             "feed_total_timeout_ms":"30000",
             "odometer_stall_timeout_ms":"600"}
    rt,printer,drv=make(options,identity=CFG6,features=0xE3)
    contact(printer)
    assert drv.writes==[(2,220),(3,100),(5,150),(20,30000),(22,600)]
    assert rt.apply_state.startswith("applied 5 value(s)")

@pytest.mark.parametrize("ident,feature",[
    (CFG6,0xE5),
    (dict(K2OH,revision="v323-cfg5t"),0xE3),
    (dict(K2OH,revision="v323-cfg4"),0xE3),
    (OCFS,0xE3),
    (None,0xE3),
])
def test_mismatched_or_opencfs_firmware_gets_no_cfg6_set(ident,feature):
    rt,printer,drv=make(dict({"auto_apply":"true"},**CUSTOM),
                        identity=ident,features=feature)
    contact(printer)
    assert drv.writes==[]
    assert not rt.apply_state.startswith("applied")

def test_cfg6_auto_apply_off_never_writes():
    rt,printer,drv=make(dict({"auto_apply":"false"},**CUSTOM),
                        identity=CFG6,features=0xE3)
    contact(printer)
    assert drv.writes==[]
    assert rt.apply_state=="skipped: auto_apply is off"

@pytest.mark.parametrize("key,value",[
("insert_sensor_timeout_ms","8001"),
("feed_total_timeout_ms","25001"),
("odometer_stall_timeout_ms","501"),
])
def test_cfg6_strict_tick_units_and_no_writes_on_invalid(key,value):
    rt,printer,drv=make({"auto_apply":"true",key:value},
                        identity=CFG6,features=0xE3)
    contact(printer)
    assert drv.writes==[]
    assert "multiple of" in rt.apply_state

def test_cfg6_readonly_stock_dead_paths_skipped():
    # Exact v3.25 firmware rejects these IDs; host must ignore them.
    overrides={"auto_apply":"true","unload_sensor_timeout_ms":"900",
               "buffer_fill_timeout_ms":"20000",
               "transition_detect_timeout_ms":"350"}
    rt,printer,drv=make(overrides,identity=CFG6,features=0xE3)
    contact(printer)
    assert drv.writes==[]
    assert rt.apply_state=="skipped: no values configured"
