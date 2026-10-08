import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from octopus import device_agent
from octopus.action_executor import _peer_progress
from octopus.reporting import append_task_result as append_task_result_log
from octopus.session_state import ExecutionLedger, RuntimeBudget, RuntimeSwitchState
from octopus import task_schema
from octopus.services.coverage_store import CoverageMetrics, CoverageStore
from octopus.services.memory_store import MemoryPool
from octopus.outcome_verifier import TaskResultVerifier


class UiGraphAndMemoryTests(unittest.TestCase):
    def test_state_normalization_removes_bounds_time_counts_and_random_ids(self):
        first = (
            "id:'Inbox 12 unread messages', clickable=true, bounds='[0,0][100,100]'\n"
            "id:'Updated 10:42 PM', clickable=false\n"
            "id:'550e8400-e29b-41d4-a716-446655440000', clickable=false\n"
            "id:'Compose', clickable=true\n"
        )
        second = (
            "id:'Inbox 93 unread messages', clickable=true, bounds='[9,9][200,200]'\n"
            "id:'Updated 11:07 PM', clickable=false\n"
            "id:'9f1c8400-a23b-42d4-b716-998877665544', clickable=false\n"
            "id:'Compose', clickable=true\n"
        )

        sig1 = CoverageStore.build_state_signature(1, "com.example", ".MainActivity", first)
        sig2 = CoverageStore.build_state_signature(1, "com.example", ".MainActivity", second)
        sig_other_device = CoverageStore.build_state_signature(2, "com.example", ".MainActivity", second)

        self.assertEqual(sig1, sig2)
        self.assertNotEqual(sig1, sig_other_device)

    def test_transition_attempt_and_frontier_are_persisted(self):
        store = CoverageStore()
        first_ui = (
            "id:'Open', clickable=true\n"
            "id:'Search', clickable=true, editable=true\n"
        )
        second_ui = "id:'Details', clickable=true\n"
        first_sig = CoverageStore.build_state_signature(1, "com.example", ".MainActivity", first_ui)
        second_sig = CoverageStore.build_state_signature(1, "com.example", ".DetailActivity", second_ui)

        store.record_observation(
            1, first_sig, first_ui, "", activity=".MainActivity", package_name="com.example"
        )
        store.record_observation(
            1,
            second_sig,
            second_ui,
            first_sig,
            activity=".DetailActivity",
            package_name="com.example",
            action_label="tap:open",
            action_outcome={"changed": True},
        )

        snapshot = store.snapshot()["exploration"]
        self.assertEqual(len(snapshot["ui_states"]), 2)
        self.assertEqual(len(snapshot["transitions"]), 1)
        self.assertEqual(snapshot["action_attempts"][first_sig]["tap:open"], 1)
        first_frontier = next(item for item in snapshot["frontier"] if item["signature"] == first_sig)
        self.assertNotIn("tap:open", first_frontier["untried_actions"])
        self.assertIn("tap:search", first_frontier["untried_actions"])
        self.assertEqual(snapshot["known_paths"][second_sig][0]["action"], "tap:open")

    def test_step_clear_preserves_exploration_and_task_memory(self):
        pool = MemoryPool()
        store = CoverageStore(pool.exploration_memory)
        ui = "id:'Settings', clickable=true\n"
        signature = CoverageStore.build_state_signature(1, "com.example", ".MainActivity", ui)
        pool.begin_task("task_1")
        pool.add_memory("0", "1", "[tap] [Settings]", "opened settings")
        store.record_observation(
            1, signature, ui, "", activity=".MainActivity", package_name="com.example"
        )
        pool.record_task_result({"task_id": "task_1", "result": "success"})

        pool.clear_step_memory()

        self.assertEqual(pool.get_memory_snapshot(), [])
        self.assertEqual(pool.get_step_memory()["records"], [])
        self.assertEqual(len(pool.get_exploration_snapshot()["ui_states"]), 1)
        self.assertEqual(pool.get_task_memory()[0]["result"], "success")

    def test_concurrent_memory_reads_and_writes_are_consistent(self):
        pool = MemoryPool()
        store = CoverageStore(pool.exploration_memory)
        ui = "id:'Open', clickable=true\n"
        signature = CoverageStore.build_state_signature(1, "com.example", ".MainActivity", ui)

        def writer(worker_id):
            for index in range(100):
                pool.add_memory("0", str(worker_id), f"tap-{index}", "ok")
                store.record_observation(
                    1, signature, ui, "", activity=".MainActivity", package_name="com.example"
                )
                pool.get_memory_snapshot()
                pool.get_exploration_snapshot()

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(pool.get_memory_snapshot()), 600)
        node = pool.get_exploration_snapshot()["ui_states"][signature]
        self.assertEqual(node["visit_count"], 600)

    def test_peer_event_lifecycle_is_atomic_and_single_use(self):
        pool = MemoryPool()
        event = pool.record_peer_event(
            "Device2", "notification", "conference invite", "org.example", ttl_seconds=1
        )

        claimed = pool.claim_peer_event(
            "task_1", device="Device2", expected="invite", package_name="org.example"
        )
        self.assertEqual(claimed["event_id"], event["event_id"])
        self.assertIsNone(pool.claim_peer_event("task_2", device="Device2", expected="invite"))
        self.assertEqual(pool.consume_peer_event(event["event_id"])["status"], "consumed")
        self.assertIsNone(pool.consume_peer_event(event["event_id"]))

        expired = pool.record_peer_event("Device2", "notification", "old", ttl_seconds=0.01)
        time.sleep(0.12)
        self.assertFalse(any(item["event_id"] == expired["event_id"] for item in pool.pending_peer_events()))

    def test_peer_transition_is_not_added_to_replayable_path(self):
        store = CoverageStore()
        first = CoverageStore.build_state_signature(1, "pkg", ".A", "id:'Send', clickable=true")
        second = CoverageStore.build_state_signature(1, "pkg", ".B", "id:'Done', clickable=false")
        store.record_observation(1, first, "id:'Send', clickable=true", "", activity=".A", package_name="pkg")
        store.exploration.record_state(second, 1, "pkg", ".B", [], [])
        store.exploration.record_transition(
            first, "peer_event:invite", second, 1, replayable=False
        )

        self.assertEqual(store.snapshot()["exploration"]["known_paths"][second], [])


