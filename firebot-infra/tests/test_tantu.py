import struct
import threading
import time
import unittest

from firebot_infra.drishti import MeasurementSpec, StateEngine, Status
from firebot_infra.framing import encode_frame
from firebot_infra.setu import make_pipe
from firebot_infra.tantu import (Binding, PeripheralRuntime, SimSensor, UartSensorDriver, build_bindings,
                                 register_driver, struct_parser)

SPEC = MeasurementSpec("temperature", "degC", max_age_s=1.0, min_value=-40, max_value=1200)


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def rig(driver, **kw):
    c = Clock()
    events = []
    eng = StateEngine(clock=c)
    rt = PeripheralRuntime(eng, clock=c, on_event=lambda *a, **k: events.append((a[1], k)))
    rt.bind(Binding("t1", driver, 0.1, SPEC, **kw))
    return c, eng, rt, events


class TantuTests(unittest.TestCase):
    def test_reading_flows_into_drishti(self):
        c, eng, rt, ev = rig(SimSensor("temperature", "degC", [21.5]))
        self.assertTrue(rt.poll("t1"))
        sv = eng.get("t1", "temperature")
        self.assertEqual((sv.status, sv.value), (Status.VALID, 21.5))
        self.assertEqual(rt.health()["t1"].state, "ok")

    def test_driver_unit_is_converted_by_drishti(self):
        c, eng, rt, _ = rig(SimSensor("temperature", "degF", [212.0]))
        rt.poll("t1")
        self.assertAlmostEqual(eng.get("t1", "temperature").value, 100.0)

    def test_errors_degrade_then_fail_then_reset_then_recover(self):
        sim = SimSensor("temperature", "degC", default=20.0)
        c, eng, rt, ev = rig(sim, max_failures=3)
        rt.poll("t1")
        sim.inject_error(3)
        for _ in range(2):
            self.assertFalse(rt.poll("t1"))
        self.assertEqual(rt.health()["t1"].state, "degraded")
        rt.poll("t1")
        h = rt.health()["t1"]
        self.assertEqual((h.state, h.consecutive_failures, h.resets, h.total_errors), ("failed", 3, 1, 3))
        self.assertEqual(sim.resets, 1)
        self.assertGreater(rt.next_delay("t1"), 0.1)           # backing off while failed
        self.assertTrue(rt.poll("t1"))
        self.assertEqual(rt.health()["t1"].state, "ok")
        self.assertEqual(rt.next_delay("t1"), 0.1)
        self.assertEqual([e["new"] for t, e in ev if t == "sensor_state"], ["ok", "degraded", "failed", "ok"])

    def test_failed_sensor_becomes_stale_in_drishti_not_fabricated(self):
        sim = SimSensor("temperature", "degC", [20.0])
        c, eng, rt, _ = rig(sim)
        rt.poll("t1")
        sim.inject_error(1)
        rt.poll("t1")
        c.t += 2.0
        self.assertEqual(eng.get("t1", "temperature").status, Status.STALE)

    def test_bad_values_are_rejected_by_drishti_and_counted(self):
        sim = SimSensor("temperature", "degC")
        c, eng, rt, _ = rig(sim, max_failures=2)
        sim.inject_value(float("nan")); sim.inject_value(5000.0)
        rt.poll("t1"); rt.poll("t1")
        h = rt.health()["t1"]
        self.assertEqual((h.rejected, h.state, h.total_errors), (2, "failed", 0))
        self.assertIn("out_of_range", h.last_error)
        self.assertEqual(eng.get("t1", "temperature").status, Status.UNAVAILABLE)

    def test_no_data_is_neutral(self):
        sim = SimSensor("temperature", "degC")
        c, eng, rt, _ = rig(sim)
        sim.inject_no_data(2)
        rt.poll("t1"); rt.poll("t1")
        h = rt.health()["t1"]
        self.assertEqual((h.no_data, h.consecutive_failures), (2, 0))

    def test_slow_reads_counted(self):
        class Slow(SimSensor):
            def read(self_inner):
                c.t += 0.5
                return super().read()
        c = Clock()
        eng = StateEngine(clock=c)
        rt = PeripheralRuntime(eng, clock=c)
        rt.bind(Binding("t1", Slow("temperature", "degC", default=20.0), 0.1, SPEC, slow_read_s=0.2))
        rt.poll("t1")
        self.assertEqual(rt.health()["t1"].slow_reads, 1)

    def test_uart_driver_reads_latest_valid_frame_and_counts_bad_ones(self):
        sa, sb = make_pipe()
        drv = UartSensorDriver(sb, "temperature", "degC", struct_parser(">f"))
        c, eng, rt, _ = rig(drv)
        bad = bytearray(encode_frame(struct.pack(">f", 99.0))); bad[4] ^= 0xFF
        sa.write(encode_frame(struct.pack(">f", 20.0)) + bytes(bad) + encode_frame(struct.pack(">f", 22.5))
                 + encode_frame(b"xy"))                          # wrong length -> parse error
        rt.poll("t1")
        self.assertEqual(eng.get("t1", "temperature").value, 22.5)
        self.assertEqual((drv.stats["frames"], drv.stats["bad_frames"], drv.stats["parse_errors"]), (2, 1, 1))
        self.assertTrue(rt.poll("t1"))                           # nothing new -> no_data, not an error
        sa.close()
        time.sleep(0.1)
        self.assertFalse(rt.poll("t1"))                          # closed port is a driver failure
        self.assertIn("stream closed", rt.health()["t1"].last_error)

    def test_hung_driver_does_not_stall_other_sensors(self):
        gate = threading.Event()
        hung = SimSensor("temperature", "degC", default=1.0)
        hung.block_until(gate)
        c = time.monotonic
        eng = StateEngine()
        eng.register(SPEC)
        rt = PeripheralRuntime(eng)
        rt.bind(Binding("hung", hung, 0.02, SPEC))
        rt.bind(Binding("ok", SimSensor("temperature", "degC", default=25.0), 0.02, SPEC))
        rt.start()
        try:
            time.sleep(0.3)
            self.assertEqual(eng.get("ok", "temperature").status, Status.VALID)
            self.assertEqual(eng.get("hung", "temperature").status, Status.UNAVAILABLE)
            self.assertGreater(rt.health()["ok"].total_reads, 3)
        finally:
            gate.set()
            rt.stop()

    def test_stop_is_prompt_and_closes_drivers(self):
        closed = []
        class D(SimSensor):
            def close(self): closed.append(1)
        eng = StateEngine()
        rt = PeripheralRuntime(eng)
        rt.bind(Binding("a", D("temperature", "degC", default=1.0), 5.0, SPEC))
        rt.start()
        t0 = time.monotonic()
        rt.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(closed, [1])

    def test_duplicate_binding_rejected(self):
        c, eng, rt, _ = rig(SimSensor("temperature", "degC"))
        with self.assertRaises(ValueError):
            rt.bind(Binding("t1", SimSensor("temperature", "degC"), 1))

    def test_build_from_config_with_resources_and_unknown_driver(self):
        sa, sb = make_pipe()
        cfg = [{"sensor_id": "u1", "driver": "uart_frames", "period_s": 0.05,
                "params": {"stream": "@port1", "measurement_type": "temperature", "unit": "degC",
                           "parse": struct_parser(">f")},
                "spec": {"measurement_type": "temperature", "unit": "degC", "max_age_s": 1.0}},
               {"sensor_id": "s1", "driver": "sim", "period_s": 0.05,
                "params": {"measurement_type": "distance", "unit": "m", "default": 1.0},
                "spec": {"measurement_type": "distance", "unit": "m", "max_age_s": 1.0}}]
        bs = build_bindings(cfg, {"port1": sb})
        self.assertEqual([b.sensor_id for b in bs], ["u1", "s1"])
        self.assertIs(bs[0].driver.stream, sb)
        with self.assertRaises(ValueError):
            build_bindings([{"sensor_id": "x", "driver": "mystery", "period_s": 1}])
        with self.assertRaises(ValueError):
            register_driver("sim", SimSensor)

    def test_event_callback_failure_does_not_affect_acquisition(self):
        c = Clock()
        eng = StateEngine(clock=c)
        rt = PeripheralRuntime(eng, clock=c, on_event=lambda *a, **k: 1 / 0)
        rt.bind(Binding("t1", SimSensor("temperature", "degC", [20.0]), 0.1, SPEC))
        self.assertTrue(rt.poll("t1"))
        self.assertEqual(eng.get("t1", "temperature").status, Status.VALID)


if __name__ == "__main__":
    unittest.main()
