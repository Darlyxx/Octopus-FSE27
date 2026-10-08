"""Build campaign paths, statistics, and result records."""

import json
import os
import re
import csv
from datetime import datetime
from dataclasses import asdict
from time import time

from octopus.services import model_client, trace_writer
from octopus.services.coverage_store import CoverageMetrics
from octopus.outcome_verifier import TaskResultVerifier
from octopus.ui_context import state_signature
from octopus.task_schema import task_fingerprint, text_similarity


def build_run_paths(app_package, apk_path, run_ts):
    name = app_package or os.path.splitext(os.path.basename(apk_path or ""))[0] or "unknown_app"
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "unknown_app"
    run_dir = os.path.join("logs", name, run_ts)
    os.makedirs(run_dir, exist_ok=True)
    return {
        "run_dir": run_dir,
        "info_path": os.path.join(run_dir, "info.jsonl"),
        "task_result_log_path": os.path.join(run_dir, "task_results.jsonl"),
        "exploration_memory_path": os.path.join(run_dir, "exploration_memory.json"),
        "summary_path": os.path.join(run_dir, "summary.json"),
        "metrics_path": os.path.join(run_dir, "metrics.csv"),
    }


def init_run_log_file(paths, detailed_info=False):
    os.environ["OCTOPUS_RUN_DIR"] = paths["run_dir"]
    info_path = paths["info_path"] if detailed_info else ""
    trace_writer.set_log_path(info_path)
    trace_writer.log_event("run_start", **paths)
    print(f"\noctopus | detailed logs: {'on' if detailed_info else 'off'}\nResults: {paths['run_dir']}")


def build_global_state(contexts, active_device):
    devices = {}
    for context in contexts or []:
        index = context.get("device_index") if isinstance(context, dict) else None
        if index is None:
            continue
        name = f"Device{index}"
        devices[name] = {
            "device": name,
            "device_id": context.get("device_id", ""),
            "package": context.get("package", ""),
            "activity": context.get("activity", ""),
            "ui_summary": context.get("ui_summary", ""),
            "all_comps": context.get("all_comps", ""),
            "context_collection_error": context.get("context_collection_error", ""),
            "signature": context.get("signature") or state_signature(
                context.get("activity", ""),
                context.get("ui_summary", ""),
                context.get("package", ""),
                index,
            ),
        }
    return {"active_device": f"Device{active_device}", "devices": devices}


def begin_episode(memory_pool, ledger, task_record, episode, contexts, coverage_metrics,
                  coverage_store):
    task_id = task_record.get("task_id") or f"episode_{episode}"
    memory_pool.begin_task(task_id, task_record)
    ledger.begin(
        task_id,
        build_global_state(contexts, memory_pool.get_current_device()),
        coverage_metrics.compute(coverage_store),
        model_client.get_usage(),
    )
    trace_writer.log_event("task_start", task_id=task_id, episode=episode)


