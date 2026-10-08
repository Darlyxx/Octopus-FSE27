import os
import sys
import json
import tempfile
import unittest
import csv


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from octopus.services.coverage_store import CoverageMetrics, CoverageStore
from octopus.services.coverage_timeline import ActivityCoverageTimeline
from octopus.services.instrumentation import AndroidLogCoverageParser
from octopus.services.memory_store import MemoryPool
from octopus import task_schema
from octopus.app_lifecycle import _extract_package_activities, _extract_apk_manifest_activities
from octopus.services import android_device
from octopus.ui_context import get_task_ui_summary
from octopus.reporting import append_task_result as append_task_result_log
from octopus.task_planner import _build_planner_prompt


class ActivityCoverageTests(unittest.TestCase):
    def test_activity_coverage_only(self):
        store = CoverageStore()
        store.set_known_activities(["com.example.MainActivity", "com.example.SettingsActivity"])
        store.set_known_code_units(["activity:com.example.MainActivity", "service:com.example.SyncService"])
        store.record_code_unit(1, "activity:com.example.MainActivity")
        store.record_observation(
            1,
            "ignored-signature",
            "",
            "",
            activity=".MainActivity",
            package_name="com.example",
        )

        result = CoverageMetrics().compute(store)

        self.assertEqual(result["__overall__"]["num_activities"], 1)
        self.assertEqual(result["__overall__"]["known_activities"], 2)
        self.assertEqual(result["__overall__"]["activity_cov"], 0.5)
        self.assertEqual(result["__overall__"]["num_code_units"], 1)
        self.assertEqual(result["__overall__"]["known_code_units"], 2)
        self.assertEqual(result["__overall__"]["code_cov"], 0.5)
        self.assertTrue(result["__overall__"]["code_coverage_denominator_known"])

    def test_observation_does_not_create_code_units(self):
        store = CoverageStore()
        store.record_observation(
            1,
            "signature",
            "id:'Open settings', clickable=true\nid:'search_box', clickable=true\n",
            "",
            activity=".MainActivity",
            package_name="com.example",
        )

        result = CoverageMetrics().compute(store)

        self.assertEqual(result["__overall__"]["num_activities"], 1)
        self.assertEqual(result["__overall__"]["num_code_units"], 0)
        self.assertEqual(result["__overall__"]["code_cov"], 0.0)
        self.assertFalse(result["__overall__"]["code_coverage_denominator_known"])

    def test_static_activity_extraction(self):
        dumpsys = """
        Activity Resolver Table:
          com.example/.MainActivity
          com.example/com.example.SettingsActivity
          service com.example/.SyncService
        """

        activities = _extract_package_activities("com.example", dumpsys)

        self.assertIn("com.example.MainActivity", activities)
        self.assertIn("com.example.SettingsActivity", activities)
        self.assertNotIn("com.example.SyncService", activities)

    def test_androidlog_parser_extracts_units(self):
        parser = AndroidLogCoverageParser()

        self.assertEqual(
            parser.parse_line(
                "06-26 13:19:06.688 15310 15310 D MADROID_COVERAGE: "
                "METHOD=<com.example.MainActivity: void onCreate(android.os.Bundle)>"
            ),
            "<com.example.MainActivity: void onCreate(android.os.Bundle)>",
        )
        self.assertEqual(
            parser.parse_line("D/OtherTag(123): METHOD=<com.example.MainActivity: void onResume()>"),
            "",
        )

    def test_task_history_survives_action_memory_reset(self):
        pool = MemoryPool()
        pool.add_memory("0", "1", "Tap", "old action")
        pool.record_task_history({
            "description": "Open a visible settings entry on Device1.",
            "is_multi_device": False,
        }, episode=1)
        pool.clear_step_memory()

        self.assertEqual(pool.get_memory_snapshot(), [])
        self.assertEqual(pool.get_task_history()[0]["episode"], 1)


    def test_task_ui_summary_keeps_resource_id_out_of_label(self):
        xml = """
        <hierarchy>
          <node package="org.example" class="android.widget.FrameLayout" clickable="true"
                enabled="true" resource-id="org.example:id/tab_profile_ava"
                text="" content-desc="">
            <node package="org.example" class="android.widget.TextView" clickable="false"
                  enabled="true" resource-id="" text="My profile" content-desc="" />
            <node package="org.example" class="android.view.View" clickable="false"
                  enabled="true" resource-id="" text="" content-desc="Profile" />
          </node>
        </hierarchy>
        """

        summary = get_task_ui_summary(xml)

        self.assertIn("label:'My profile'", summary)
        self.assertIn("content_desc:'Profile'", summary)
        self.assertIn("resource_id:'tab_profile_ava'", summary)
        self.assertNotIn("My profile Profile tab_profile_ava", summary)

    def test_apk_manifest_activity_extraction_from_badging_text(self):
        output = """
        package: name='com.example' versionCode='1'
        launchable-activity: name='com.example.MainActivity'
        activity: name='com.example.SettingsActivity'
        activity-alias: name='.AliasActivity'
        """
        original_execute = android_device.execute_adb_args
        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as handle:
            apk_path = handle.name
        try:
            android_device.execute_adb_args = lambda args: {
                "returncode": 0,
                "stdout": output,
                "stderr": "",
            }
            activities, source = _extract_apk_manifest_activities(apk_path, "com.example")
        finally:
            android_device.execute_adb_args = original_execute
            os.remove(apk_path)

        self.assertEqual(source, "aapt_badging")
        self.assertEqual(
            activities,
            {
                "com.example.MainActivity",
                "com.example.SettingsActivity",
                "com.example.AliasActivity",
            },
        )

    def test_coverage_timeline_is_small_and_written_incrementally(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CoverageStore()
            store.set_known_activities(["app.Main"])
            store.set_known_code_units(["<app.Main: void open()>", "<app.Other: void close()>"])
            store.record_activity(1, "app.Main")
            store.record_code_unit(1, "<app.Main: void open()>")
            timeline = ActivityCoverageTimeline(directory)
            coverage = CoverageMetrics().compute(store)
            self.assertTrue(timeline.sample(coverage, 0, "initial"))
            self.assertFalse(timeline.sample(coverage, 10))
            self.assertTrue(timeline.sample(coverage, 60))
            with open(timeline.path, encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 4)  # Overall and one device, twice.
            self.assertEqual(rows[0]["class_cov"], "0.5")
            self.assertEqual(rows[0]["covered_methods"], "1")
            self.assertEqual(os.listdir(directory), ["coverage_timeline.csv"])
            self.assertNotIn("app.Main", str(rows))

    def test_class_and_method_coverage_union_devices_and_exclude_unknown_targets(self):
        store = CoverageStore()
        methods = ["<app.A: void first()>", "<app.A: void second()>", "<app.B: void first()>"]
        store.set_known_code_units(methods)
        store.set_known_activities(["app.Main", "app.Other"])
        store.record_code_units(1, [methods[0], "<external.C: void other()>"])
        store.record_code_units(2, [methods[0], methods[1]])
        store.record_activity(1, "app.Main")
        store.record_activity(2, "external.Screen")
        result = CoverageMetrics().compute(store)["__overall__"]
        self.assertAlmostEqual(result["method_cov"], 2 / 3)
        self.assertEqual(result["class_cov"], 0.5)
        self.assertEqual(result["activity_cov"], 0.5)
        self.assertEqual(result["out_of_scope_activities"], 1)
        self.assertEqual(result["out_of_scope_methods"], 1)

    def test_unknown_coverage_denominators_are_not_measured_zero(self):
        store = CoverageStore()
        store.record_activity(1, "app.Main")
        result = CoverageMetrics().compute(store)["__overall__"]
        for name in ("activity_cov", "method_cov", "class_cov"):
            self.assertIsNone(result[name])

    def test_task_result_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "task_results_test.jsonl")
            append_task_result_log(
                path,
                {
                    "task_id": "task_2",
                    "episode": 2,
                    "task": "Device1 opens settings.",
                    "reason": "repeated_action_failure",
                    "result": "failure",
                    "task_record": {"description": "Device1 opens settings.", "is_multi_device": False},
                },
            )

            with open(path, encoding="utf-8") as handle:
                record = json.loads(handle.readline())

            self.assertEqual(record["episode"], 2)
            self.assertEqual(record["task"], "Device1 opens settings.")
            self.assertEqual(record["result"], "failure")
            self.assertEqual(record["reason"], "repeated_action_failure")
            self.assertIsNone(record["duration_seconds"])
            self.assertNotIn("task_record", record)
            self.assertNotIn("total_tasks", record)
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "info.jsonl")))




