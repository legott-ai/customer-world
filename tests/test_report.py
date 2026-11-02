"""Publication and report boundaries, independent of inference providers."""
import tempfile
import unittest
from pathlib import Path

from customer_world.report import aggregate_view, render_report, write_report


class ReportTests(unittest.TestCase):
    def test_participant_content_cannot_enter_export(self):
        report = render_report({"day": "2030-01-01", "prepared": 3,
                                "memory": "PRIVATE_SENTINEL", "sources": "PRIVATE_SOURCE"})
        self.assertNotIn("PRIVATE_SENTINEL", report)
        self.assertNotIn("PRIVATE_SOURCE", report)
        self.assertIn("aggregate counts only", report)

    def test_untrusted_date_is_escaped(self):
        self.assertIn("&lt;script&gt;", render_report({"day": "<script>"}))
        self.assertNotIn("<script>", render_report({"day": "<script>"}))

    def test_actual_core_intention_counts_survive_the_export_boundary(self):
        data = aggregate_view({"intent_counts": {"keep_incumbent": 2, "try_again": 1},
                               "actor_days": 4, "participant_private_data": "OMIT"})
        self.assertEqual(data["decisions"]["keep_incumbent"], 2)
        self.assertEqual(data["decisions"]["try_again"], 1)
        self.assertEqual(data["actor_days"], 4)
        self.assertNotIn("participant_private_data", data)

    def test_negative_or_boolean_counts_are_rejected(self):
        for value in (-1, True, "three"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                aggregate_view({"prepared": value})

    def test_report_does_not_replace_file_without_explicit_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.html"
            write_report({"prepared": 1}, output)
            original = output.read_bytes()
            with self.assertRaises(FileExistsError):
                write_report({"prepared": 2}, output)
            self.assertEqual(output.read_bytes(), original)
            write_report({"prepared": 2}, output, force=True)
            self.assertNotEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
