import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from firebot_infra import __version__
from firebot_infra.akshara import Recorder
from firebot_infra.ananta import Connector, JsonlFileSink, Spool
from firebot_infra.cli import main
from firebot_infra.framing import encode_frame
from firebot_infra.tarang import Capture, CaptureMeta, flip_byte, save_capture


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def make_run(self, name="r1", version="v1", n=30, warn_every=0):
        conn = Connector(Spool(self.dir / f"sp-{name}"), JsonlFileSink(self.dir / "events.jsonl"), device_id="d", salt="s")
        with Recorder(self.dir / "runs", name, software_version=version, tee=conn.log) as rec:
            for i in range(n):
                rec.emit("nav", "step", "warning" if warn_every and i % warn_every == 0 else "info", i=i, duration_ms=5.0)
        conn.flush()
        return self.dir / "runs" / name

    def test_selftest_passes(self):
        code, out, _ = run("selftest")
        self.assertEqual(code, 0, out)
        self.assertIn("ALL CHECKS PASSED", out)
        self.assertEqual(sum(1 for l in out.splitlines() if l.startswith(("  PASS", "  SKIP"))), 15)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()) as out:
            main(["--version"])
        self.assertEqual(e.exception.code, 0)
        self.assertIn(__version__, out.getvalue())

    def test_run_info_and_diff(self):
        a, b = self.make_run("a"), self.make_run("b")
        code, out, _ = run("run-info", str(a))
        self.assertEqual(code, 0)
        self.assertIn("nav", out)
        code, out, _ = run("diff-runs", str(a), str(a))
        self.assertEqual((code, out.strip()), (0, "identical"))

    def test_decode_good_and_damaged_captures(self):
        cap = Capture.from_bytes(encode_frame(b"one") + encode_frame(b"two"), CaptureMeta())
        p = self.dir / "c.jsonl"
        save_capture(p, cap)
        code, out, _ = run("decode", str(p))
        self.assertEqual(code, 0)
        self.assertIn("2 frames", out)
        save_capture(p, flip_byte(cap, 5))
        code, out, _ = run("decode", str(p))
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM", out)

    def test_report_and_html(self):
        self.make_run("a", n=40, warn_every=4)
        html_path = self.dir / "r.html"
        code, out, _ = run("report", str(self.dir / "events.jsonl"), "--html", str(html_path))
        self.assertEqual(code, 0)
        self.assertIn("Overview", out)
        self.assertIn("UTC", out)
        self.assertIn("<table>", html_path.read_text())

    def test_compare_versions_exit_code_signals_regression(self):
        self.make_run("a", "v1", n=100, warn_every=0)
        self.make_run("b", "v2", n=100, warn_every=2)
        code, out, _ = run("compare-versions", str(self.dir / "events.jsonl"), "v1", "v2")
        self.assertEqual(code, 1)
        self.assertIn("REGRESSION", out)
        code, _, _ = run("compare-versions", str(self.dir / "events.jsonl"), "v1", "v1")
        self.assertEqual(code, 0)

    def test_bench_quick_and_schema(self):
        out_json = self.dir / "b.json"
        code, out, _ = run("bench", "--quick", "--only", "framing", "drishti", "--json", str(out_json))
        self.assertEqual(code, 0)
        self.assertIn("DRISHTI", out)
        data = json.loads(out_json.read_text())
        self.assertTrue(all(r["value"] > 0 for r in data["results"]))
        code, out, _ = run("schema")
        self.assertIn("CREATE TABLE IF NOT EXISTS firebot_events", out)

    def test_friendly_errors_not_tracebacks(self):
        code, out, err = run("run-info", str(self.dir / "does-not-exist"))
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        code, _, err = run("report", str(self.dir / "missing.jsonl"))
        self.assertEqual(code, 2)
        (self.dir / "empty").mkdir()
        code, _, err = run("run-info", str(self.dir / "empty"))          # a real folder, but not a recording
        self.assertEqual(code, 2)
        self.assertIn("does not look like a recorded run", err)
        code, _, err = run("diff-runs", str(self.dir / "nope"), str(self.dir / "nope"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
