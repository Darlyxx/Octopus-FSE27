"""Orchestrate one octopus testing campaign."""

import argparse
import threading
from time import sleep, time
import json
import re
import os
from datetime import datetime
from dataclasses import asdict

import uiautomator2 as u2

from octopus.task_planner import select_generated_task
from octopus.experiment_config import MethodOptions
from octopus.reporting import (
    append_task_result,
    begin_episode,
    build_run_paths,
    build_task_result,
    write_run_summary,
    print_task_result,
    print_run_summary,
    init_run_log_file,
)
from octopus.action_executor import (
    _record_coverage_state,
    execute_action_infos_scheduled,
    record_context_coverage,
    record_execution_feedback,
    sample_activity_coverage_if_due,
)
from octopus.app_lifecycle import (
    _infer_app_package_from_contexts,
    _install_apk_on_devices,
    _launch_app_on_devices,
    _register_static_activity_baseline,
    _resolve_app_package,
)
from octopus.session_state import ExecutionLedger, RuntimeBudget, RuntimeSwitchState
from octopus.event_bridge import (
    DeviceEventBus,
    _env_enabled,
    start_accessibility_event_helper,
    start_accessibility_http_router,
    start_androidlog_coverage_listener,
    start_logcat_event_listener,
    wait_for_task_peer_event,
)
from octopus.ui_context import (
    collect_device_contexts,
    get_all_comps,
    get_task_ui_summary,
    state_signature,
)
from octopus.services.android_device import ActionType
from octopus.services import memory_store
from octopus.services.coverage_timeline import ActivityCoverageTimeline
from octopus.services.instrumentation import (
    AndroidLogCoverageParser,
    extract_androidlog_instrumented_units,
)
from octopus.services.coverage_store import CoverageMetrics, CoverageStore
from octopus.services import android_device
from octopus.services import model_client
from octopus.services import trace_writer
from octopus import task_dispatcher
from octopus import device_agent

task_done = False
episode_done = False
episode_done_reason = ""


AUTO_TASK_IDLE_RETRY_SECONDS = 5
AUTO_TASK_IDLE_CONSOLE_INTERVAL_SECONDS = 60

