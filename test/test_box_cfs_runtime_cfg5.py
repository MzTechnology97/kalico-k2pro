"""Offline CFG5 experimental Kalico contract; no physical CFS writes."""
import pytest
from test_box_cfs_runtime_k2oh import K2OH,OCFS,make,contact

CFG5=dict(K2OH,revision="v323-cfg5")
CFG4=dict(K2OH,revision="v323-cfg4")
def test_cfg5_writes_two_new_effective_ids_in_true_ms_only():
    rt,printer,drv=make({
        "auto_apply":"true",
        "feed_total_timeout_ms":"30000",
        "odometer_stall_timeout_ms":"600",
        "insert_sensor_timeout_ms":"8000",
        "load_retry_count":"1",
    },identity=CFG5,features=0xE7)
    contact(printer)
    assert drv.writes==[(8,8000),(20,30000),(22,600)]
    assert rt.apply_state.startswith("applied 3 value(s)")
    assert "load_retry_count" in rt.apply_state
    assert rt._cfg5_for_diagnostics(drv.probe()) is True
    assert rt._diagnostic_spec(drv.probe(),20).default==25000
    assert rt._diagnostic_spec(drv.probe(),22).default==500

@pytest.mark.parametrize("key,value",[
    ("feed_total_timeout_ms","25001"),
    ("odometer_stall_timeout_ms","501"),
    ("insert_sensor_timeout_ms","8001"),
])
def test_cfg5_bad_tick_precision_fails_closed(key,value):
    rt,printer,drv=make({key:value},identity=CFG5,features=0xE7)
    contact(printer)
    assert drv.writes==[]
    assert rt.apply_state.startswith("failed:")
    assert "multiple of" in rt.apply_state

def test_cfg4_does_not_write_id20_id22():
    rt,printer,drv=make({"feed_total_timeout_ms":"30000","odometer_stall_timeout_ms":"600"},identity=CFG4,features=0xD7)
    contact(printer)
    assert drv.writes==[]
    assert "skipped unsupported" in rt.apply_state
    assert rt._diagnostic_spec(drv.probe(),20).default==1250
    assert rt._diagnostic_spec(drv.probe(),22).default==25

@pytest.mark.parametrize("identity,feature",[(CFG5,0xD7),(CFG4,0xE7),(OCFS,0xE7),(None,0xE7)])
def test_cfg5_needs_exact_revision_feature_magic(identity,feature):
    rt,printer,drv=make({"feed_total_timeout_ms":"30000"},identity=identity,features=feature)
    contact(printer)
    assert drv.writes==[]
    assert not rt.apply_state.startswith("applied")

def test_cfg5_auto_apply_off():
    rt,printer,drv=make({"auto_apply":"false","feed_total_timeout_ms":"30000"},identity=CFG5,features=0xE7)
    contact(printer)
    assert drv.writes==[]
    assert rt.apply_state=="skipped: auto_apply is off"


def test_cfg5t_identification_and_live_remaining():
    identity = dict(K2OH, revision="v323-cfg5t")
    rt, printer, drv = make(
        {"auto_apply": "true", "feed_total_timeout_ms": "30000",
         "odometer_stall_timeout_ms": "600"},
        identity=identity, features=0xE5)
    contact(printer)
    assert drv.writes == [(20, 30000), (22, 600)]
    status = rt.get_status(0.)
    assert status["live_remaining"] is True
    assert status["identity"]["revision"] == "v323-cfg5t"
    assert rt._cfg5_for_diagnostics(drv.probe()) is True


def test_cfg5t_needs_exact_feature_and_revision():
    for identity, feature in (
            (dict(K2OH, revision="v323-cfg5t"), 0xD7),
            (dict(K2OH, revision="v323-cfg5"), 0xE5),
            (dict(K2OH, revision="v323-cfg4"), 0xE5),
            (OCFS, 0xE5)):
        rt, printer, drv = make(
            {"auto_apply": "true", "feed_total_timeout_ms": "30000"},
            identity=identity, features=feature)
        contact(printer)
        assert drv.writes == []
        assert rt.get_status(0.)["live_remaining"] is False
