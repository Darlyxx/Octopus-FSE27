import re
from time import sleep, time


def _device_name(value):
    match = re.search(r"\d+", str(value or ""))
    return f"Device{int(match.group())}" if match else str(value or "").strip()


def _snapshots(value):
    if isinstance(value, list):
        if value and all(isinstance(item, dict) and "devices" in item for item in value):
            return value
        return [{"devices": value}]
    return [value] if isinstance(value, dict) else []


def _devices(snapshot):
    values = snapshot.get("devices", snapshot) if isinstance(snapshot, dict) else {}
    if isinstance(values, dict):
        return {_device_name(key): state for key, state in values.items()}
    return {
        _device_name(item.get("device", item.get("device_index"))): item
        for item in values if isinstance(item, dict)
    }


def _state_value(state, condition_type):
    if condition_type == "foreground_activity":
        return str(state.get("activity", ""))
    return " ".join(str(state.get(key, "") or "") for key in (
        "ui_summary", "state_ui_summary", "all_comps", "observation"
    )).strip()


def _matches(actual, expected, operator):
    actual = str(actual or "").casefold()
    expected = str(expected or "").casefold()
    if operator == "equals":
        return actual == expected or actual.rsplit(".", 1)[-1] == expected.rsplit(".", 1)[-1]
    if operator == "absent":
        return expected not in actual
    return expected in actual


def _matching_event(condition, evidence):
    device = _device_name(condition.get("device"))
    expected = str(condition.get("expected", "")).casefold()
    for event in evidence:
        if not isinstance(event, dict):
            continue
        if str(event.get("status", "")).casefold() not in {"pending", "claimed", "consumed", "expired"}:
            continue
        if _device_name(event.get("device")) != device:
            continue
        if expected in str(event.get("value", "")).casefold():
            return event
    return None


class TaskResultVerifier:
    FAILURE_REASONS = {
        "task_timeout_failure": "timeout",
        "run_timeout_failure": "timeout",
        "repeated_action_failure": "repeated_action",
        "action_budget_exhausted": "budget",
        "llm_call_budget_exhausted": "budget",
        "token_budget_exhausted": "budget",
        "infeasible": "infeasible",
        "grounding_failure": "grounding",
        "no_progress": "no_progress",
    }

    def verify(self, task_record, terminal_global_state, evidence=None,
               termination_reason="", operator_end_requested=False, *,
               observe=None, max_rounds=0, deadline=None):
        snapshots = _snapshots(terminal_global_state)
        rounds = 0
        while True:
            result = self._verify(task_record, snapshots, evidence, termination_reason,
                                  operator_end_requested)
            result["observation_rounds"] = rounds
            if (not observe or rounds >= max_rounds or result["result"] == "success"
                    or termination_reason in self.FAILURE_REASONS or not isinstance(task_record, dict)):
                return result
            satisfied = [item["condition"] for item in result["evidence"]
                         if "condition" in item and ("event" in item or all(item.get("matches", [False])))]
            pending = [condition for condition in task_record.get("verification_conditions", [])
                       if condition.get("required", True) and condition not in satisfied]
            if not pending or (deadline is not None and time() + 0.2 >= deadline):
                return result
            reference = task_record.get("first_device")
            pending.sort(key=lambda condition: condition.get("device") != reference)
            sleep(0.2)
            snapshots.append(observe(pending[0]["device"]))
            rounds += 1

    def _verify(self, task_record, terminal_global_state, evidence=None,
               termination_reason="", operator_end_requested=False):
        evidence = list(evidence or [])
        category = self.FAILURE_REASONS.get(str(termination_reason or ""))
        if category:
            return self._result("failure", termination_reason, category, evidence)
        if not isinstance(task_record, dict):
            return self._result(
                "unknown", "missing_structured_task_record", "verification_unknown", evidence
            )

        conditions = [
            condition for condition in task_record.get("verification_conditions", [])
            if isinstance(condition, dict) and condition.get("required", True) is not False
        ]
        if not conditions:
            return self._result(
                "unknown", "missing_verification_conditions", "verification_unknown", evidence
            )

        snapshots = [_devices(item) for item in _snapshots(terminal_global_state)]
        verified = []
        for condition in conditions:
            condition_type = str(condition.get("type", "")).casefold()
            device = _device_name(condition.get("device"))
            if not condition.get("expected") or not device:
                return self._result("unknown", "invalid_verification_condition",
                                    "verification_unknown", verified)
            if condition_type == "peer_event":
                event = _matching_event(condition, evidence)
                if not event:
                    return self._result(
                        "unknown", f"peer_event_not_observed_on:{device}",
                        "verification_unknown", verified,
                    )
                verified.append({"condition": condition, "event": event})
                continue
            if condition_type not in {"foreground_activity", "visible"}:
                return self._result(
                    "unknown", f"unsupported_verification_type:{condition_type}",
                    "verification_unknown", verified,
                )
            available = [snapshot for snapshot in snapshots if device in snapshot]
            if len(snapshots) < 2:
                return self._result(
                    "unknown", "state_condition_requires_two_consecutive_snapshots",
                    "verification_unknown", verified,
                )
            last_two = available[-2:]
            if len(last_two) < 2:
                return self._result(
                    "unknown", f"missing_terminal_state_for:{device}",
                    "missing_device_evidence", verified,
                )
            if any(snapshot[device].get("context_collection_error") or not
                   _state_value(snapshot[device], condition_type) for snapshot in last_two):
                return self._result(
                    "unknown", f"missing_terminal_state_for:{device}",
                    "missing_device_evidence", verified,
                )
            matches = [
                _matches(
                    _state_value(snapshot[device], condition_type),
                    condition.get("expected", ""),
                    str(condition.get("operator", "contains")).casefold(),
                )
                for snapshot in last_two
            ]
            if not all(matches):
                return self._result(
                    "unknown" if any(matches) else "failure",
                    f"verification_condition_not_established_on:{device}",
                    "verification_unknown" if any(matches) else "success_criteria_not_met",
                    verified + [{"condition": condition, "matches": matches}],
                )
            verified.append({"condition": condition, "matches": matches})

        result = self._result(
            "success", "all_structured_verification_conditions_satisfied", "", verified
        )
        result["operator_end_requested"] = bool(operator_end_requested)
        return result

    @staticmethod
    def _result(result, reason, category, evidence):
        return {
            "result": result,
            "reason": str(reason or ""),
            "failure_category": category,
            "evidence": evidence,
        }