def build_task_result(episode, task, task_record, reason, terminal_contexts,
                      active_device, coverage_after, ledger, memory_pool=None,
                      observe=None, deadline=None):
    execution = ledger.snapshot()
    context_snapshots = (
        terminal_contexts
        if terminal_contexts and isinstance(terminal_contexts[0], list)
        else [terminal_contexts]
    )
    terminal_states = [
        build_global_state(contexts, active_device) for contexts in context_snapshots
    ]
    evidence = list(execution["evidence"])
    # Events must belong to this task interval, even if routing has not claimed them.
    def refresh_events():
        if memory_pool is not None:
            evidence[:] = [item for item in evidence if "event_id" not in item]
            evidence.extend(event for event in memory_pool.peer_event_history()
                            if event["event_id"] not in memory_pool.task_event_ids
                            and event["created_at"] >= execution["started_at"])

    refresh_events()

    def observe_device(device_name):
        contexts = observe(device_name)
        refresh_events()
        return build_global_state(contexts, active_device)

    verification = TaskResultVerifier().verify(
        task_record,
        terminal_states,
        evidence=evidence,
        termination_reason=reason,
        operator_end_requested=execution["operator_end_requested"],
        observe=observe_device if observe else None,
        max_rounds=memory_pool.method.verification_rounds if memory_pool else 0,
        deadline=deadline,
    )
    usage = {
        key: max(0, model_client.get_usage().get(key, 0) - execution["llm_usage_before"].get(key, 0))
        for key in ("llm_call_count", "prompt_tokens", "completion_tokens", "total_tokens")
    }
    result = verification["result"]
    return {
        "task_id": task_record.get("task_id", execution["task_id"]),
        "episode": episode,
        "task": task,
        "task_record": task_record,
        "participating_devices": task_record.get("participating_devices", []),
        "start_global_state": execution["start_global_state"],
        "terminal_global_state": {"active_device": f"Device{active_device}", "devices": {
            name: state for snapshot in terminal_states for name, state in snapshot["devices"].items()
        }},
        "terminal_state_snapshots": terminal_states,
        "result": result,
        "success": result == "success",
        "failure_category": verification["failure_category"],
        "reason": verification["reason"],
        "finish_reason": reason,
        "evidence": evidence,
        "verification": verification,
        "coverage_before": execution["coverage_before"],
        "coverage_after": coverage_after,
        "coverage_delta": CoverageMetrics.delta(execution["coverage_before"], coverage_after),
        "action_count": execution["action_count"],
        "device_switch_count": execution["device_switch_count"],
        "event_triggered_switch_count": execution["event_triggered_switch_count"],
        "duration": round(max(0, time() - execution["started_at"]), 3),
        "method": asdict(memory_pool.method) if memory_pool else {},
        **usage,
    }


def summarize_coverage(before, after):
    """Overall coverage ratios and newly observed units; unavailable ratios are null."""
    before = (before or {}).get("__overall__", {})
    after = (after or {}).get("__overall__", {})
    summary = {}
    for name, metric, count, new_count in (
        ("activity", "activity_cov", "covered_activities", "new_activities"),
        ("method", "method_cov", "covered_methods", "new_methods"),
        ("class", "class_cov", "covered_classes", "new_classes"),
    ):
        available_before = before.get(metric) is not None
        available_after = after.get(metric) is not None
        if not available_after:
            summary[name] = None
            continue
        summary[name] = {
            "before": round(before[metric], 6) if available_before else None,
            "after": round(after[metric], 6),
            "gain": round(after[metric] - before[metric], 6) if available_before else None,
            new_count: (
                after[count] - before[count] if count in before and count in after else None
            ),
        }
    return summary


def summarize_actions(evidence):
    """Keep executed UI actions in order, without grounding or runtime metadata."""
    actions = []
    for operation in evidence:
        if operation.get("type") not in {"Tap", "Input", "Back"}:
            continue
        action = {"type": operation["type"], "device": operation.get("device", "")}
        if operation.get("target_label"):
            action["target"] = operation["target_label"]
        actions.append(action)
    return actions


def append_task_result(path, result_record, *, detailed_info=False, info_path=None,
                       memory_pool=None):
    """Write a concise public result and optionally the full evidence to info.jsonl.

    The original result remains available to task memory and the planner.
    """
    actions = summarize_actions(result_record.get("evidence", []))
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "task_id": result_record["task_id"],
        "episode": result_record["episode"],
        "task": result_record["task"],
        "result": result_record["result"],
        "reason": result_record["reason"],
        "duration_seconds": result_record.get("duration"),
        "action_count": result_record.get("action_count", len(actions)),
        "actions": actions,
        "fingerprint": task_fingerprint(result_record.get("task_record", result_record.get("task", ""))),
        "participant_count": len(result_record.get("participating_devices", [])),
        "failure_category": result_record.get("failure_category", ""),
        "device_switch_count": result_record.get("device_switch_count", 0),
        "event_triggered_switch_count": result_record.get("event_triggered_switch_count", 0),
        "verification_rounds": result_record.get("verification", {}).get("observation_rounds", 0),
        "usage": {key: result_record.get(key, 0) for key in (
            "llm_call_count", "prompt_tokens", "completion_tokens", "total_tokens")},
        "coverage": summarize_coverage(
            result_record.get("coverage_before"), result_record.get("coverage_after")
        ),
        "method": result_record.get("method", {}),
    }
    if detailed_info:
        info_path = info_path or os.path.join(os.path.dirname(path or ""), "info.jsonl")
        trace_writer.write_event(
            info_path, "task_info", task_id=record["task_id"], episode=record["episode"],
            result_record=result_record,
        )
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if memory_pool is not None:
        memory_pool.record_task_result(result_record)
    return record


