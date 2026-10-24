import importlib.util
import json
import pathlib
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "save_mcu_dictionaries",
    ROOT / "klippy" / "extras" / "save_mcu_dictionaries.py",
)
smd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smd)

DICT = {"app": "Klipper", "version": "v1", "commands": {"a": 1, "b": 2}}


class Error(Exception):
    pass


def _mcu(name, raw):
    parser = SimpleNamespace(get_raw_data_dictionary=lambda: raw)
    serial = SimpleNamespace(get_msgparser=lambda: parser)
    return SimpleNamespace(_serial=serial, get_name=lambda: name)


def _module(mcus, path):
    commands = {}
    gcode = SimpleNamespace(
        register_command=lambda name, func, desc: commands.update({name: func})
    )
    printer = SimpleNamespace(
        lookup_object=lambda name: gcode,
        lookup_objects=lambda module: [
            ("mcu " + m.get_name(), m) for m in mcus
        ],
    )
    config = SimpleNamespace(
        get_printer=lambda: printer, get=lambda key, default: str(path)
    )
    smd.load_config(config)
    return commands["SAVE_MCU_DICTIONARIES"]


def _gcmd():
    replies = []
    gcmd = SimpleNamespace(
        get=lambda key, default: default,
        respond_info=replies.append,
        error=Error,
    )
    return gcmd, replies


def test_saves_each_mcu_dictionary(tmp_path):
    raw = json.dumps(DICT).encode()
    cmd = _module(
        [_mcu("mcu", raw), _mcu("nozzle_mcu", raw), _mcu("rpi", "")], tmp_path
    )
    gcmd, replies = _gcmd()
    cmd(gcmd)
    saved = sorted(p.name for p in tmp_path.iterdir())
    assert saved == ["mcu.dict", "nozzle_mcu.dict"]
    assert json.loads((tmp_path / "mcu.dict").read_text()) == DICT
    assert "mcu: Klipper v1, 2 commands" in replies[0]


def test_no_dictionary_is_an_error(tmp_path):
    cmd = _module([_mcu("mcu", "")], tmp_path)
    gcmd, _ = _gcmd()
    with pytest.raises(Error):
        cmd(gcmd)
