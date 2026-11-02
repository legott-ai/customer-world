"""Offline command workflows; no participant decisions or external inference."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from customer_world.cli import main


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.sources = self.base / "sources.jsonl"
        self.sources.write_text(json.dumps({
            "id": "fixture-one", "brief": "A fictional reader wants to save book notes.",
            "provenance": {"kind": "fictional", "reference": "Local test fixture"},
        }) + "\n", encoding="utf-8")

    def call(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def init(self):
        return self.call("init", "--project", str(self.project), "--sources", str(self.sources),
                         "--count", "1", "--tool", "product=http://127.0.0.1:3000")

    def test_prepare_status_verify_report_without_provider(self):
        code, output, error = self.init()
        self.assertEqual(code, 0, error)
        summary = json.loads(output)
        self.assertEqual(summary["prepared"], 1)
        self.assertEqual(summary["generated_profiles"], 0)
        code, output, error = self.call("status", "--project", str(self.project))
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["autonomous_decisions"], 0)
        code, output, error = self.call("verify", "--project", str(self.project))
        self.assertEqual(code, 0, error)
        report = self.base / "report.html"
        code, output, error = self.call("report", "--project", str(self.project), "--output", str(report))
        self.assertEqual(code, 0, error)
        self.assertIn("aggregate counts only", report.read_text())
        self.assertNotIn("fictional reader", report.read_text())

    def test_invalid_command_argv_never_reaches_subprocess(self):
        self.init()
        code, output, error = self.call("generate", "--project", str(self.project),
                                        "--provider", "command", "--command-json", '"not-an-array"')
        self.assertEqual(code, 1)
        self.assertIn("argv array", error)

    def test_explicit_model_required_before_provider_construction(self):
        self.init()
        code, output, error = self.call("generate", "--project", str(self.project), "--provider", "openai")
        self.assertEqual(code, 1)
        self.assertIn("--model is required", error)

    def test_prepare_rejects_requested_count_beyond_source_evidence(self):
        code, output, error = self.call("init", "--project", str(self.project), "--sources", str(self.sources), "--count", "100")
        self.assertEqual(code, 1)
        self.assertFalse((self.project / "config.json").exists())

    def test_inspection_rejects_pending_symlinks(self):
        self.init()
        pending = self.project / "broker" / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        external = self.base / "external.json"
        external.write_text('{"private": "EXTERNAL_SENTINEL"}', encoding="utf-8")
        (pending / "unexpected.json").symlink_to(external)
        code, output, error = self.call("pending", "--project", str(self.project))
        self.assertEqual(code, 1)
        self.assertNotIn("EXTERNAL_SENTINEL", output + error)


if __name__ == "__main__":
    unittest.main()
