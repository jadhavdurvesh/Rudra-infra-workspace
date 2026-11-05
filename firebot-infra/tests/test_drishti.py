import unittest

from firebot_infra.drishti import MeasurementSpec, Observation, StateEngine, Status


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def engine(**kw):
    c = Clock()
    e = StateEngine(clock=c, **kw)
    e.register(MeasurementSpec("distance", "m", max_age_s=1.0, min_value=0.0, max_value=50.0))
    e.register(MeasurementSpec("temperature", "degC", max_age_s=2.0, min_value=-40, max_value=1200))
    e.register(MeasurementSpec("position", "m", max_age_s=1.0, dims=2, frame_id="map"))
    return c, e


def obs(v=1.0, t=100.0, **kw):
    d = dict(sensor_id="s1", measurement_type="distance", value=v, unit="m", measurement_time=t)
    d.update(kw)
    return Observation(**d)


class DrishtiTests(unittest.TestCase):
    def test_unavailable_then_valid_then_stale(self):
        c, e = engine()
        self.assertEqual(e.get("s1", "distance").status, Status.UNAVAILABLE)
        self.assertTrue(e.ingest(obs()).accepted)
        self.assertEqual(e.get("s1", "distance").status, Status.VALID)
        self.assertEqual(e.latest_valid("s1", "distance"), 1.0)
        c.t = 101.5
        sv = e.get("s1", "distance")
        self.assertEqual(sv.status, Status.STALE)
        self.assertEqual(sv.value, 1.0)           # visible but marked stale
        self.assertIsNone(e.latest_valid("s1", "distance"))

    def test_malformed_rejected_and_preserved(self):
        _, e = engine()
        for bad in (float("nan"), float("inf"), "1.0", True, None, (1.0, 2.0)):
            self.assertEqual(e.ingest(obs(bad)).reason, "malformed", bad)
        self.assertEqual(e.ingest(obs(measurement_time=float("nan"))).reason, "malformed")
        self.assertEqual(e.ingest(obs(sensor_id="")).reason, "malformed")
        self.assertEqual(e.ingest(obs(quality="weird")).reason, "malformed")
        self.assertEqual(len(e.rejected), 9)
        self.assertEqual(e.get("s1", "distance").status, Status.UNAVAILABLE)

    def test_unknown_type(self):
        _, e = engine()
        self.assertEqual(e.ingest(obs(measurement_type="smell")).reason, "unknown_type")

    def test_out_of_order_and_future(self):
        c, e = engine()
        e.ingest(obs(1.0, 100.0))
        self.assertEqual(e.ingest(obs(2.0, 99.0)).reason, "out_of_order")
        self.assertEqual(e.get("s1", "distance").value, 1.0)
        self.assertEqual(e.ingest(obs(2.0, 105.0)).reason, "future_timestamp")
        self.assertTrue(e.ingest(obs(3.0, 100.0)).accepted)  # equal timestamp allowed

    def test_unit_mismatch_rejected_or_explicitly_converted(self):
        _, e = engine()
        self.assertTrue(e.ingest(obs(1500, unit="mm")).accepted)
        self.assertAlmostEqual(e.get("s1", "distance").value, 1.5)
        self.assertEqual(e.ingest(obs(1.0, unit="furlong")).reason, "unit_mismatch")
        _, strict = engine(auto_convert=False)
        self.assertEqual(strict.ingest(obs(1500, unit="mm")).reason, "unit_mismatch")
        strict.add_conversion("mm", "m", lambda v: v / 1000)
        self.assertTrue(strict.ingest(obs(1500, unit="mm")).accepted)
        self.assertAlmostEqual(strict.get("s1", "distance").value, 1.5)

    def test_temperature_conversion(self):
        _, e = engine()
        e.ingest(obs(212.0, measurement_type="temperature", unit="degF"))
        self.assertAlmostEqual(e.get("s1", "temperature").value, 100.0)

    def test_range(self):
        _, e = engine()
        self.assertEqual(e.ingest(obs(-1.0)).reason, "out_of_range")
        self.assertEqual(e.ingest(obs(51.0)).reason, "out_of_range")

    def test_frames(self):
        _, e = engine()
        p = dict(sensor_id="lidar", measurement_type="position", unit="m", measurement_time=100.0)
        self.assertEqual(e.ingest(Observation(value=(1.0, 2.0), **p)).reason, "frame_missing")
        self.assertEqual(e.ingest(Observation(value=(1.0, 2.0), frame_id="base", **p)).reason, "frame_mismatch")
        e.add_transform("base", "map", lambda v: (v[0] + 10.0, v[1] + 20.0))
        self.assertTrue(e.ingest(Observation(value=(1.0, 2.0), frame_id="base", **p)).accepted)
        self.assertEqual(e.get("lidar", "position").value, (11.0, 22.0))
        self.assertEqual(e.get("lidar", "position").frame_id, "map")
        self.assertEqual(e.ingest(Observation(value=(1.0,), frame_id="map", **p)).reason, "malformed")

    def test_degraded_and_bad_quality(self):
        _, e = engine()
        e.ingest(obs(quality="degraded"))
        self.assertEqual(e.get("s1", "distance").status, Status.DEGRADED)
        self.assertEqual(e.ingest(obs(quality="bad")).reason, "bad_quality")

    def test_subscribe_and_subscriber_errors_isolated(self):
        _, e = engine()
        seen = []
        e.subscribe(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        e.subscribe(lambda s, m, v: seen.append((s, m, v.status)))
        e.ingest(obs())
        self.assertEqual(seen, [("s1", "distance", Status.VALID)])
        self.assertEqual(e.counts["subscriber_errors"], 1)

    def test_freshness_transitions_emit_events(self):
        events = []
        c, e = engine(on_event=lambda *a, **k: events.append((a[1], k)))
        e.ingest(obs())
        c.t = 102.0
        self.assertEqual(e.check_freshness(), [("s1", "distance", Status.STALE)])
        self.assertEqual(e.check_freshness(), [])      # no repeat
        self.assertTrue(any(t == "status_change" and k["new"] == "stale" for t, k in events))

    def test_rejection_events_rate_limited_and_logger_failure_ignored(self):
        events = []
        c, e = engine(on_event=lambda *a, **k: events.append(1))
        for _ in range(50):
            e.ingest(obs(float("nan")))
        self.assertEqual(len(events), 1)
        _, e2 = engine(on_event=lambda *a, **k: 1 / 0)
        self.assertFalse(e2.ingest(obs(float("nan"))).accepted)   # no exception escapes

    def test_health(self):
        c, e = engine()
        e.ingest(obs())
        e.ingest(obs(float("nan")))
        h = e.health()
        self.assertEqual(h["by_status"], {"valid": 1})
        self.assertEqual(h["counts"]["rejected_malformed"], 1)


if __name__ == "__main__":
    unittest.main()


class FlatlineTests(unittest.TestCase):
    def mk(self, **kw):
        c = Clock()
        events = []
        e = StateEngine(clock=c, on_event=lambda *a, **k: events.append((a[1], k)))
        e.register(MeasurementSpec("temperature", "degC", max_age_s=1.0, flatline_window_s=1.0, **kw))
        e.register(MeasurementSpec("counter", "n", max_age_s=1.0))                    # flatline check is opt-in
        return c, e, events

    def feed(self, e, c, t, v, mtype="temperature", unit="degC"):
        c.t = t
        return e.ingest(Observation("s1", mtype, v, unit, t))

    def test_frozen_value_becomes_degraded_flatline_while_still_fresh(self):
        c, e, ev = self.mk()
        for i in range(30):
            self.feed(e, c, 100.0 + i * 0.1, 21.0)             # same value, arriving on time: looks perfectly fresh
        sv = e.get("s1", "temperature")
        self.assertEqual((sv.status, sv.reason), (Status.DEGRADED, "flatline"))
        self.assertIsNone(e.latest_valid("s1", "temperature"))
        self.assertEqual(e.health()["by_reason"], {"flatline": 1})
        changes = [k for t, k in ev if t == "status_change"]
        self.assertEqual((changes[-1]["new"], changes[-1]["reason"]), ("degraded", "flatline"))

    def test_not_flagged_before_window_or_min_samples(self):
        c, e, _ = self.mk()
        for i in range(8):
            self.feed(e, c, 100.0 + i * 0.1, 21.0)             # 0.7 s of identical values
        self.assertEqual(e.get("s1", "temperature").status, Status.VALID)
        c2, e2, _ = self.mk(flatline_min_samples=10)
        for i in range(6):
            self.feed(e2, c2, 100.0 + i * 0.3, 21.0)           # long enough, but only 6 samples
        self.assertEqual(e2.get("s1", "temperature").status, Status.VALID)

    def test_noisy_signal_and_recovery(self):
        c, e, _ = self.mk()
        for i in range(30):
            self.feed(e, c, 100.0 + i * 0.1, 21.0 + (i % 2) * 0.01)
        self.assertEqual(e.get("s1", "temperature").status, Status.VALID)
        for i in range(30):
            self.feed(e, c, 103.0 + i * 0.1, 22.0)
        self.assertEqual(e.get("s1", "temperature").reason, "flatline")
        self.feed(e, c, 106.1, 22.5)                            # the value moves again: recovered
        self.assertEqual(e.get("s1", "temperature").status, Status.VALID)

    def test_epsilon_tolerates_tiny_jitter(self):
        c, e, _ = self.mk(flatline_epsilon=0.05)
        for i in range(30):
            self.feed(e, c, 100.0 + i * 0.1, 21.0 + (i % 3) * 0.02)
        self.assertEqual(e.get("s1", "temperature").reason, "flatline")

    def test_disabled_by_default_and_stale_takes_precedence(self):
        c, e, _ = self.mk()
        for i in range(30):
            self.feed(e, c, 100.0 + i * 0.1, 5.0, "counter", "n")
        self.assertEqual(e.get("s1", "counter").status, Status.VALID)          # constant counters are legitimate
        for i in range(30):
            self.feed(e, c, 100.0 + i * 0.1, 21.0)
        c.t = 110.0
        self.assertEqual(e.get("s1", "temperature").reason, "stale")

    def test_vector_values_and_driver_quality_reason(self):
        c = Clock()
        e = StateEngine(clock=c)
        e.register(MeasurementSpec("pos", "m", max_age_s=1.0, dims=2, flatline_window_s=1.0))
        for i in range(20):
            c.t = 100.0 + i * 0.1
            e.ingest(Observation("g", "pos", (1.0, 2.0), "m", c.t))
        self.assertEqual(e.get("g", "pos").reason, "flatline")
        c.t = 102.0
        e.ingest(Observation("g", "pos", (1.0, 2.5), "m", c.t, quality="degraded"))
        self.assertEqual(e.get("g", "pos").reason, "driver_quality")