def summarize_tasks(records):
    groups = []
    for record in records:
        fingerprint = record["fingerprint"]
        group = next((group for group in groups
                      if text_similarity(fingerprint, group["fingerprint"]) >= 0.95), None)
        if group is None:
            group = {"fingerprint": fingerprint, "success": False}
            groups.append(group)
        group["success"] |= record["result"] == "success"
    successes = sum(record["result"] == "success" for record in records)
    unknown = sum(record["result"] == "unknown" for record in records)
    return {
        "attempt_count": len(records), "success_attempts": successes,
        "failed_attempts": len(records) - successes - unknown, "unknown_attempts": unknown,
        "task_count": len(groups), "successful_task_count": sum(group["success"] for group in groups),
        "task_success_rate": sum(group["success"] for group in groups) / len(groups) if groups else None,
        "attempt_success_rate": successes / len(records) if records else None,
        "single_device_attempts": sum(record.get("participant_count", 0) == 1 for record in records),
        "multi_device_attempts": sum(record.get("participant_count", 0) > 1 for record in records),
    }


def write_run_summary(paths, config, records, coverage, elapsed_seconds, budget, status="running"):
    # The internal legacy code_cov alias uses zero for unavailable coverage.
    # Public reports use method_cov, whose missing denominator is explicitly null.
    coverage = {scope: {key: value for key, value in values.items() if key != "code_cov"}
                for scope, values in coverage.items()}
    metrics = {**summarize_tasks(records), **coverage.get("__overall__", {})}
    summary = {
        "status": status, "config": config, "elapsed_seconds": round(elapsed_seconds, 3),
        "metrics": metrics, "usage_and_budget": budget, "coverage_by_device": coverage,
        "measurement": {
            "activity_denominator": "statically discovered target activities; external activities excluded",
            "method_denominator": "AndroidLog instrumented method signatures; library filtering depends on instrumentation",
            "class_denominator": "declaring classes of instrumented methods, not all APK classes",
            "task_deduplication": "normalized task fingerprint token cosine >= 0.95; first matching representative",
            "task_success_rate": "distinct tasks with at least one successful attempt / distinct attempted tasks",
            "usage_scope": "whole run, including planning; per-task usage covers execution and verification",
            "confirmed_issue_count": None,
        },
    }
    with open(paths["summary_path"], "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    arguments = config.get("arguments", {})
    row = {"run_id": config["run_id"], "app": config["app_package"], "status": status,
           "model": config["model"], "elapsed_seconds": summary["elapsed_seconds"],
           "run_seconds": arguments.get("run_seconds"),
           "task_timeout_seconds": arguments.get("task_timeout_seconds"),
           "device_count": len(arguments.get("dip", [])),
           **config["method"], **metrics, **budget}
    with open(paths["metrics_path"], "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    return summary


def print_task_result(record):
    print(f"[Task {record['episode']}] {record['result'].upper()} | "
          f"{record['duration_seconds']:.1f}s | {record['action_count']} actions | "
          f"{record['usage']['total_tokens']} tokens | {record['reason']}")


def print_run_summary(summary, paths):
    metrics = summary["metrics"]
    def percent(value):
        return f"{value:.1%}" if value is not None else "N/A"
    print(f"\nRun {summary['status']} | {summary['elapsed_seconds']:.1f}s\n"
          f"Tasks: {metrics['task_count']} distinct / {metrics['attempt_count']} attempts | "
          f"success {percent(metrics['task_success_rate'])} | unknown {metrics['unknown_attempts']}\n"
          f"Coverage: Activity {percent(metrics.get('activity_cov'))} | "
          f"Method {percent(metrics.get('method_cov'))} | Class {percent(metrics.get('class_cov'))}\n"
          f"LLM: {summary['usage_and_budget'].get('llm_call_count', 0)} calls | "
          f"{summary['usage_and_budget'].get('total_tokens', 0)} reported tokens\n"
          f"Metrics: {paths['metrics_path']}\nSummary: {paths['summary_path']}")