class TaskUtilityTests(unittest.TestCase):
    def setUp(self):
        self.devices = ["emulator-5554", "emulator-5556"]

    @staticmethod
    def task(participants=None):
        participants = participants or ["Device1"]
        return {
            "task_id": "task_1",
            "goal": "Verify the selected app behavior.",
            "participating_devices": participants,
            "first_device": participants[0],
            "device_goals": [
                {"device": device, "goal": f"Complete the local objective on {device}."}
                for device in participants
            ],
            "grounding_information": [
                {"device": participants[0], "control": "Settings"}
            ],
            "verification_conditions": [
                {"device": participants[-1], "type": "visible", "expected": "Done"}
            ],
        }

    def test_participants_determine_multi_device_task(self):
        single = task_schema.normalize_task(self.task(), self.devices)
        multi = task_schema.normalize_task(
            self.task(["Device1", "Device2"]), self.devices
        )

        self.assertFalse(single["is_multi_device"])
        self.assertTrue(multi["is_multi_device"])

    def test_preplanned_actions_are_rejected(self):
        task = self.task()
        task["actions"] = [{"type": "Tap", "device": "Device1"}]

        validation = task_schema.validate_task(task, self.devices)

        self.assertFalse(validation["valid"])
        self.assertIn("preplanned_actions_not_allowed", validation["errors"])

    def test_missing_structured_fields_are_rejected(self):
        task = self.task()
        task["device_goals"] = []
        task["verification_conditions"] = []

        validation = task_schema.validate_task(task, self.devices)

        self.assertIn("missing_device_goal", validation["errors"])
        self.assertIn("invalid_verification_conditions", validation["errors"])

    def test_generated_batch_contains_single_and_multi(self):
        raw = json.dumps({"tasks": [self.task(), self.task(["Device1", "Device2"])]})
        tasks = [
            task_schema.validate_task(task, self.devices)["task"]
            for task in task_schema.parse_generated_tasks(raw, self.devices)
            if task_schema.validate_task(task, self.devices)["valid"]
        ]

        self.assertEqual([task["is_multi_device"] for task in tasks], [False, True])

    def test_planner_prompt_describes_structured_action_free_tasks(self):
        _, prompt = _build_planner_prompt(self.devices, "")

        self.assertIn("A Task is the final business testing objective", prompt)
        self.assertIn("verification_conditions", prompt)
        self.assertIn("Never output an action sequence", prompt)


if __name__ == "__main__":
    unittest.main()
