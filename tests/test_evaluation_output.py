import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from octopus import session
from octopus import reporting
from octopus.services import trace_writer


class EvaluationOutputTests(unittest.TestCase):
    def test_task_deduplication_keeps_retries_out_of_task_count(self):
        records = [
            {"fingerprint": "open profile", "result": "failure", "participant_count": 1},
            {"fingerprint": "profile open", "result": "success", "participant_count": 1},
            {"fingerprint": "send message", "result": "unknown", "participant_count": 2},
        ]
        summary = reporting.summarize_tasks(records)
        self.assertEqual(summary["attempt_count"], 3)
        self.assertEqual(summary["task_count"], 2)
        self.assertEqual(summary["task_success_rate"], 0.5)
        self.assertEqual(summary["attempt_success_rate"], 1 / 3)
        self.assertEqual(summary["multi_device_attempts"], 1)
        self.assertIsNone(reporting.summarize_tasks([])["task_success_rate"])

    def run_campaign(self, directory, detailed=False, interrupt=False):
        paths = {"run_dir": directory, **{key: os.path.join(directory, filename) for key, filename in (
            ("info_path", "info.jsonl"), ("task_result_log_path", "task_results.jsonl"),
            ("summary_path", "summary.json"), ("metrics_path", "metrics.csv"),
            ("exploration_memory_path", "exploration_memory.json"))}}
        task = {"task_id": "task_1", "goal": "Open Settings", "description": "Open Settings",
                "first_device": "Device1", "participating_devices": ["Device1"],
                "device_goals": [{"device": "Device1", "goal": "Open Settings"}],
                "verification_conditions": [{"device": "Device1", "type": "visible", "expected": "Settings"}]}
        device = Mock()
        device.dump_hierarchy.return_value = '<hierarchy rotation="0"><node text="Settings" resource-id="app:id/settings" clickable="true" package="app" bounds="[0,0][50,50]" /></hierarchy>'
        device.app_current.return_value = {"activity": "app.Settings", "package": "app"}
        agent = Mock()
        agent.task_execution.return_value = SimpleNamespace(response={"status": 1})
        if interrupt:
            agent.task_execution.side_effect = KeyboardInterrupt

        def baseline(store, *args):
            store.set_known_activities(["app.Main", "app.Settings"])
            return {"source": "test manifest", "known_activities": 2}

        previous_log = trace_writer.get_log_path()
        self.addCleanup(trace_writer.set_log_path, previous_log)
        output = io.StringIO()
        with ExitStack() as stack:
            for target, kwargs in (
                ("build_run_paths", {"return_value": paths}),
                ("_install_apk_on_devices", {}), ("_launch_app_on_devices", {}),
                ("_register_static_activity_baseline", {"side_effect": baseline}),
                ("u2.connect", {"return_value": device}),
                ("android_device.AndroidController", {}),
                ("device_agent.DeviceAgent", {"return_value": agent}),
                ("select_generated_task", {"return_value": (task, "")}),
                ("sleep", {}),
            ):
                stack.enter_context(patch(f"octopus.session.{target}", **kwargs))
            stack.enter_context(patch.dict(os.environ))
            stack.enter_context(redirect_stdout(output))
            session.run(["--dip", "phone", "--task", "Open Settings", "--app-package", "app",
                          *(["--detailed-log"] if detailed else [])])
        with open(paths["summary_path"], encoding="utf-8") as handle:
            return json.load(handle), output.getvalue()

    def test_campaign_output_is_compact_by_default_and_details_are_opt_in(self):
        with tempfile.TemporaryDirectory() as root:
            for detailed in (False, True):
                directory = os.path.join(root, str(detailed))
                summary, output = self.run_campaign(directory, detailed)
                names = set(os.listdir(directory))
                expected = {"summary.json", "metrics.csv", "task_results.jsonl", "coverage_timeline.csv"}
                if detailed:
                    expected |= {"info.jsonl", "exploration_memory.json"}
                self.assertEqual(names, expected)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["metrics"]["task_count"], 1)
                self.assertEqual(summary["metrics"]["task_success_rate"], 1)
                self.assertIsNone(summary["metrics"]["class_cov"])
                self.assertNotIn("code_cov", summary["metrics"])
                self.assertIn("Method N/A | Class N/A", output)
                self.assertIn("[Task 1] SUCCESS", output)
                with open(os.path.join(directory, "metrics.csv"), encoding="utf-8-sig") as handle:
                    row = next(csv.DictReader(handle))
                self.assertEqual(row["activity_cov"], "0.5")
                self.assertEqual(row["class_cov"], "")
                self.assertEqual(row["device_count"], "1")
                self.assertEqual(row["run_seconds"], "3600")

    def test_interrupt_still_saves_summary_and_marks_incomplete_run(self):
        with tempfile.TemporaryDirectory() as directory:
            summary, output = self.run_campaign(directory, interrupt=True)
            self.assertEqual(summary["status"], "interrupted")
            self.assertIn("Run interrupted", output)


if __name__ == "__main__":
    unittest.main()
