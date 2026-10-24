import importlib.util
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "upstream_sync", ROOT / "scripts" / "upstream_sync.py"
)
upstream_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream_sync)

OURS = {"klippy/extras/box.py", "klippy/gcode.py"}


@pytest.mark.parametrize(
    "files, category, flags",
    [
        (["klippy/toolhead.py"], "red", []),
        (["klippy/chelper/c_helper.so", "klippy/gcode.py"], "red", ["ours"]),
        (["src/stepper.c", "klippy/mcu.py"], "red", ["MCU"]),
        (["src/stm32/spi.c"], "other", []),
        (["docs/Config_Reference.md", ".github/workflows/x.yaml"], "other", []),
        (["klippy/extras/box.py"], "yellow", ["K2"]),
        (["klippy/extras/new_sensor.py"], "green", []),
        (["config/k2/printer.cfg"], "green", ["K2"]),
        (["config/printer-foo.cfg", "README.md"], "other", []),
    ],
)
def test_classify(files, category, flags):
    assert upstream_sync.classify(files, OURS) == (category, flags)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        + list(args),
        check=True,
        capture_output=True,
    )


def _commit(repo, path, text, message):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_report_on_a_small_history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "klippy/gcode.py", "base\n", "base")
    _commit(repo, "klippy/toolhead.py", "base\n", "base toolhead")
    _git(repo, "branch", "up")
    # our fork: a change of our own and a fix also sent upstream
    _commit(repo, "klippy/gcode.py", "ours\n", "our gcode change")
    _commit(repo, "klippy/fix.py", "fix\n", "shared fix")
    _git(repo, "branch", "-m", "k2-pro-openhost")
    _git(repo, "checkout", "-q", "up")
    _commit(repo, "klippy/fix.py", "fix\n", "shared fix, upstream copy")
    _commit(repo, "klippy/toolhead.py", "faster\n", "toolhead timing")
    _commit(repo, "klippy/gcode.py", "theirs\n", "upstream gcode change")
    _commit(repo, "klippy/new.py", "new\n", "new module")
    _commit(repo, "docs/x.md", "doc\n", "docs")
    skipped = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / ".github").mkdir()
    cfg = {
        "target": "k2-pro-openhost",
        "upstreams": [{"name": "jacob", "repo": "J/k", "branch": "main"}],
        "decisions": {skipped: {"decision": "skipped", "note": "docs"}},
    }
    (repo / ".github" / "upstream-sync.json").write_text(json.dumps(cfg))

    report = upstream_sync.build_report(
        str(repo), cfg, "k2-pro-openhost", {"jacob": "up"}
    )
    up = report["upstreams"][0]
    assert up["counts"] == {
        "red": 1,
        "yellow": 1,
        "green": 1,
        "other": 0,
        "present": 1,
        "via": 0,
        "decided": 1,
    }
    by_subject = {p["subject"]: p["category"] for p in up["pending"]}
    assert by_subject == {
        "toolhead timing": "red",
        "upstream gcode change": "yellow",
        "new module": "green",
    }
    assert up["merge"] == {"clean": False, "conflicts": ["klippy/gcode.py"]}
    body = upstream_sync.render_markdown(report)
    assert "1 conflicts" in body and "toolhead timing" in body
    assert "shared fix" not in body.split("## J/k")[1]


def test_long_reports_are_truncated():
    commit = {
        "sha": "a" * 40,
        "date": "2026-10-25",
        "author": "x",
        "subject": "s" * 200,
        "files": ["klippy/toolhead.py"],
        "category": "red",
        "flags": [],
    }
    up = {
        "name": "jacob",
        "repo": "J/k",
        "branch": "main",
        "sha": "b" * 40,
        "via": None,
        "behind": 500,
        "counts": dict.fromkeys(
            upstream_sync.CATEGORIES + ("present", "via", "decided"), 0
        ),
        "pending": [commit] * 500,
    }
    report = {
        "generated": "now",
        "target": "t",
        "target_sha": "c" * 40,
        "upstreams": [up],
    }
    body = upstream_sync.render_markdown(report, limit=5000)
    assert len(body) < 5200 and "Truncated" in body
