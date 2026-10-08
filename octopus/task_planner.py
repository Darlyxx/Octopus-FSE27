"""Generate a small task batch and rank it against the current UI and history."""

import json
import re

from octopus import task_schema
from octopus.services import model_client, trace_writer
from octopus.ui_context import state_signature
from octopus.experiment_config import MethodOptions

def _console_text(value, limit=240):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _render_context_lines(contexts: list):
    context_lines = []
    for ctx in contexts:
        if "context_collection_error" in ctx:
            context_lines.append(
                f"Device{ctx['device_index']} (id={ctx['device_id']}, role={ctx['role']})\n"
                f"- context_collection_error: {ctx['context_collection_error']}\n"
            )
            continue
        context_lines.append(
            f"Device{ctx['device_index']} (id={ctx['device_id']}, role={ctx['role']})\n"
            f"- package: {ctx['package']}\n"
            f"- activity_metadata: {ctx['activity']} (metadata only; do not mention this in task descriptions)\n"
            f"- ui_summary fields: label/content_desc are user-facing names; resource_id is locator metadata only\n"
            f"{ctx['ui_summary']}\n"
        )
    return context_lines


def _render_task_history(task_history):
    descriptions = []
    for item in task_history or []:
        description = item.get("description", "") if isinstance(item, dict) else item
        description = str(description or "").strip()
        if description:
            descriptions.append(description)
    if not descriptions:
        return "No earlier tasks have been selected."
    return "\n".join(f"- {description}" for description in descriptions[-12:])


def _render_campaign_summary(campaign_summary):
    summary = campaign_summary if isinstance(campaign_summary, dict) else {}
    if not summary:
        return "No campaign memory is available yet."
    compact = {
        "frontier_states": summary.get("frontier_states", [])[:10],
        "uncovered_activities": summary.get("uncovered_activities", [])[:12],
        "uncovered_code_units": summary.get("uncovered_code_units", [])[:12],
        "recent_task_results": summary.get("recent_task_results", [])[-8:],
        "coverage_gain_by_task_type": summary.get("coverage_gain_by_task_type", {}),
        "no_progress_actions": summary.get("no_progress_actions", [])[:10],
        "known_paths": summary.get("known_paths", [])[:8],
    }
    return json.dumps(compact, ensure_ascii=False, sort_keys=True)


def _build_planner_prompt(device_ids: list, task_hint: str = "", task_history=None,
                          campaign_summary=None, method=None):
    method = method or MethodOptions()
    planner = model_client.GeneralGPT()
    if not method.diversity:
        campaign_summary = {key: value for key, value in (campaign_summary or {}).items()
                            if key != "recent_task_results"}
    schema = {
        "tasks": [{
            "task_id": "task_1",
            "goal": "On Device1, complete one concrete behavior and verify its result.",
            "participating_devices": ["Device1"],
            "first_device": "Device1",
            "device_goals": [{"device": "Device1", "goal": "local objective"}],
            "grounding_information": [{
                "device": "Device1", "control": "visible control", "state_signature": "optional"
            }],
            "verification_conditions": [{
                "device": "Device1",
                "type": "foreground_activity|visible|peer_event",
                "expected": "observable value",
            }],
        }]
    }
    prompt = (
        f"Generate at most {method.candidate_count} distinct grounded Android testing tasks for Device1..Device{len(device_ids)}.\n"
        "A Task is the final business testing objective; GUI actions are chosen later from fresh observations.\n"
        "Each task must contain a global goal, participating devices, one local goal per participant, "
        "the first device, grounding information, and structured verification conditions.\n"
        "A task is multi-device only when completion or verification depends on an observable peer effect.\n"
        "Use only foreground_activity, visible, or peer_event verification. Never output an action sequence.\n"
        "Ground tasks in current controls, reached states, frontier controls, pending events, known paths, "
        "activity coverage gaps, and code coverage gaps.\n"
        "Completion conditions must describe only outcomes required by the goal, not merely actions issued. "
        "Use observable text or activity evidence; never invent resource IDs or exact unseen UI text.\n"
        "Return strict JSON only in this schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n"
        "Executed tasks:\n"
        f"{_render_task_history(task_history if method.diversity else [])}\n"
        + ("Avoid behavior already explored in a similar screen context.\n" if method.diversity else "")
        + "Session memory:\n"
        f"{_render_campaign_summary(campaign_summary)}\n"
    )
    if task_hint.strip():
        prompt += f"User preference: {task_hint.strip()}\n"
    return planner, prompt


def generate_tasks_from_context(device_ids, contexts, task_hint="", task_history=None,
                                campaign_summary=None, rejected_sink=None, method=None):
    method = method or MethodOptions()
    planner, prompt = _build_planner_prompt(
        device_ids, task_hint, task_history, campaign_summary, method
    )
    prompt += "\nCurrent device contexts:\n" + "\n".join(_render_context_lines(contexts))
    result = planner.ask_gpt_message(messages=[
        {"role": "system", "content": "Generate grounded structured mobile test tasks."},
        {"role": "user", "content": prompt},
    ])
    candidates, seen = [], set()
    for item in task_schema.parse_generated_tasks(result.get("content", ""))[:method.candidate_count]:
        validation = task_schema.validate_task(item, device_ids)
        if not validation["valid"]:
            if rejected_sink:
                rejected_sink({"task_record": item, "result": "rejected",
                               "reason": ",".join(validation["errors"])})
            continue
        task = validation["task"]
        fingerprint = task_schema.task_fingerprint(task)
        if fingerprint not in seen:
            candidates.append(task)
            seen.add(fingerprint)
    return candidates


def select_generated_task(device_ids, contexts, task_hint, pool, episode):
    # Keep alternatives with their source screen; a different screen gets fresh tasks.
    state_key = tuple(state_signature(
        ctx.get("activity", ""), ctx.get("ui_summary", ""),
        ctx.get("package", ""), ctx.get("device_index", ""),
    ) for ctx in contexts)
    candidates = pool.task_candidates.get(state_key)
    if candidates is None:
        candidates = generate_tasks_from_context(
            device_ids, contexts, task_hint, pool.get_task_history(),
            pool.get_planner_summary(), pool.record_task_result, pool.method,
        )
        if candidates:
            pool.task_candidates[state_key] = candidates
    ranked = task_schema.rank_task_candidates(
        candidates, pool.get_task_memory(), contexts=contexts, method=pool.method,
    )
    if not ranked:
        # Retry generation next time, using the updated history and observations.
        pool.task_candidates.pop(state_key, None)
        return None, "no_candidate_survived_selection"
    selected = ranked[0]
    selected["task_id"] = f"task_{episode}"
    pool.task_candidates[state_key] = [
        task for task in candidates
        if task_schema.task_fingerprint(task) != selected["fingerprint"]
    ]
    pool.record_task_history(selected, episode=episode)
    trace_writer.log_event("task_candidate_selected", episode=episode, task=selected)
    print(f"[Task {episode}] {_console_text(selected['goal'])} "
          f"| devices: {', '.join(selected['participating_devices'])}")
    return selected, ""