def device_parallel_worker_loop(device_index: int, devices: dict, pool: memory_store.MemoryPool,
                                stop_event: threading.Event, event_bus: DeviceEventBus = None):
    global task_done, episode_done, episode_done_reason
    agent = devices.get(f"agent{device_index}")
    device = devices.get(f"d{device_index}")
    controller = devices.get(f"controller{device_index}")
    if agent is None or device is None or controller is None:
        return

    if event_bus is None:
        event_bus = DeviceEventBus()

    last_llm_ts = 0.0
    last_signature = ""
    last_passive_probe_ts = 0.0
    llm_min_interval = float(os.getenv("OCTOPUS_EVENT_LLM_MIN_INTERVAL", "1.0"))
    passive_probe_interval = float(os.getenv("OCTOPUS_EVENT_PASSIVE_PROBE_INTERVAL", "2.2"))

    while not stop_event.is_set() and not task_done:
        try:
            current_device = pool.get_current_device()
            has_token = current_device == device_index
            participants = (pool.get_current_task_record() or {}).get("participating_devices", [])
            if episode_done or f"Device{device_index}" not in participants:
                sleep(0.2)
                continue

            # Event-driven wait: avoid hot XML polling when this device is not active.
            wait_timeout = 0.5 if has_token else 1.0
            got_event = event_bus.wait(wait_timeout)
            if got_event:
                event_bus.consume_all()

            now = time()
            should_passive_probe = (not has_token) and ((now - last_passive_probe_ts) >= passive_probe_interval)
            if not has_token and not got_event and not should_passive_probe:
                continue

            xml = device.dump_hierarchy(compressed=False, pretty=False)
            activity = device.app_current().get("activity", "").split("/")[-1]
            quick_signature = f"{activity}::{(xml or '')[:320]}"

            if not has_token:
                last_passive_probe_ts = now
                local_goal = (
                    pool.device_sub_task_list[device_index - 1]
                    if device_index <= len(pool.device_sub_task_list) else ""
                )
                goal_tokens = set(re.findall(r"[a-z0-9_]{3,}", local_goal.lower()))
                eligible_event = next((
                    event for event in pool.pending_peer_events(device=f"Device{device_index}")
                    if goal_tokens & set(re.findall(r"[a-z0-9_]{3,}", event.get("value", "").lower()))
                ), None)
                if not eligible_event:
                    continue
                claimed_event = pool.claim_peer_event(
                    pool.get_step_memory().get("task_id", ""),
                    device=f"Device{device_index}",
                    expected=eligible_event["value"],
                )
                if not claimed_event:
                    continue
                switched = pool.set_current_device(
                    device_index,
                    reason=claimed_event["value"],
                    source="peer_event",
                    cooldown=True,
                )
                if not switched and pool.get_current_device() != device_index:
                    continue
                pool.consume_peer_event(claimed_event["event_id"])

            # Throttle repeated identical-state LLM calls.
            if quick_signature == last_signature and (now - last_llm_ts) < llm_min_interval:
                continue
            last_signature = quick_signature
            last_llm_ts = now
            data_action = {"xml": xml, "activity": activity}
            result = agent.task_execution(data_action, pool)
            trace_writer.log_event(
                "device_parallel_worker_operator_result",
                current_device=device_index,
                response=result.response
            )

            # Token may be switched by agent output.
            if stop_event.is_set() or task_done or episode_done or pool.get_current_device() != device_index:
                sleep(0.1)
                continue

            if result.response.get("status") == 1:
                ledger = devices.get("execution_ledger")
                if ledger is not None:
                    ledger.mark_operator_end(device_index)
                episode_done_reason = "task_completed"
                episode_done = True
                trace_writer.log_event(
                    "device_parallel_worker_episode_finished",
                    current_device=device_index
                )
                while episode_done and not stop_event.is_set() and not task_done:
                    sleep(0.1)
                last_signature = ""
                last_llm_ts = 0.0
                continue

            if result.response.get("grounding_failed"):
                ledger = devices.get("execution_ledger")
                grounding_failures = ledger.record_grounding(True)
                pool.record_step_event(
                    "grounding_failure", device=f"Device{device_index}", count=grounding_failures
                )
                if grounding_failures >= 2:
                    episode_done_reason = "grounding_failure"
                    episode_done = True
                continue
            ledger = devices.get("execution_ledger")
            action_infos = result.response.get("action_infos", [])
            if any(
                item.get("action_type") in {ActionType.CLICK, ActionType.INPUT}
                for item in action_infos
            ):
                ledger.record_grounding(False)
            is_nop_wait = (
                action_infos
                and all(item.get("action_type", ActionType.NOP) == ActionType.NOP for item in action_infos)
                and not result.response.get("device_switch")
                and not result.response.get("invalid_switch")
                and not result.response.get("parse_error")
            )
            if is_nop_wait:
                peer_event = wait_for_task_peer_event(
                    pool,
                    pool.get_current_task_record(),
                    {device_index: event_bus},
                )
                pool.record_step_event(
                    "nop_wait", device=f"Device{device_index}", peer_event=peer_event
                )
                ledger = devices.get("execution_ledger")
                if peer_event and ledger is not None:
                    ledger.record_evidence(peer_event)
                continue
            exec_meta = execute_action_infos_scheduled(
                pool, controller, device, action_infos, device_index, devices
            )
            record_execution_feedback(
                pool,
                devices.get("coverage_store"),
                devices.get("execution_ledger"),
                device_index,
                exec_meta,
            )
            trace_writer.log_event(
                "device_parallel_worker_action_execution",
                current_device=device_index,
                exec_meta=exec_meta,
                action_infos=action_infos
            )
            input_events = exec_meta.get("input_events", [])
            if any(e.get("input_attempted") and not e.get("final_success_hint") for e in input_events):
                agent.feedback = "Input failed; retry on the grounded editable control."
                sleep(0.2)
                continue

            if exec_meta.get("had_critical_action"):
                no_progress = ledger.record_progress(exec_meta.get("changed"))
                if no_progress >= 3:
                    episode_done_reason = "no_progress"
                    episode_done = True

            first_action = action_infos[0].get("action_type", ActionType.NOP) if action_infos else ActionType.NOP
            if first_action == ActionType.NOP:
                sleep(0.25)
            else:
                sleep(0.08)
        except Exception as e:
            trace_writer.log_event(
                "device_parallel_worker_error",
                current_device=device_index,
                error=str(e)
            )
            sleep(0.25)


