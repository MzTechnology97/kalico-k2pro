"""Host-only regression checks; run with python3 test/unit/test_motor_telemetry.py."""
import ast
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'klippy/extras/motor_control.py'


def load_nodes(names):
    tree = ast.parse(SOURCE.read_text())
    nodes = [n for n in tree.body
             if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    namespace = dict(math=math, ALL_AXES=('x', 'y', 'e'),
                     GET_MCU_TEMP_INDEX=17, POLL_INTERVAL=6., POLL_TIMEOUT=.25)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.ns = load_nodes({'Mot2AxisTempSensor', 'Mot2TempSensorHub',
                              'describe_fault_detail', '_decode_mask'})
        self.clock = 100.
        self.fail = False
        self.queries = []
        def read(addr, index, **kwargs):
            self.queries.append((addr, index))
            if self.fail:
                raise TimeoutError('read timed out')
            return 42.
        reactor = SimpleNamespace(NEVER=float('inf'), register_timer=lambda cb: cb,
                                  update_timer=lambda *args: None,
                                  monotonic=lambda: self.clock)
        self.replacement = SimpleNamespace(
            reactor=reactor, is_ready=True, motor_params_init=True,
            printer=SimpleNamespace(add_object=lambda *args: None),
            axes=SimpleNamespace(target=lambda axis: SimpleNamespace(
                addr=axis, client=SimpleNamespace(get_value=read))))
        self.hub = self.ns['Mot2TempSensorHub'](self.replacement)

    def test_initial_poll_stale_failure_recovery_and_stop(self):
        self.assertIsNone(self.hub.get_status(100)['x']['temperature'])
        self.assertFalse(self.hub.get_status(100)['x']['valid'])
        self.hub.start()
        for _ in range(3): self.hub._poll(100)
        self.assertEqual(self.queries, [('x', 17), ('y', 17), ('e', 17)])
        self.assertTrue(self.hub.get_status(100)['x']['valid'])
        self.assertFalse(self.hub.get_status(137)['x']['valid'])
        self.fail = True
        self.hub._poll(100)
        sample = self.hub.get_status(100)['x']
        self.assertFalse(sample['valid'])
        self.assertEqual(sample['temperature'], 42.)
        self.assertEqual(sample['read_errors'], 1)
        self.assertEqual(sample['consecutive_errors'], 1)
        self.fail = False
        for _ in range(3): self.hub._poll(100)
        self.assertTrue(self.hub.get_status(100)['x']['valid'])
        self.assertEqual(self.hub.get_status(100)['x']['consecutive_errors'], 0)
        self.hub.stop()
        self.assertFalse(self.hub.get_status(100)['x']['valid'])

    def test_status_is_serializable_and_performs_no_io(self):
        self.ns.update(ERROR_CODE_LABELS={8: 'tracking error'},
                       WARNING_CODE_LABELS={2: 'MCU overheating'})
        tree = ast.parse(SOURCE.read_text())
        controller = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                          and n.name == 'MotorControl')
        method = next(n for n in controller.body if isinstance(n, ast.FunctionDef)
                      and n.name == 'get_status')
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(SOURCE), 'exec'), self.ns)
        fake = SimpleNamespace(
            reactor=self.replacement.reactor, motor_fault_detail={'e': {
                'error_code': 256, 'warning_code': 4, 'active': True}},
            _protection_last_query={'e': 95.},
            is_check_cut_pos_start=False, cut_state=False,
            _transport_ready_status=lambda: {}, is_homing=False,
            is_ready=True, motor_params_init=True, _startup_started=True,
            _startup_complete=True, _startup_step_index=3, _startup_error=None,
            stall_monitor=SimpleNamespace(read_all=lambda: {'x': 0}),
            temp_sensors=self.hub)
        status = self.ns['get_status'](fake, 100.)
        self.assertEqual(status['faults']['e']['error_labels'], ['tracking error'])
        self.assertEqual(status['faults']['e']['query_age'], 5.)
        self.assertFalse(status['faults']['x']['queried'])
        json.dumps(status, allow_nan=False)
        self.assertEqual(self.queries, [])
        for axis in self.hub.samples: self.assertIsNone(self.hub.samples[axis]['last_update'])


if __name__ == '__main__':
    unittest.main()
