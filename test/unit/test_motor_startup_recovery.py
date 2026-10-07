"""Motor startup recovery after every startup attempt failed (K2-OpenHost).

Fakes only: no bus traffic, no real timers.
"""

import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "klippy"))

from extras import motor_control as mc  # noqa: E402


class Reactor:
    NEVER = 9.9e99

    def __init__(self):
        self.now = 100.0
        self.timers = {}

    def monotonic(self):
        return self.now

    def update_timer(self, timer, when):
        self.timers[timer] = when


def failing_step():
    raise RuntimeError("serial motor discovery timed out")


def make(print_state="standby"):
    m = mc.MotorControl.__new__(mc.MotorControl)
    m.reactor = Reactor()
    m.raw, m.info, m.begins = [], [], []
    m.gcode = SimpleNamespace(
        respond_raw=m.raw.append, respond_info=m.info.append
    )
    m.print_stats = SimpleNamespace(state=print_state)
    m.shutdown = False
    m.printer = SimpleNamespace(
        lookup_object=lambda name, default=None: (
            m.print_stats if name == "print_stats" else default
        ),
        is_shutdown=lambda: m.shutdown,
    )
    m.is_homing = False
    m.is_ready = False
    m.motor_params_init = False
    m.auto_retry = True
    m.startup_retry_limit = 8
    m.retry_delay = 3.0
    m._startup_complete = False
    m._startup_step_index = 0
    m._startup_error = ""
    m._startup_allow_auto_retry = True
    # the retry budget is spent: the next failure is final
    m._startup_auto_retry_count = 8
    m._startup_recovery_timer = "recovery"
    m._startup_recovery_delay = mc.STARTUP_RECOVERY_MIN
    m._startup_recovery_active = False
    m._startup_recovery_attempts = 0
    m._protection_poll_timer = "poll"
    m.temp_sensors = SimpleNamespace(start=lambda: None)
    m._startup_steps = lambda: [("serial_target_discovery", failing_step)]
    m._begin_startup = lambda force=False, allow_auto_retry=True: (
        m.begins.append((force, allow_auto_retry))
    )
    return m


def test_final_failure_schedules_a_recovery():
    m = make()
    assert m._startup_handler(m.reactor.now) == m.reactor.NEVER
    assert m._startup_complete and not m.is_ready
    assert len(m.raw) == 1 and "Retrying while idle" in m.raw[0]
    assert m.reactor.timers["recovery"] == 100.0 + mc.STARTUP_RECOVERY_MIN
    assert m._startup_recovery_delay == 2 * mc.STARTUP_RECOVERY_MIN


def test_recovery_attempt_runs_one_startup_while_idle():
    m = make()
    m._startup_handler(m.reactor.now)
    assert m._startup_recovery_handler(m.reactor.now) == m.reactor.NEVER
    assert m.begins == [(True, False)]
    assert m._startup_recovery_active and m._startup_recovery_attempts == 1


def test_recovery_failures_back_off_quietly_up_to_the_cap():
    m = make()
    m._startup_handler(m.reactor.now)
    for _ in range(8):
        m._startup_recovery_handler(m.reactor.now)
        m._startup_complete = False  # the scheduled single attempt runs ...
        m._startup_step_index = 0
        m._startup_allow_auto_retry = False
        m._startup_handler(m.reactor.now)  # ... and fails again
    assert len(m.raw) == 1  # only the first final failure reached the console
    assert m._startup_recovery_delay == mc.STARTUP_RECOVERY_MAX
    assert m.reactor.timers["recovery"] == 100.0 + mc.STARTUP_RECOVERY_MAX


def test_recovery_waits_while_printing_or_homing():
    m = make(print_state="printing")
    m._startup_handler(m.reactor.now)
    assert m._startup_recovery_handler(m.reactor.now) == (
        m.reactor.now + mc.STARTUP_RECOVERY_MIN
    )
    m.print_stats.state = "standby"
    m.is_homing = True
    m._startup_recovery_handler(m.reactor.now)
    assert m.begins == []


def test_recovery_stops_when_ready_or_shut_down():
    m = make()
    m._startup_handler(m.reactor.now)
    m.is_ready = True
    assert m._startup_recovery_handler(m.reactor.now) == m.reactor.NEVER
    m.is_ready = False
    m.shutdown = True
    assert m._startup_recovery_handler(m.reactor.now) == m.reactor.NEVER
    assert m.begins == []


def test_link_restored_retries_at_once_only_after_a_failure():
    m = make()
    m._handle_serial485_link_restored({})
    assert "recovery" not in m.reactor.timers  # startup never failed
    m._startup_handler(m.reactor.now)
    m._startup_recovery_delay = mc.STARTUP_RECOVERY_MAX
    m._handle_serial485_link_restored({})
    assert m.reactor.timers["recovery"] == m.reactor.now + 1.0
    assert m._startup_recovery_delay == mc.STARTUP_RECOVERY_MIN


def test_success_after_recovery_is_reported_and_resets_the_backoff():
    m = make()
    m._startup_recovery_active = True
    m._startup_recovery_attempts = 2
    m._startup_recovery_delay = mc.STARTUP_RECOVERY_MAX
    m._startup_steps = lambda: []
    assert m._startup_handler(m.reactor.now) == m.reactor.NEVER
    assert m.is_ready
    assert any("RS-485 link came back" in msg for msg in m.info)
    assert not m._startup_recovery_active
    assert m._startup_recovery_delay == mc.STARTUP_RECOVERY_MIN
