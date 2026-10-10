"""Test the actual Box operation/write mutex methods without a printer."""

import ast
from contextlib import contextmanager
from pathlib import Path


class FakeBoxError(Exception):
    pass


def _box_mutex_class():
    src = (
        Path(__file__).resolve().parents[1] / "klippy" / "extras" / "box.py"
    ).read_text()
    tree = ast.parse(src)
    target = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "Box"
    )
    methods = {
        "acquire_cfs_runtime_write",
        "release_cfs_runtime_write",
        "_operation",
    }
    selected = [
        item
        for item in target.body
        if isinstance(item, ast.FunctionDef) and item.name in methods
    ]
    assert len(selected) == len(methods)
    cls = ast.ClassDef(
        name="BoxMutexUnderTest",
        bases=[],
        keywords=[],
        body=selected,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    scope = {
        "contextmanager": contextmanager,
        "BoxError": FakeBoxError,
    }
    exec(compile(module, "<box-operation-mutex>", "exec"), scope)
    return scope["BoxMutexUnderTest"]


def test_operation_and_write_are_mutually_exclusive():
    box = _box_mutex_class()()
    box.operation_depth = 0
    box.operation_progress = None
    box.clog_baseline = None
    box.change_engine = type("ChangeEngine", (), {"pending": None})()
    box._cfs_runtime_write_owner = None

    box.acquire_cfs_runtime_write("runtime-set")
    assert box._cfs_runtime_write_owner == "runtime-set"
    try:
        with box._operation():
            raise AssertionError("Movement must not enter")
    except FakeBoxError:
        pass
    else:
        raise AssertionError("Movement was admitted during SET")
    assert box.operation_depth == 0

    try:
        box.release_cfs_runtime_write("other-owner")
    except FakeBoxError:
        pass
    else:
        raise AssertionError("Other writer released the lock")
    box.release_cfs_runtime_write("runtime-set")
    assert box._cfs_runtime_write_owner is None

    with box._operation():
        assert box.operation_depth == 1
        try:
            box.acquire_cfs_runtime_write("runtime-reset")
        except FakeBoxError:
            pass
        else:
            raise AssertionError("RESET admitted during movement")
    assert box.operation_depth == 0
    assert box._cfs_runtime_write_owner is None

    box.change_engine.pending = object()
    try:
        box.acquire_cfs_runtime_write("runtime-set")
    except FakeBoxError:
        pass
    else:
        raise AssertionError("Pending physical change must block SET")
    box.change_engine.pending = None
    box.acquire_cfs_runtime_write("runtime-set")
    box.release_cfs_runtime_write("runtime-set")