def run(argv=None, *, detailed_info=False):
    global task_done, episode_done, episode_done_reason
    task_done = False
    episode_done = False
    episode_done_reason = ""
    parser = argparse.ArgumentParser(prog="octopus")
    parser.add_argument("--task", help="initial task description; omit to generate automatically")
    parser.add_argument("--apk", default="",
                        help="optional APK path to install on all selected devices before testing")
    parser.add_argument("--app-package", default="",
                        help="target app package name; used for launching APK and static activity coverage")
    parser.add_argument("--multi-task", action="store_true",
                        help="after each finished episode, auto-generate the next task for multi-task coverage runs")
    parser.add_argument("--task-hint", default="",
                        help="optional preference for auto-generated task")
    parser.add_argument("--run-seconds", type=int, default=3600,
                        help="stop the experiment after this many seconds and dump coverage statistics")
    parser.add_argument("--task-timeout-seconds", type=int, default=0,
                        help="fail the current task after this many seconds; 0 disables per-task timeout")
    parser.add_argument("--max-actions", type=int, default=0,
                        help="stop cleanly after this many executed UI actions; 0 disables the budget")
    parser.add_argument("--max-llm-calls", type=int, default=0,
                        help="stop cleanly after this many LLM calls; 0 disables the budget")
    parser.add_argument("--max-total-tokens", type=int, default=0,
                        help="stop cleanly after this many reported LLM tokens; 0 disables the budget")
    parser.add_argument("--detailed-info", "--detailed-log", action="store_true", default=detailed_info,
                        help="retain full traces and exploration memory; default saves compact metrics only")
    parser.add_argument("--candidate-count", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--no-relevance", action="store_true")
    parser.add_argument("--no-diversity", action="store_true")
    parser.add_argument("--no-effect-feedback", action="store_true")
    parser.add_argument("--verification-rounds", type=int, choices=range(0, 6), default=2,
                        help="extra evidence observations; 0 disables observation refinement")
    parser.add_argument("--dip", nargs='+', required=True, help="device's ip")
    args = parser.parse_args(argv)
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    app_package = _resolve_app_package(args.apk, args.app_package)
    run_paths = build_run_paths(app_package, args.apk, run_ts)
    init_run_log_file(run_paths, detailed_info=args.detailed_info)

    task = args.task
    task_generation_enabled = args.multi_task or not bool(task)
    dip = args.dip
    dtype = ["default user"] * len(dip)
    trace_writer.log_event(
        "runtime_args",
        task=task,
        apk=os.path.abspath(args.apk) if args.apk else "",
        app_package=app_package,
        multi_task=args.multi_task,
        task_generation_enabled=task_generation_enabled,
        task_hint=args.task_hint,
        run_seconds=args.run_seconds,
        task_timeout_seconds=args.task_timeout_seconds,
        max_actions=args.max_actions,
        max_llm_calls=args.max_llm_calls,
        max_total_tokens=args.max_total_tokens,
        dip=dip,
        dtype=dtype
    )
    print(f"App: {app_package or 'current foreground app'} | Devices: {len(dip)} | "
          f"Model: {model_client.DEFAULT_MODEL} | Time budget: {args.run_seconds}s")
    run_started_at = time()

    model_client.reset_usage()
    runtime_budget = RuntimeBudget(
        max_actions=args.max_actions,
        max_llm_calls=args.max_llm_calls,
        max_total_tokens=args.max_total_tokens,
    )
    execution_ledger = ExecutionLedger(runtime_budget=runtime_budget)
    memory_pool = memory_store.MemoryPool(MethodOptions(
        candidate_count=args.candidate_count, relevance=not args.no_relevance,
        diversity=not args.no_diversity, effect_feedback=not args.no_effect_feedback,
        verification_rounds=args.verification_rounds,
    ))
    controller_dict = {
        "memory_pool": memory_pool,
        "runtime_budget": runtime_budget,
        "execution_ledger": execution_ledger,
    }
    coverage_store = CoverageStore(memory_pool.exploration_memory)
    controller_dict["coverage_store"] = coverage_store
    coverage_metrics = CoverageMetrics()
    activity_timeline = ActivityCoverageTimeline(
        log_dir=run_paths["run_dir"],
        sample_interval_seconds=60
    )
    task_result_log_path = run_paths["task_result_log_path"]
    androidlog_coverage_enabled = bool(args.apk)
    current_task_record = None

    code_coverage_metadata = {
        "mode": "androidlog_auto" if androidlog_coverage_enabled else "not_available_without_apk",
        "instrumented_apk_required": androidlog_coverage_enabled,
        "apk_path": os.path.abspath(args.apk) if args.apk else "",
        "androidlog_tag": AndroidLogCoverageParser.tag,
        "known_code_units": 0,
        "denominator_known": False,
    }
    if androidlog_coverage_enabled:
        print("[Code Coverage] Reading AndroidLog instrumentation points from APK...")
        known_code_units = extract_androidlog_instrumented_units(args.apk)
        if not known_code_units:
            raise RuntimeError(
                "No AndroidLog instrumentation points were found in the APK; "
                "code coverage cannot be calculated."
            )
        coverage_store.set_known_code_units(known_code_units)
        code_coverage_metadata["known_code_units"] = len(known_code_units)
        code_coverage_metadata["denominator_known"] = bool(known_code_units)
        trace_writer.log_event("androidlog_code_coverage_config", **code_coverage_metadata)

    listener_stop_event = threading.Event()

    _install_apk_on_devices(dip, args.apk)

    # Connect devices first so we can feed real-time UI context into LLM for auto task generation.
    for i in range(len(dip)):
        controller_dict[f"d{i + 1}"] = u2.connect(dip[i])
        controller_dict[f"controller{i + 1}"] = android_device.AndroidController(dip[i])
        controller_dict[f"agent{i + 1}"] = device_agent.DeviceAgent(i + 1, 1)

    if androidlog_coverage_enabled:
        androidlog_parser = AndroidLogCoverageParser()
        for idx, device_id in enumerate(dip, 1):
            start_androidlog_coverage_listener(
                device_id=device_id,
                device_index=idx,
                coverage_store=coverage_store,
                stop_event=listener_stop_event,
                parser=androidlog_parser,
            )
        print(
            f"[Code Coverage] AndroidLog listener started for {len(dip)} device(s); "
            f"known methods={code_coverage_metadata['known_code_units']}"
        )
        trace_writer.log_event(
            "androidlog_code_coverage_listener_started",
            device_count=len(dip),
            known_code_units=code_coverage_metadata["known_code_units"],
        )

    if app_package:
        _launch_app_on_devices(controller_dict, len(dip), app_package)

    def collect_contexts(only=None):
        return collect_device_contexts(dip, dtype, controller_dict, only=only)

    def collect_verification_contexts(only):
        observations = collect_contexts(only)
        record_context_coverage(coverage_store, observations)
        return observations

    contexts = collect_contexts()
    trace_writer.log_event("device_contexts_collected", contexts=contexts)
    if not app_package:
        app_package = _infer_app_package_from_contexts(contexts)
    coverage_metadata = _register_static_activity_baseline(coverage_store, dip, app_package, args.apk)
    record_context_coverage(
        coverage_store,
        contexts,
    )
    sample_activity_coverage_if_due(
        activity_timeline,
        coverage_metrics,
        coverage_store,
        time() - run_started_at,
        reason="initial",
        force=True,
    )
    task_results = []
    run_config = {
        "run_id": run_ts, "app_package": app_package, "model": model_client.DEFAULT_MODEL,
        "provider": model_client.ACTIVE_PROVIDER, "method": asdict(memory_pool.method),
        "arguments": vars(args), "activity_baseline": {
            key: value for key, value in coverage_metadata.items() if key != "known_activity_list"
        },
        "code_baseline": code_coverage_metadata,
    }

    def save_summary(status="running"):
        return write_run_summary(run_paths, run_config, task_results,
                                 coverage_metrics.compute(coverage_store), time() - run_started_at,
                                 runtime_budget.snapshot(), status)

    save_summary()
    initial_hint = args.task_hint
    if task:
        initial_hint = f"Structure this exact requested behavior; do not substitute another task: {task}"
    initial_retry_count = 0
    last_initial_console_at = 0
    while True:
        current_task_record, planning_reason = select_generated_task(
            dip, contexts, initial_hint, memory_pool, episode=1
        )
        if current_task_record:
            task = current_task_record["goal"]
            break
        initial_retry_count += 1
        trace_writer.log_event(
            "task_generation_initial_retry",
            episode=1,
            reason=planning_reason,
            retry_seconds=AUTO_TASK_IDLE_RETRY_SECONDS,
            consecutive_failures=initial_retry_count,
        )
        if time() - run_started_at >= args.run_seconds:
            raise RuntimeError(
                "No executable task was generated before the run timeout: "
                f"{planning_reason}"
            )
        now = time()
        if (
            last_initial_console_at <= 0
            or now - last_initial_console_at >= AUTO_TASK_IDLE_CONSOLE_INTERVAL_SECONDS
        ):
            print(
                f"[Task Generation] no initial executable task ({planning_reason}); "
                f"retrying in {AUTO_TASK_IDLE_RETRY_SECONDS}s."
            )
            last_initial_console_at = now
        sleep(AUTO_TASK_IDLE_RETRY_SECONDS)
        contexts = collect_contexts()
        trace_writer.log_event(
            "device_contexts_collected", episode=1, contexts=contexts, source="initial_retry"
        )
        record_context_coverage(
            coverage_store,
            contexts,
        )
        sample_activity_coverage_if_due(
            activity_timeline,
            coverage_metrics,
            coverage_store,
            time() - run_started_at,
            reason="periodic",
        )

    trace_writer.log_event("task_auto_generated", task=current_task_record or task)
    trace_writer.log_event("flow_selected", verifier="rule_based")

    coordinator = task_dispatcher.TaskDispatcher(current_task_record)
    coordinator.task_create(dtype, dip, memory_pool)
    trace_writer.log_event(
        "coordinator_task_create",
        task=task,
        device_num=coordinator.device_num,
        device_types=coordinator.device_type_list,
        sub_tasks=coordinator.sub_task_list,
        first_device=coordinator.first_device_num,
    )
    last_signature_map = {}
    memory_pool.register_switch_scheduler(RuntimeSwitchState(ledger=execution_ledger))
    begin_episode(
        memory_pool,
        execution_ledger,
        current_task_record,
        1,
        contexts,
        coverage_metrics,
        coverage_store,
    )

    device_event_buses = {}
    episode_index = 1
    episode_started_at = time()
    task_generation_idle = False
    idle_retry_count = 0
    last_idle_console_at = 0

    if len(dip) > 1:
        for index in range(1, len(dip) + 1):
            device_event_buses[index] = DeviceEventBus(
                memory_pool, index, app_package
            )
        _, accessibility_endpoint = start_accessibility_http_router(
            device_event_buses, listener_stop_event
        )
        trace_writer.log_event(
            "accessibility_helper_enabled",
            endpoint=accessibility_endpoint,
            target_devices=list(device_event_buses),
        )
        for index, device_id in enumerate(dip, 1):
            start_accessibility_event_helper(
                device_id,
                index,
                accessibility_endpoint,
                listener_stop_event,
            )
            if _env_enabled("OCTOPUS_EVENT_LOGCAT_FALLBACK", "0"):
                start_logcat_event_listener(
                    device_id, device_event_buses[index], listener_stop_event
                )
        for index in range(2, len(dip) + 1):
            thread = threading.Thread(
                target=device_parallel_worker_loop,
                args=(
                    index,
                    controller_dict,
                    memory_pool,
                    listener_stop_event,
                    device_event_buses[index],
                ),
                daemon=True,
            )
            thread.start()
        trace_writer.log_event(
            "parallel_worker_started", device_count=max(0, len(dip) - 1)
        )

    run_status = "completed"
    try:
        while not task_done:
            sample_activity_coverage_if_due(
                activity_timeline,
                coverage_metrics,
                coverage_store,
                time() - run_started_at,
                reason="periodic",
            )
            budget_reason = runtime_budget.exhaustion_reason()
            if budget_reason and not episode_done:
                trace_writer.log_event(
                    "budget_exhausted",
                    reason=budget_reason,
                    budget=runtime_budget.snapshot(),
                )
                episode_done_reason = budget_reason
                episode_done = True
                continue
            if not episode_done and time() - run_started_at >= args.run_seconds:
                trace_writer.log_event("run_timeout", run_seconds=args.run_seconds)
                episode_done_reason = "run_timeout_failure"
                episode_done = True
                continue
            if task_generation_idle:
                current_contexts = collect_contexts()
                trace_writer.log_event("device_contexts_collected", episode=episode_index, contexts=current_contexts, source="idle_retry")
                current_task_record, planning_reason = select_generated_task(
                    dip, current_contexts, args.task_hint, memory_pool, episode_index
                )
                next_task = current_task_record["goal"] if current_task_record else ""
                if not current_task_record:
                    idle_retry_count += 1
                    trace_writer.log_event(
                        "task_generation_idle_retry",
                        episode=episode_index,
                        reason=planning_reason,
                        retry_seconds=AUTO_TASK_IDLE_RETRY_SECONDS,
                        consecutive_failures=idle_retry_count,
                    )
                    now = time()
                    if (
                        last_idle_console_at <= 0
                        or now - last_idle_console_at >= AUTO_TASK_IDLE_CONSOLE_INTERVAL_SECONDS
                    ):
                        print(
                            f"[Task Generation] no executable task right now ({planning_reason}); "
                            f"retrying in {AUTO_TASK_IDLE_RETRY_SECONDS}s until run timeout."
                        )
                        last_idle_console_at = now
                    sleep(AUTO_TASK_IDLE_RETRY_SECONDS)
                    continue

                idle_retry_count = 0
                last_idle_console_at = 0
                task = next_task
                trace_writer.log_event("task_auto_generated", episode=episode_index, task=current_task_record or task)
                coordinator = task_dispatcher.TaskDispatcher(current_task_record)
                coordinator.task_create(dtype, dip, memory_pool)
                trace_writer.log_event(
                    "coordinator_task_create",
                    episode=episode_index,
                    task=task,
                    device_num=coordinator.device_num,
                    device_types=coordinator.device_type_list,
                    sub_tasks=coordinator.sub_task_list,
                    first_device=coordinator.first_device_num
                )
                begin_episode(
                    memory_pool, execution_ledger, current_task_record, episode_index,
                    current_contexts, coverage_metrics, coverage_store,
                )
                episode_done = False
                episode_done_reason = ""
                episode_started_at = time()
                task_generation_idle = False
                continue
            if (
                args.task_timeout_seconds > 0
                and not episode_done
                and (time() - episode_started_at) >= args.task_timeout_seconds
            ):
                episode_done_reason = "task_timeout_failure"
                trace_writer.log_event(
                    "task_episode_timeout",
                    episode=episode_index,
                    task=task,
                    timeout_seconds=args.task_timeout_seconds
                )
                print(
                    f"[Episode Fail] task timed out after {args.task_timeout_seconds}s. "
                    "Moving to next task."
                )
                episode_done = True
                continue
            if episode_done:
                final_reason = episode_done_reason or "task_completed"
                current_contexts = collect_contexts()
                trace_writer.log_event("device_contexts_collected", episode=episode_index, contexts=current_contexts)
                record_context_coverage(
                    coverage_store,
                    current_contexts,
                )
                sleep(0.2)
                confirmation_contexts = collect_contexts()
                record_context_coverage(coverage_store, confirmation_contexts)
                coverage_after = coverage_metrics.compute(coverage_store)
                result_record = build_task_result(
                    episode_index,
                    task,
                    current_task_record,
                    final_reason,
                    [current_contexts, confirmation_contexts],
                    memory_pool.get_current_device(),
                    coverage_after,
                    execution_ledger,
                    memory_pool=memory_pool,
                    observe=collect_verification_contexts,
                    deadline=run_started_at + args.run_seconds,
                )
                latest = result_record["terminal_global_state"]["devices"]
                current_contexts = [{**context, **latest.get(f"Device{context['device_index']}", {})}
                                    for context in confirmation_contexts]
                result_record["coverage_after"] = coverage_metrics.compute(coverage_store)
                result_record["coverage_delta"] = CoverageMetrics.delta(
                    result_record["coverage_before"], result_record["coverage_after"]
                )
                memory_pool.record_step_event(
                    "task_verification",
                    result=result_record["result"],
                    reason=result_record["reason"],
                    failure_category=result_record["failure_category"],
                    verification=result_record["verification"],
                )
                result_record["step_memory"] = memory_pool.get_step_memory()
                compact_result = append_task_result(
                    task_result_log_path,
                    result_record,
                    memory_pool=memory_pool,
                    detailed_info=args.detailed_info,
                    info_path=run_paths["info_path"],
                )

                task_results.append(compact_result)
                print_task_result(compact_result)
                save_summary()
                episode_index += 1
                trace_writer.log_event(
                    "task_episode_finished",
                    episode=episode_index - 1,
                    task=task,
                    reason=final_reason,
                    result=result_record["result"],
                    verification=result_record["verification"],
                    coverage_delta=result_record["coverage_delta"],
                    success=result_record["success"],
                )
                last_signature_map = {}
                memory_pool.clear_step_memory()

                if final_reason == "run_timeout_failure":
                    task_done = True
                    break
                budget_reason = runtime_budget.exhaustion_reason()
                if budget_reason:
                    trace_writer.log_event(
                        "budget_exhausted",
                        reason=budget_reason,
                        budget=runtime_budget.snapshot(),
                    )
                    task_done = True
                    break

                if not task_generation_enabled:
                    task_done = True
                    break

                current_task_record, planning_reason = select_generated_task(
                    dip, current_contexts, args.task_hint, memory_pool, episode_index
                )
                next_task = current_task_record["goal"] if current_task_record else ""
                if not current_task_record:
                    trace_writer.log_event("task_generation_exhausted", episode=episode_index, reason=planning_reason)
                    task_generation_idle = True
                    idle_retry_count = 0
                    last_idle_console_at = 0
                    episode_done = False
                    episode_done_reason = ""
                    continue
                trace_writer.log_event("task_auto_generated", episode=episode_index, task=current_task_record or next_task)

                task = next_task
                coordinator = task_dispatcher.TaskDispatcher(current_task_record)
                coordinator.task_create(dtype, dip, memory_pool)
                trace_writer.log_event(
                    "coordinator_task_create",
                    episode=episode_index,
                    task=task,
                    device_num=coordinator.device_num,
                    device_types=coordinator.device_type_list,
                    sub_tasks=coordinator.sub_task_list,
                    first_device=coordinator.first_device_num
                )
                begin_episode(
                    memory_pool, execution_ledger, current_task_record, episode_index,
                    current_contexts, coverage_metrics, coverage_store,
                )
                episode_done = False
                episode_done_reason = ""
                episode_started_at = time()
                task_done = False
                continue
            current_device = memory_pool.get_current_device()
            if len(dip) >= 2 and current_device != 1:
                sleep(0.1)
                continue
            agent = controller_dict.get(f"agent{current_device}")
            device = controller_dict.get(f"d{current_device}")
            controller = controller_dict.get(f"controller{current_device}")
            sleep(0.2)
            xml = device.dump_hierarchy(compressed=False, pretty=False)
            app_info = device.app_current()
            package_name = app_info.get("package", "")
            activity = app_info.get("activity", "").split("/")[-1]
            all_comps = get_all_comps(xml)
            state_ui_summary = get_task_ui_summary(xml)
            trace_writer.log_event(
                "device_step_input",
                current_device=current_device,
                activity=activity,
                xml=xml,
                all_comps=all_comps
            )
            signature = state_signature(
                activity,
                state_ui_summary,
                package_name=package_name,
                device_id=current_device,
            )

            previous_signature = last_signature_map.get(current_device)
            last_signature_map[current_device] = signature
            stable_signature = _record_coverage_state(
                coverage_store,
                current_device,
                activity,
                state_ui_summary,
                signature,
                previous_signature or "",
                package_name=package_name,
            )
            memory_pool.record_step_event(
                "observation",
                device=f"Device{current_device}",
                package=package_name,
                activity=activity,
                signature=stable_signature,
                previous_signature=previous_signature or "",
                no_progress_count=execution_ledger.no_progress_count(),
            )
            sample_activity_coverage_if_due(
                activity_timeline,
                coverage_metrics,
                coverage_store,
                time() - run_started_at,
                reason="periodic",
            )

            data_action = {"xml": xml, "activity": activity}
            result = agent.task_execution(data_action, memory_pool)
            trace_writer.log_event("operator_result", current_device=current_device, response=result.response)

            if result.response["status"] == 1:
                execution_ledger.mark_operator_end(current_device)
                episode_done_reason = "task_completed"
                episode_done = True
            else:
                if result.response.get("grounding_failed"):
                    grounding_failures = execution_ledger.record_grounding(True)
                    memory_pool.record_step_event(
                        "grounding_failure",
                        device=f"Device{current_device}",
                        count=grounding_failures,
                    )
                    if grounding_failures >= 2:
                        episode_done_reason = "grounding_failure"
                        episode_done = True
                    continue
                action_infos = result.response.get("action_infos", [])
                if any(
                    item.get("action_type") in {ActionType.CLICK, ActionType.INPUT}
                    for item in action_infos
                ):
                    execution_ledger.record_grounding(False)
                is_nop_wait = (
                    action_infos
                    and all(item.get("action_type", ActionType.NOP) == ActionType.NOP for item in action_infos)
                    and not result.response.get("device_switch")
                    and not result.response.get("invalid_switch")
                    and not result.response.get("parse_error")
                )
                if is_nop_wait:
                    peer_event = wait_for_task_peer_event(
                        memory_pool, current_task_record, device_event_buses
                    )
                    memory_pool.record_step_event(
                        "nop_wait",
                        device=f"Device{current_device}",
                        peer_event=peer_event,
                    )
                    if peer_event:
                        execution_ledger.record_evidence(peer_event)
                    continue
                # If listener preempted token during this step, drop stale action to avoid cross-device race.
                if memory_pool.get_current_device() != current_device:
                    trace_writer.log_event(
                        "stale_operator_result_skipped",
                        current_device=current_device,
                        latest_device=memory_pool.get_current_device()
                    )
                    continue
                exec_meta = execute_action_infos_scheduled(
                    memory_pool,
                    controller,
                    device,
                    action_infos,
                    current_device,
                    controller_dict,
                )
                record_execution_feedback(
                    memory_pool,
                    coverage_store,
                    execution_ledger,
                    current_device,
                    exec_meta,
                )
                trace_writer.log_event("action_execution", current_device=current_device, exec_meta=exec_meta,
                                     action_infos=result.response.get("action_infos", []))
                input_events = exec_meta.get("input_events", [])
                if input_events:
                    input_failed = any(e.get("input_attempted") and not e.get("final_success_hint") for e in input_events)
                    if input_failed:
                        agent.feedback = (
                            "Input failed: expected text not visible after input. "
                            "Retry input on focused field before proceeding."
                        )
                if exec_meta["had_critical_action"]:
                    no_progress = execution_ledger.record_progress(exec_meta["changed"])
                    if not exec_meta["changed"]:
                        agent.feedback = "The last grounded action made no progress; choose another path."
                        if no_progress >= 3:
                            episode_done_reason = "no_progress"
                            episode_done = True


    except KeyboardInterrupt:
        run_status = "interrupted"
    except Exception:
        listener_stop_event.set()
        save_summary("error")
        raise

    listener_stop_event.set()
    sample_activity_coverage_if_due(
        activity_timeline, coverage_metrics, coverage_store, time() - run_started_at,
        reason="final", force=True,
    )
    summary = save_summary(run_status)
    if args.detailed_info:
        with open(run_paths["exploration_memory_path"], "w", encoding="utf-8") as handle:
            json.dump(memory_pool.get_exploration_snapshot(), handle, ensure_ascii=False,
                      default=lambda value: sorted(value) if isinstance(value, set) else str(value))
    print_run_summary(summary, run_paths)
    trace_writer.log_event("run_end", summary=summary)