class PlanningVerificationAndBudgetTests(unittest.TestCase):
    def test_operator_accepts_only_strict_action_lines(self):
        parse = device_agent.DeviceAgent._parse_line
        self.assertEqual(parse("[tap] [Settings]")["kind"], "tap")
        self.assertEqual(parse("[switch] [Device2] [invite sent]")["device"], 2)
        self.assertIsNone(parse("[tap] Settings"))
        self.assertIsNone(parse("switch to device 2: invite sent"))

    def test_operator_normalizes_model_tap_shorthand_before_grounding(self):
        parse_output = device_agent.DeviceAgent._parse_output
        action = parse_output("[tap] org.linphone:id/history")
        self.assertEqual(action["kind"], "tap")
        self.assertEqual(action["target"], "org.linphone:id/history")
        self.assertEqual(action["raw"], "[tap] [org.linphone:id/history]")

    def test_coverage_delta_calculation(self):
        before = {
            "__overall__": {
                "activity_cov": 0.25,
                "code_cov": 0.1,
                "num_activities": 1,
                "num_code_units": 2,
            }
        }
        after = {
            "__overall__": {
                "activity_cov": 0.75,
                "code_cov": 0.4,
                "num_activities": 3,
                "num_code_units": 7,
            }
        }

        delta = CoverageMetrics.delta(before, after)["__overall__"]

        self.assertAlmostEqual(delta["activity_cov"], 0.5)
        self.assertAlmostEqual(delta["code_cov"], 0.3)
        self.assertEqual(delta["num_activities"], 2)
        self.assertEqual(delta["num_code_units"], 5)

    def test_operator_end_request_is_not_success(self):
        task = {
            "task_id": "task_1",
            "participating_devices": ["Device1"],
            "verification_conditions": [{
                "device": "Device1", "type": "visible", "expected": "confirmation"
            }],
        }
        terminal = {
            "devices": {
                "Device1": {
                    "device": "Device1",
                    "ui_summary": "label:'Submit', clickable=true",
                }
            }
        }

        result = TaskResultVerifier().verify(
            task,
            terminal,
            evidence=[{"type": "operator_end_request", "device": "Device1"}],
            termination_reason="task_completed",
            operator_end_requested=True,
        )

        self.assertEqual(result["result"], "unknown")

    def test_multi_device_task_missing_device_state_cannot_succeed(self):
        task = {
            "task_id": "task_multi",
            "participating_devices": ["Device1", "Device2"],
            "verification_conditions": [{
                "device": "Device2", "type": "visible", "expected": "invite"
            }],
        }
        terminal = {
            "devices": {
                "Device1": {
                    "device": "Device1",
                    "ui_summary": "label:'Invite sent', clickable=false",
                }
            }
        }

        result = TaskResultVerifier().verify(
            task,
            [terminal, terminal],
            evidence=[{"type": "Tap", "device": "Device1", "changed": True}],
            termination_reason="task_completed",
            operator_end_requested=True,
        )

        self.assertNotEqual(result["result"], "success")
        self.assertEqual(result["failure_category"], "missing_device_evidence")

    def test_task_result_is_written_to_task_memory_and_jsonl(self):
        pool = MemoryPool()
        result_record = {
            "task_id": "task_7",
            "episode": 7,
            "task": "test",
            "reason": "missing_verification_conditions",
            "task_record": {"task_id": "task_7", "description": "test"},
            "result": "unknown",
            "coverage_delta": {"__overall__": {"activity_cov": 0.0}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "task_results.jsonl")
            append_task_result_log(
                path,
                result_record,
                memory_pool=pool,
            )
            with open(path, "r", encoding="utf-8") as handle:
                logged = json.loads(handle.readline())

        self.assertEqual(logged["result"], "unknown")
        self.assertEqual(pool.get_task_memory()[0]["task_id"], "task_7")

    def test_runtime_switches_use_registered_scheduler(self):
        pool = MemoryPool()
        pool.align_2(2, ["a", "b"], 1)
        ledger = ExecutionLedger()
        scheduler = RuntimeSwitchState(cooldown_seconds=0, ledger=ledger)
        pool.register_switch_scheduler(scheduler)

        switched = pool.set_current_device(
            2, reason="unit_test_handoff", source="operator", cooldown=False
        )

        self.assertTrue(switched)
        self.assertEqual(pool.get_current_device(), 2)
        switches = [
            item for item in pool.get_step_memory()["records"]
            if item["event"] == "device_switch"
        ]
        self.assertEqual(len(switches), 1)
        self.assertEqual(ledger.snapshot()["device_switch_count"], 1)
        executed, _ = scheduler.execute_if_current(pool, 1, lambda: "stale")
        self.assertFalse(executed)

    def test_action_and_token_budgets_report_clean_exhaustion(self):
        action_budget = RuntimeBudget(max_actions=2)
        action_budget.record_actions(1)
        self.assertEqual(action_budget.exhaustion_reason(), "")
        action_budget.record_actions(1)
        self.assertEqual(action_budget.exhaustion_reason(), "action_budget_exhausted")

        token_budget = RuntimeBudget(max_total_tokens=100)
        self.assertEqual(
            token_budget.exhaustion_reason({
                "llm_call_count": 1,
                "prompt_tokens": 80,
                "completion_tokens": 20,
                "total_tokens": 100,
            }),
            "token_budget_exhausted",
        )
        verified = TaskResultVerifier().verify(
            {
                "participating_devices": ["Device1"],
                "verification_conditions": [{
                    "device": "Device1", "type": "visible", "expected": "done"
                }],
            },
            {"devices": {"Device1": {"device": "Device1", "ui_summary": "done"}}},
            termination_reason="token_budget_exhausted",
        )
        self.assertEqual(verified["result"], "failure")
        self.assertEqual(verified["failure_category"], "budget")

    def test_execution_limits_are_task_global(self):
        ledger = ExecutionLedger()

        self.assertEqual(ledger.record_progress(False), 1)
        self.assertEqual(ledger.record_progress(False), 2)
        self.assertEqual(ledger.record_progress(False), 3)
        self.assertEqual(ledger.record_progress(True), 0)
        self.assertEqual(ledger.record_grounding(True), 1)
        self.assertEqual(ledger.record_grounding(True), 2)

    def test_selection_balances_relevance_and_screen_task_novelty(self):
        from octopus.experiment_config import MethodOptions

        contexts = [{"device_index": 1, "ui_summary": "Search Settings"}]
        candidates = [{"goal": goal, "first_device": "Device1"}
                      for goal in ("Settings", "Search", "Send photo")]
        ranked = task_schema.rank_task_candidates(candidates, contexts=contexts)
        self.assertEqual(ranked[0]["goal"], "Settings")
        history = [{"result": "failure", "task_record": ranked[0]}]
        ranked = task_schema.rank_task_candidates(candidates, history, contexts=contexts)
        self.assertEqual(ranked[0]["goal"], "Search")
        ranked = task_schema.rank_task_candidates(
            candidates, history, contexts=contexts, method=MethodOptions(diversity=False))
        self.assertEqual(ranked[0]["goal"], "Settings")
        ranked = task_schema.rank_task_candidates(
            candidates[::-1], contexts=contexts,
            method=MethodOptions(relevance=False, diversity=False))
        self.assertEqual(ranked[0]["goal"], "Send photo")

    def test_structured_state_verification_requires_two_snapshots(self):
        task = {
            "participating_devices": ["Device1"],
            "verification_conditions": [{
                "device": "Device1", "type": "visible", "expected": "Connected"
            }],
        }
        snapshot = {
            "devices": {"Device1": {"device": "Device1", "ui_summary": "Connected"}}
        }
        verifier = TaskResultVerifier()

        self.assertEqual(verifier.verify(task, snapshot)["result"], "unknown")
        self.assertEqual(verifier.verify(task, [snapshot, snapshot])["result"], "success")

    def test_structured_event_verification_uses_consumed_event(self):
        task = {
            "participating_devices": ["Device2"],
            "verification_conditions": [{
                "device": "Device2", "type": "peer_event", "expected": "invite"
            }],
        }
        result = TaskResultVerifier().verify(
            task,
            {"devices": {"Device2": {"device": "Device2"}}},
            evidence=[{
                "device": "Device2", "status": "consumed", "value": "conference invite"
            }],
        )
        self.assertEqual(result["result"], "success")

    def test_verification_refreshes_only_missing_peer_and_honors_ablation(self):
        task = {"first_device": "Device1", "verification_conditions": [
            {"device": "Device1", "type": "visible", "expected": "Sent"},
            {"device": "Device2", "type": "visible", "expected": "Received"},
        ]}
        initial = {"devices": {"Device1": {"ui_summary": "Sent"},
                               "Device2": {"ui_summary": "Waiting"}}}
        observe = Mock(return_value={"devices": {"Device2": {"ui_summary": "Received"}}})
        verifier = TaskResultVerifier()
        with patch("octopus.outcome_verifier.sleep"):
            result = verifier.verify(task, [initial, initial], observe=observe, max_rounds=2)
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["observation_rounds"], 2)
        self.assertEqual([call.args[0] for call in observe.call_args_list], ["Device2", "Device2"])
        observe.reset_mock()
        result = verifier.verify(task, [initial, initial], observe=observe, max_rounds=0)
        self.assertNotEqual(result["result"], "success")
        observe.assert_not_called()
        verifier.verify(task, [initial, initial], observe=observe, max_rounds=2, deadline=0)
        observe.assert_not_called()

    def test_empty_or_failed_observation_cannot_prove_absence(self):
        task = {"verification_conditions": [
            {"device": "Device1", "type": "visible", "expected": "Error", "operator": "absent"}
        ]}
        for state in ({}, {"context_collection_error": "offline"}):
            snapshot = {"devices": {"Device1": state}}
            self.assertEqual(TaskResultVerifier().verify(task, [snapshot, snapshot])["result"], "unknown")

    def test_effect_feedback_uses_observed_peer_and_can_be_disabled(self):
        from octopus.action_executor import execute_action_infos_scheduled
        from octopus.experiment_config import MethodOptions

        task = {"participating_devices": ["Device1", "Device2"]}
        condition = {"device": "Device2", "type": "visible", "expected": "Incoming", "operator": "contains"}
        base = {"had_critical_action": True, "changed": True, "after_state": {"all_comps": "Dialing"}}
        for enabled, expected_changed in ((True, False), (False, True)):
            pool = MemoryPool(MethodOptions(effect_feedback=enabled))
            pool.begin_task("call", task)
            with patch("octopus.action_executor.execute_action_infos", return_value=dict(base)), patch(
                "octopus.action_executor._capture_peer_states", return_value={"Device2": {"all_comps": "Idle"}}
            ):
                result = execute_action_infos_scheduled(
                    pool, None, None, [{"expected_effect": condition}], 1)
            self.assertEqual(result["changed"], expected_changed)
            if enabled:
                self.assertEqual(result["effect_status"], "unresolved")
                self.assertEqual(pool.get_step_memory()["records"][-1]["condition"], condition)

    def test_operator_effect_json_and_ambiguous_grounding(self):
        operator = device_agent.DeviceAgent
        action = operator._parse_output(json.dumps({
            "objective": "Call B", "action": "[tap] [Call]",
            "expected_effect": {"device": "Device2", "type": "visible", "expected": "Incoming"},
        }))
        self.assertEqual(action["expected_effect"]["device"], "Device2")
        self.assertEqual(action["kind"], "tap")
        components = [{"@text": "Call", "@clickable": "true", "@resource-id": name}
                      for name in ("call1", "call2")]
        self.assertIsNone(operator._find_target(components, "Call"))
        self.assertEqual(operator._find_target(components, "call2")["@resource-id"], "call2")

    def test_candidates_stay_with_source_screen_and_generation_is_one_call(self):
        from octopus.task_planner import select_generated_task

        def task(goal):
            return {"goal": goal, "participating_devices": ["Device1"], "first_device": "Device1",
                    "device_goals": [{"device": "Device1", "goal": goal}],
                    "grounding_information": [{"device": "Device1", "control": goal}],
                    "verification_conditions": [{"device": "Device1", "type": "visible", "expected": goal}]}

        pool = MemoryPool()
        home = [{"device_index": 1, "device_id": "phone", "role": "user", "package": "pkg",
                 "activity": "Home", "ui_summary": "Settings Search"}]
        other = [{**home[0], "activity": "Settings", "ui_summary": "Account"}]
        with patch("octopus.task_planner.model_client.GeneralGPT") as model:
            model.return_value.ask_gpt_message.side_effect = [
                {"content": json.dumps({"tasks": [task("Settings"), task("Search")]})},
                {"content": json.dumps({"tasks": [task("Account")]})},
            ]
            first, _ = select_generated_task(["phone"], home, "", pool, 1)
            second, _ = select_generated_task(["phone"], other, "", pool, 2)
            third, _ = select_generated_task(["phone"], home, "", pool, 3)
        self.assertEqual([first["goal"], second["goal"], third["goal"]], ["Settings", "Account", "Search"])
        self.assertEqual(model.return_value.ask_gpt_message.call_count, 2)

    def test_task_relevant_peer_change_counts_as_progress(self):
        task = {
            "participating_devices": ["Device1", "Device2"],
            "verification_conditions": [{
                "device": "Device2", "type": "visible", "expected": "Invite received"
            }],
        }
        before = {
            "Device2": {
                "signature": "before", "state_ui_summary": "Inbox", "all_comps": ""
            }
        }
        after = {
            "Device2": {
                "signature": "after",
                "state_ui_summary": "Invite received",
                "all_comps": "",
            }
        }

        progress = _peer_progress(task, before, after)

        self.assertEqual(progress[0]["device"], "Device2")

    def test_peer_event_matching_local_goal_counts_as_progress(self):
        task = {
            "participating_devices": ["Device1", "Device2"],
            "device_goals": [{
                "device": "Device2", "goal": "Open the conference invite notification."
            }],
            "verification_conditions": [],
        }
        progress = _peer_progress(task, {}, {}, [{
            "event_id": "event-1",
            "device": "Device2",
            "status": "pending",
            "value": "conference invite received",
        }])

        self.assertTrue(progress[0]["local_goal_matched"])


if __name__ == "__main__":
    unittest.main()
