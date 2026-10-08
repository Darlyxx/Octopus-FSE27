import copy
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from octopus import reporting
from octopus.services import trace_writer
from octopus.services.android_device import ActionType
from octopus.services.coverage_store import CoverageMetrics, CoverageStore
from octopus.services.memory_store import MemoryPool
from octopus.session_state import ExecutionLedger


class ResultLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "task_results.jsonl")
        self.info_path = os.path.join(self.temp.name, "info.jsonl")
        previous_path = trace_writer.get_log_path()
        self.addCleanup(trace_writer.set_log_path, previous_path)
        trace_writer.set_log_path("")

    def build_result(self, finish_reason="task_completed", instrumented=True):
        store = CoverageStore()
        store.set_known_activities(["app.Home", "app.Settings"])
        if instrumented:
            store.set_known_code_units(["app.Home.open", "app.Settings.open"])
        store.record_activity(1, "app.Home")
        contexts = [{"device_index": 1, "activity": "app.Settings", "ui_summary": "Settings"}]
        task = {
            "task_id": "task_1", "description": "Open settings",
            "verification_conditions": [
                {"device": "Device1", "type": "visible", "expected": "Settings"}
            ],
        }
        ledger = ExecutionLedger()
        ledger.begin("task_1", {}, CoverageMetrics().compute(store), {})
        ledger.record_actions([
            {"type": "Nop", "device": "Device1"},
            {"type": "Tap", "device": "Device1", "target_label": "Settings",
             "bounds": [0, 0, 100, 100], "resource_id": "app:id/settings"},
            {"type": "Input", "device": "Device1", "target_label": "Search",
             "text": "debug input", "input_meta": {"fallback_used": True}},
            {"type": "Back", "device": "Device1"},
        ])
        ledger.record_switch(1, 2, "peer event", "logcat")
        ledger.mark_operator_end(1)
        store.record_activity(1, "app.Settings")
        store.record_activity(2, "app.Settings")  # Overall gain must not double count.
        if instrumented:
            store.record_code_unit(1, "app.Settings.open")
            store.record_code_unit(2, "app.Settings.open")
        with patch("octopus.reporting.time", return_value=ledger.started_at + 12.5):
            return reporting.build_task_result(
                1, task["description"], task, finish_reason, [contexts, contexts],
                1, CoverageMetrics().compute(store), ledger,
            )

    def append(self, record, **kwargs):
        return reporting.append_task_result(
            self.path, record, **kwargs,
        )

    def test_default_result_keeps_actions_duration_and_overall_gains(self):
        summary = self.append(self.build_result())
        self.assertEqual(summary["result"], "success")
        self.assertEqual(summary["duration_seconds"], 12.5)
        self.assertEqual(summary["action_count"], 3)
        self.assertEqual(summary["actions"], [
            {"type": "Tap", "device": "Device1", "target": "Settings"},
            {"type": "Input", "device": "Device1", "target": "Search"},
            {"type": "Back", "device": "Device1"},
        ])
        self.assertEqual(summary["coverage"], {
            "activity": {"before": 0.5, "after": 1.0, "gain": 0.5, "new_activities": 1},
            "method": {"before": 0.0, "after": 0.5, "gain": 0.5, "new_methods": 1},
            "class": None,
        })
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.loads(handle.read()), summary)
        serialized = json.dumps(summary)
        for detail in ("app.Settings", "resource_id", "bounds", "input_meta", "debug input",
                       "task_record", '"verification"', "evidence", "total_tasks"):
            self.assertNotIn(detail, serialized)
        self.assertFalse(os.path.exists(self.info_path))

    def test_detailed_mode_preserves_evidence_and_main_schema(self):
        record = self.build_result()
        original = copy.deepcopy(record)
        pool = MemoryPool()
        plain = self.append(record)
        detailed = self.append(record, detailed_info=True, memory_pool=pool)
        plain.pop("ts")
        detailed.pop("ts")
        self.assertEqual(plain, detailed)
        self.assertEqual(record, original)
        stored = pool.get_task_memory()[0]
        stored.pop("recorded_at")
        self.assertEqual(stored, original)
        with open(self.info_path, encoding="utf-8") as handle:
            info = json.loads(handle.read())
        self.assertEqual(info["event"], "task_info")
        self.assertEqual(info["payload"]["task_id"], plain["task_id"])
        self.assertEqual(info["payload"]["episode"], plain["episode"])
        self.assertEqual(info["payload"]["result_record"], original)
        self.assertEqual(len(info["payload"]["result_record"]["terminal_state_snapshots"]), 2)

    def test_unavailable_code_coverage_is_null(self):
        summary = self.append(self.build_result(instrumented=False))
        self.assertIsNone(summary["coverage"]["method"])
        self.assertEqual(summary["coverage"]["activity"]["new_activities"], 1)

    def test_failure_and_unknown_remain_distinct(self):
        summary = self.append(self.build_result(finish_reason="task_timeout_failure"))
        self.assertEqual(summary["result"], "failure")
        self.assertEqual(summary["reason"], "task_timeout_failure")
        record = self.build_result()
        record.update(result="unknown", reason="missing_verification_conditions")
        summary = self.append(record)
        self.assertEqual(summary["result"], "unknown")
        self.assertEqual(summary["reason"], "missing_verification_conditions")

    def test_run_configuration_disables_previous_detailed_logging(self):
        paths = {"run_dir": self.temp.name, "info_path": self.info_path}
        with patch.dict(os.environ):
            reporting.init_run_log_file(paths, detailed_info=True)
            trace_writer.log_event("trace", xml="<node/>" * 1000)
            with open(self.info_path, encoding="utf-8") as handle:
                info = [json.loads(line) for line in handle]
            self.assertEqual(info[-1]["payload"]["xml"], "<node/>" * 1000)
            other_path = os.path.join(self.temp.name, "disabled", "info.jsonl")
            reporting.init_run_log_file({"run_dir": self.temp.name, "info_path": other_path})
            trace_writer.log_event("ignored", xml="debug")
            self.assertFalse(os.path.exists(other_path))
            with open(self.info_path, encoding="utf-8") as handle:
                self.assertEqual(len(handle.readlines()), 2)

    def test_parallel_diagnostic_writes_are_complete_json_lines(self):
        trace_writer.set_log_path(self.info_path)
        def write(index):
            trace_writer.log_event("action", index=index, action=ActionType.CLICK,
                                 units={"app.one", "app.two"}, text="设置" * 2000)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(write, range(40)))
        with open(self.info_path, encoding="utf-8") as handle:
            rows = [json.loads(line)["payload"] for line in handle]
        self.assertEqual({row["index"] for row in rows}, set(range(40)))
        for row in rows:
            self.assertEqual(row["action"], "CLICK")
            self.assertEqual(row["units"], ["app.one", "app.two"])
            self.assertEqual(row["text"], "设置" * 2000)

    def test_task_scoped_events_and_ablation_settings_are_preserved(self):
        from octopus.experiment_config import MethodOptions

        pool = MemoryPool(MethodOptions(effect_feedback=False, verification_rounds=0))
        old = pool.record_peer_event("Device2", "notification", "invite")
        pool.claim_peer_event("previous", device="Device2")
        pool.consume_peer_event(old["event_id"])
        ledger = ExecutionLedger()
        ledger.begin("current", {}, {}, {})
        task = {"task_id": "current", "verification_conditions": [
            {"device": "Device2", "type": "peer_event", "expected": "invite"}
        ]}
        pool.begin_task("current", task)

        def build():
            return reporting.build_task_result(1, "invite", task, "task_completed", [],
                                             1, {}, ledger, memory_pool=pool)

        self.assertEqual(build()["result"], "unknown")
        pool.record_peer_event("Device2", "notification", "invite")
        record = build()
        self.assertEqual(record["result"], "success")
        self.assertEqual(len(record["evidence"]), 1)
        logged = self.append(record)
        self.assertFalse(logged["method"]["effect_feedback"])
        self.assertEqual(logged["method"]["verification_rounds"], 0)


if __name__ == "__main__":
    unittest.main()
