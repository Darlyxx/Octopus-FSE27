"""Ground, execute, and evaluate device actions."""

import re

from octopus.services import trace_writer
from octopus.services.coverage_timeline import ActivityCoverageTimeline
from octopus.services.android_device import ActionType
from octopus.services.coverage_store import CoverageMetrics, CoverageStore
from octopus.ui_context import get_all_comps, get_task_ui_summary, state_signature
from octopus.outcome_verifier import _state_value, _matches


def _device_number(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0

def capture_runtime_state(device, device_id=""):
    xml = device.dump_hierarchy(compressed=False, pretty=False)
    app_info = device.app_current()
    package_name = app_info.get("package", "")
    activity = app_info.get("activity", "").split("/")[-1]
    all_comps = get_all_comps(xml)
    state_ui_summary = get_task_ui_summary(xml)
    return {
        "device": str(device_id or ""),
        "package": package_name,
        "activity": activity,
        "all_comps": all_comps,
        "state_ui_summary": state_ui_summary,
        "signature": state_signature(
            activity, state_ui_summary, package_name=package_name, device_id=device_id
        )
    }


def _record_coverage_state(coverage_store: CoverageStore, device_id: int, activity: str,
                           all_comps: str, signature: str, previous_signature: str,
                           package_name: str = "",
                           action_label=None, action_outcome=None):
    stable_signature = coverage_store.record_observation(
        device_id,
        signature,
        all_comps,
        previous_signature,
        activity=activity,
        package_name=package_name,
        action_label=action_label,
        action_outcome=action_outcome,
    )
    return stable_signature


def sample_activity_coverage_if_due(activity_timeline: ActivityCoverageTimeline,
                                    coverage_metrics: CoverageMetrics,
                                    coverage_store: CoverageStore,
                                    elapsed_seconds: float,
                                    reason: str = "periodic",
                                    force: bool = False):
    if activity_timeline is None:
        return False
    if not force and not activity_timeline.should_sample(elapsed_seconds):
        return False
    sampled = activity_timeline.sample(
        coverage_metrics.compute(coverage_store),
        elapsed_seconds,
        reason=reason,
    )
    return sampled


def execute_action_infos(controller, device, action_infos: list, device_index: int = 0):
    def _input_with_fallback(bounds, text):
        if not text:
            return {"input_attempted": False}

        input_meta = {
            "input_attempted": True,
            "text": text,
            "primary_method": "uiautomator2.focused_set_text",
            "fallback_method": "adb_broadcast",
            "primary_success_hint": False,
            "fallback_used": False,
            "final_success_hint": False
        }

        if bounds and len(bounds) >= 4:
            controller.tap(bounds[:2], bounds[2:])

        primary_ret = None
        try:
            # Most reliable path for current environment.
            device(focused=True).set_text(text)
            primary_ret = "ok"
        except Exception as e:
            primary_ret = f"error:{e}"
        input_meta["primary_ret"] = primary_ret

        # Heuristic success check from updated xml; if absent, try fallback.
        try:
            xml_after_primary = device.dump_hierarchy(compressed=False, pretty=False)
            if text in (xml_after_primary or ""):
                input_meta["primary_success_hint"] = True
                input_meta["final_success_hint"] = True
                return input_meta
        except Exception:
            pass

        # Fallback via legacy ADB broadcast path.
        try:
            controller.text(text)
            input_meta["fallback_used"] = True
            try:
                xml_after_fallback = device.dump_hierarchy(compressed=False, pretty=False)
                if text in (xml_after_fallback or ""):
                    input_meta["final_success_hint"] = True
                else:
                    input_meta["final_success_hint"] = False
            except Exception:
                input_meta["final_success_hint"] = False
        except Exception as e:
            input_meta["fallback_error"] = str(e)
            input_meta["final_success_hint"] = input_meta["primary_success_hint"]
        return input_meta

    had_critical_action = False
    before_state = capture_runtime_state(device, device_id=device_index)
    input_events = []
    actual_operations = []
    for item in action_infos:
        action_type = item.get("action_type", ActionType.NOP)
        if action_type == ActionType.NOP:
            actual_operations.append({
                "type": "Nop",
                "device": item.get("device") or (f"Device{device_index}" if device_index else ""),
                "selection_reason": item.get("selection_reason", ""),
            })
            continue
        if action_type == ActionType.CLICK:
            had_critical_action = True
            bounds = item.get("bounds")
            if bounds and len(bounds) >= 4:
                controller.tap(bounds[:2], bounds[2:])
                actual_operations.append({
                    "type": "Tap",
                    "device": item.get("device") or (f"Device{device_index}" if device_index else ""),
                    "bounds": bounds,
                    "target_label": item.get("target_label") or item.get("text") or item.get("content_desc") or item.get("requested_target", ""),
                    "resource_id": item.get("resource_id", ""),
                    "class": item.get("class", ""),
                    "selection_reason": item.get("selection_reason", ""),
                })
            continue
        if action_type == ActionType.BACK:
            had_critical_action = True
            controller.back()
            actual_operations.append({
                "type": "Back",
                "device": item.get("device") or (f"Device{device_index}" if device_index else ""),
                "selection_reason": item.get("selection_reason", ""),
            })
            continue
        if action_type == ActionType.INPUT:
            had_critical_action = True
            bounds = item.get("bounds")
            text = item.get("text", "")
            input_meta = _input_with_fallback(bounds, text)
            input_events.append(input_meta)
            actual_operations.append({
                "type": "Input",
                "device": item.get("device") or (f"Device{device_index}" if device_index else ""),
                "bounds": bounds,
                "target_label": item.get("target_label") or item.get("target_element", ""),
                "resource_id": item.get("resource_id", ""),
                "class": item.get("class", ""),
                "text": text,
                "selection_reason": item.get("selection_reason", ""),
                "input_meta": input_meta,
            })
    after_state = capture_runtime_state(device, device_id=device_index) if had_critical_action else before_state
    return {
        "had_critical_action": had_critical_action,
        "before_state": before_state,
        "after_state": after_state,
        "changed": before_state["signature"] != after_state["signature"],
        "input_events": input_events,
        "actual_operations": actual_operations
    }


def _snapshot_matches_condition(snapshot, condition):
    expected = str(condition.get("expected", "")).strip().lower()
    if not expected:
        return False
    if condition.get("type") == "foreground_activity":
        return expected in str(snapshot.get("activity", "")).lower()
    if condition.get("type") == "visible":
        visible_text = " ".join((
            str(snapshot.get("state_ui_summary", "")),
            str(snapshot.get("all_comps", "")),
        )).lower()
        return expected in visible_text
    return False


def _task_terms(value):
    stopwords = {
        "device", "complete", "local", "objective", "observe", "verify",
        "the", "this", "that", "with", "from", "then", "task",
    }
    return {
        token for token in re.findall(r"[a-z0-9_\u4e00-\u9fff]{2,}", str(value or "").casefold())
        if token not in stopwords
    }


def _local_goal_matches(task_record, device_name, observed):
    observed = str(observed or "").casefold()
    goals = [
        item.get("goal", "") for item in task_record.get("device_goals", [])
        if isinstance(item, dict) and item.get("device") == device_name
    ]
    return any(term in observed for goal in goals for term in _task_terms(goal))


def _peer_progress(task_record, before, after, events=None):
    if not isinstance(task_record, dict):
        return []
    participants = set(task_record.get("participating_devices", []))
    conditions = task_record.get("verification_conditions", [])
    progress = []
    for device_name, after_state in after.items():
        before_state = before.get(device_name, {})
        if (
            device_name not in participants
            or before_state.get("signature") == after_state.get("signature")
        ):
            continue
        matched = [
            condition for condition in conditions
            if condition.get("device") == device_name
            and _snapshot_matches_condition(after_state, condition)
        ]
        state_text = " ".join(str(after_state.get(key, "")) for key in (
            "activity", "state_ui_summary", "all_comps"
        ))
        local_goal_matched = _local_goal_matches(task_record, device_name, state_text)
        if matched or local_goal_matched:
            progress.append({
                "device": device_name,
                "before_signature": before_state.get("signature", ""),
                "after_signature": after_state.get("signature", ""),
                "conditions": matched,
                "local_goal_matched": local_goal_matched,
            })
    for event in events or []:
        device_name = event.get("device")
        if device_name not in participants:
            continue
        event_text = event.get("value", "")
        matched_conditions = [
            condition for condition in conditions
            if condition.get("device") == device_name
            and condition.get("type") == "peer_event"
            and str(condition.get("expected", "")).casefold() in str(event_text).casefold()
        ]
        local_goal_matched = _local_goal_matches(task_record, device_name, event_text)
        if matched_conditions or local_goal_matched:
            progress.append({
                "device": device_name,
                "event": event,
                "conditions": matched_conditions,
                "local_goal_matched": local_goal_matched,
            })
    return progress


def _capture_peer_states(devices, active_device, task_record):
    if not devices or not isinstance(task_record, dict):
        return {}
    states = {}
    for device_name in task_record.get("participating_devices", []):
        peer_index = _device_number(device_name)
        peer = devices.get(f"d{peer_index}")
        if peer is None or peer_index == int(active_device):
            continue
        try:
            states[device_name] = capture_runtime_state(peer, device_id=peer_index)
        except Exception as exc:
            trace_writer.log_event(
                "peer_state_capture_failed", device=device_name, error=str(exc)
            )
    return states


def execute_action_infos_scheduled(pool, controller, device, action_infos, device_index,
                                   devices=None):
    task_record = pool.get_current_task_record()

    def execute():
        known_events = {event["event_id"] for event in pool.peer_event_history()}
        peer_before = _capture_peer_states(devices, device_index, task_record)
        result = execute_action_infos(
            controller, device, action_infos, device_index=device_index
        )
        peer_after = _capture_peer_states(devices, device_index, task_record)
        new_events = [
            event for event in pool.peer_event_history()
            if event["event_id"] not in known_events
        ]
        peer_progress = _peer_progress(task_record, peer_before, peer_after, new_events)
        result["active_changed"] = result["changed"]
        result["peer_before_states"] = peer_before
        result["peer_after_states"] = peer_after
        result["peer_progress"] = peer_progress
        result["changed"] = result["changed"] or bool(peer_progress)
        effect = next((item["expected_effect"] for item in action_infos
                       if item.get("expected_effect")), None)
        if pool.method.effect_feedback and effect and result["had_critical_action"]:
            states = {f"Device{device_index}": result["after_state"], **peer_after}
            state = states.get(effect["device"])
            if effect["type"] == "peer_event":
                supported = any(event["device"] == effect["device"] and _matches(
                    event["value"], effect["expected"], effect["operator"]
                ) for event in new_events)
                status = "supported" if supported else "unresolved"
            else:
                actual = _state_value(state or {}, effect["type"])
                supported = bool(actual) and _matches(
                    actual, effect["expected"], effect["operator"]
                )
                status = "supported" if supported else "unresolved"
            result["effect_status"] = status
            before_states = {f"Device{device_index}": result.get("before_state", {}), **peer_before}
            previous = _state_value(before_states.get(effect["device"], {}), effect["type"])
            already_present = bool(previous) and _matches(previous, effect["expected"], effect["operator"])
            result["changed"] = supported and (effect["type"] == "peer_event" or not already_present)
            pool.record_step_event("effect_feedback", condition=effect, status=status,
                                   observation=state, events=new_events)
        return result

    scheduler = pool.get_switch_scheduler() if hasattr(pool, "get_switch_scheduler") else None
    if scheduler is None:
        if pool.get_current_device() != int(device_index):
            return {
                "stale": True,
                "had_critical_action": False,
                "changed": False,
                "input_events": [],
                "actual_operations": [],
            }
        return execute()
    executed, result = scheduler.execute_if_current(
        pool,
        device_index,
        execute,
    )
    if executed:
        return result
    return {
        "stale": True,
        "had_critical_action": False,
        "changed": False,
        "input_events": [],
        "actual_operations": [],
    }


def _operation_action_label(operation):
    if not isinstance(operation, dict):
        return "action"
    action_type = str(operation.get("type", "action")).strip().lower()
    target = str(
        operation.get("target_label")
        or operation.get("resource_id")
        or operation.get("text")
        or ""
    ).strip().lower()
    return f"{action_type}:{target}".strip(":")


def record_execution_feedback(pool, coverage_store, execution_ledger, device_index, exec_meta):
    if not isinstance(exec_meta, dict):
        return
    operations = exec_meta.get("actual_operations", []) or []
    changed = bool(exec_meta.get("changed", False))
    active_changed = bool(exec_meta.get("active_changed", changed))
    if execution_ledger is not None:
        execution_ledger.record_actions(operations, changed=changed)
    before_state = exec_meta.get("before_state", {}) or {}
    after_state = exec_meta.get("after_state", {}) or {}
    for operation in operations:
        action_label = _operation_action_label(operation)
        if str(operation.get("type", "")).casefold() != "nop":
            target = str(
                operation.get("target_label") or operation.get("resource_id") or ""
            ).strip()
            target_text = f" [{target}]" if target else ""
            print(
                f"[Action] Device{device_index}: {operation.get('type', 'Action')}"
                f"{target_text} (changed={str(changed).lower()})"
            )
        pool.record_step_event(
            "action_execution",
            device=f"Device{device_index}",
            action=action_label,
            before_state=before_state.get("signature", ""),
            after_state=after_state.get("signature", ""),
            changed=changed,
            operation=operation,
        )
        if coverage_store is not None:
            coverage_store.record_observation(
                device_index,
                before_state.get("signature", ""),
                before_state.get("state_ui_summary", before_state.get("all_comps", "")),
                "",
                activity=before_state.get("activity", ""),
                package_name=before_state.get("package", ""),
            )
            coverage_store.record_observation(
                device_index,
                after_state.get("signature", ""),
                after_state.get("state_ui_summary", after_state.get("all_comps", "")),
                before_state.get("signature", "") if active_changed else "",
                activity=after_state.get("activity", ""),
                package_name=after_state.get("package", ""),
                action_label=action_label if active_changed else None,
                action_outcome={"changed": active_changed},
            )
            if not active_changed:
                coverage_store.record_action_attempt(
                    before_state.get("signature", ""),
                    action_label,
                    outcome={"changed": False, "peer_progress": bool(exec_meta.get("peer_progress"))},
                )

    if coverage_store is None:
        return
    peer_before = exec_meta.get("peer_before_states", {})
    peer_after = exec_meta.get("peer_after_states", {})
    changed_peers = {
        item.get("device") for item in exec_meta.get("peer_progress", [])
        if item.get("before_signature") and item.get("after_signature")
    }
    for device_name in changed_peers:
        before = peer_before.get(device_name, {})
        after = peer_after.get(device_name, {})
        peer_index = _device_number(device_name)
        for state in (before, after):
            coverage_store.record_observation(
                peer_index,
                state.get("signature", ""),
                state.get("state_ui_summary", state.get("all_comps", "")),
                "",
                activity=state.get("activity", ""),
                package_name=state.get("package", ""),
            )
        coverage_store.exploration.record_transition(
            before.get("signature", ""),
            "peer_effect",
            after.get("signature", ""),
            peer_index,
            outcome={"changed": True, "source_device": device_index},
            replayable=False,
        )


def record_context_coverage(coverage_store: CoverageStore, contexts: list):
    for ctx in contexts:
        device_index = ctx.get("device_index")
        if device_index is None:
            continue
        activity = ctx.get("activity", "")
        package_name = ctx.get("package", "")
        state_components = ctx.get("state_ui_summary", ctx.get("ui_summary", ""))
        stable_signature = state_signature(
            activity,
            state_components,
            package_name=package_name,
            device_id=device_index,
        )
        ctx["signature"] = coverage_store.record_observation(
            device_index,
            stable_signature,
            state_components,
            "",
            activity=activity,
            package_name=package_name
        )
