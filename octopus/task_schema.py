import json
import re
from copy import deepcopy


CONDITION_TYPES = {"foreground_activity", "visible", "peer_event"}
CONDITION_ALIASES = {
    "activity": "foreground_activity",
    "text": "visible",
    "control": "visible",
    "event": "peer_event",
}
VOLATILE_FINGERPRINTS = (
    (r"\bdevice\s*\d+\b", "device"),
    (r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "account"),
    (r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "generated-id"),
    (r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", "time"),
    (r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", "date"),
    (r"\b(?:user|account|contact)[-_ ]?\d+\b", "account"),
    (r"\b\d{5,}\b", "generated-value"),
)


def extract_json_object(raw_text):
    text = str(raw_text or "").strip()
    for candidate in (text, *(re.findall(r"\{[\s\S]*\}|\[[\s\S]*\]", text)[:1])):
        try:
            value = json.loads(candidate)
            if isinstance(value, (dict, list)):
                return value
        except (TypeError, ValueError):
            pass
    return None


def _aliases(devices):
    result = {}
    for index, device_id in enumerate(devices or [], 1):
        canonical = f"Device{index}"
        for alias in (index, canonical, f"device {index}", device_id):
            result[str(alias).strip().casefold()] = canonical
    return result


def canonical_device(value, devices):
    return _aliases(devices).get(str(value or "").strip().casefold(), "")


def _items(value):
    return value if isinstance(value, list) else []


def task_devices(task, devices):
    if not isinstance(task, dict):
        return []
    values = list(_items(task.get("participating_devices")))
    values.extend(
        goal.get("device") for goal in _items(task.get("device_goals"))
        if isinstance(goal, dict)
    )
    values.append(task.get("first_device", task.get("start_device")))
    result = []
    for value in values:
        device = canonical_device(value, devices)
        if device and device not in result:
            result.append(device)
    return result


def normalize_verification_condition(condition, devices):
    if not isinstance(condition, dict):
        return None
    device = canonical_device(condition.get("device"), devices)
    condition_type = str(
        condition.get("type", condition.get("observation_type", ""))
    ).strip().casefold().replace(" ", "_")
    condition_type = CONDITION_ALIASES.get(condition_type, condition_type)
    expected = str(condition.get("expected", condition.get("value", "")) or "").strip()
    if not device or condition_type not in CONDITION_TYPES or not expected:
        return None
    return {
        "device": device,
        "type": condition_type,
        "expected": expected,
        "operator": str(condition.get("operator", "contains")).strip().casefold(),
        "required": condition.get("required", True) is not False,
    }


def normalize_task(task, devices=None, task_index=1):
    devices = devices or []
    source = deepcopy(task) if isinstance(task, dict) else {"goal": str(task or "")}
    goal = str(source.get("goal", source.get("description", "")) or "").strip()
    participants = task_devices(source, devices)
    first_device = canonical_device(
        source.get("first_device", source.get("start_device")), devices
    )
    device_goals = []
    for item in _items(source.get("device_goals")):
        if not isinstance(item, dict):
            continue
        device = canonical_device(item.get("device"), devices)
        local_goal = str(item.get("goal", "") or "").strip()
        if device and local_goal:
            device_goals.append({"device": device, "goal": local_goal})
    grounding = []
    for item in _items(source.get("grounding_information", source.get("grounding"))):
        if not isinstance(item, dict):
            grounding.append(deepcopy(item))
            continue
        normalized_item = deepcopy(item)
        normalized_item["device"] = canonical_device(item.get("device"), devices)
        grounding.append(normalized_item)
    conditions = [
        condition for condition in (
            normalize_verification_condition(item, devices)
            for item in _items(source.get("verification_conditions"))
        ) if condition
    ]
    return {
        "task_id": str(source.get("task_id") or f"task_{task_index}"),
        "goal": goal,
        "description": goal,
        "participating_devices": participants,
        "first_device": first_device,
        "start_device": first_device,
        "device_goals": device_goals,
        "grounding_information": grounding,
        "verification_conditions": conditions,
        "is_multi_device": len(participants) > 1,
    }


def parse_generated_tasks(raw_text, devices=None):
    del devices
    payload = extract_json_object(raw_text)
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        raw_tasks = payload["tasks"]
    elif isinstance(payload, dict):
        raw_tasks = [payload]
    elif isinstance(payload, list):
        raw_tasks = payload
    else:
        return []
    return [deepcopy(item) for item in raw_tasks[:5] if isinstance(item, dict)]


def validate_task(task, devices):
    normalized = normalize_task(task, devices)
    errors = []
    if not isinstance(task, dict):
        errors.append("task_not_object")
        task = {}
    if not normalized["goal"]:
        errors.append("missing_goal")
    raw_participants = _items(task.get("participating_devices"))
    if not raw_participants:
        errors.append("missing_participating_devices")
    elif len(normalized["participating_devices"]) != len(set(map(str, raw_participants))):
        errors.append("unknown_or_duplicate_participating_device")
    if not normalized["first_device"]:
        errors.append("unknown_first_device")
    elif normalized["first_device"] not in normalized["participating_devices"]:
        errors.append("first_device_not_participating")

    local_devices = [item["device"] for item in normalized["device_goals"]]
    if set(local_devices) != set(normalized["participating_devices"]):
        errors.append("missing_device_goal")
    if len(local_devices) != len(set(local_devices)):
        errors.append("duplicate_device_goal")

    grounding = normalized["grounding_information"]
    if not grounding:
        errors.append("missing_grounding_information")
    for index, item in enumerate(grounding, 1):
        if not isinstance(item, dict):
            errors.append(f"grounding_{index}_not_object")
            continue
        device = canonical_device(item.get("device"), devices)
        if device not in normalized["participating_devices"]:
            errors.append(f"grounding_{index}_device_not_participating")
        if not str(item.get("control", item.get("state_signature", "")) or "").strip():
            errors.append(f"grounding_{index}_missing_reference")

    raw_conditions = _items(task.get("verification_conditions"))
    if not raw_conditions or len(normalized["verification_conditions"]) != len(raw_conditions):
        errors.append("invalid_verification_conditions")
    for index, condition in enumerate(normalized["verification_conditions"], 1):
        if condition["device"] not in normalized["participating_devices"]:
            errors.append(f"verification_{index}_device_not_participating")
    if _items(task.get("actions")):
        errors.append("preplanned_actions_not_allowed")
    return {"task": normalized, "valid": not errors, "errors": errors}


def task_description(task):
    if isinstance(task, dict):
        return str(task.get("goal", task.get("description", "")) or "").strip()
    return str(task or "").strip()


def task_fingerprint(task):
    if isinstance(task, dict):
        values = [task_description(task)]
        values.extend(
            str(item.get("goal", "")) for item in _items(task.get("device_goals"))
            if isinstance(item, dict)
        )
        text = " ".join(dict.fromkeys(value for value in values if value))
    else:
        text = task_description(task)
    for pattern, replacement in VOLATILE_FINGERPRINTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text.casefold()).strip()


def text_similarity(left, right):
    """Token cosine: a small, dependency-free approximation of semantic similarity."""
    def tokens(text):
        return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", str(text).casefold()))
    left, right = tokens(left), tokens(right)
    return len(left & right) / (len(left) * len(right)) ** 0.5 if left and right else 0.0


def planning_ui(contexts):
    return "\n".join(
        f"Device{ctx.get('device_index', '')}: {ctx.get('ui_summary', '')}"
        for ctx in contexts or []
    )


def rank_task_candidates(candidates, task_records=None, *, contexts=None, method=None):
    from octopus.experiment_config import MethodOptions

    method = method or MethodOptions()
    current_ui = planning_ui(contexts)
    history = [record for record in task_records or []
               if record.get("result") in {"success", "failure", "unknown"}]
    ranked, seen = [], set()
    for source in candidates or []:
        candidate = deepcopy(source)
        fingerprint = task_fingerprint(candidate)
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        device = candidate.get("first_device")
        local_ui = "\n".join(ctx.get("ui_summary", "") for ctx in contexts or []
                             if f"Device{ctx.get('device_index')}" == device)
        controls = " ".join(item.get("control", "") for item in candidate.get("grounding_information", [])
                            if item.get("device") == device)
        relevance = max(text_similarity(task_description(candidate), local_ui),
                        text_similarity(controls, local_ui))
        repetition = max((
            text_similarity(current_ui, record.get("task_record", {}).get("source_ui", ""))
            * text_similarity(fingerprint, task_fingerprint(record.get("task_record", {})))
            for record in history
        ), default=0.0)
        score = (relevance if method.relevance else 0) - (repetition if method.diversity else 0)
        candidate.update(fingerprint=fingerprint, source_ui=current_ui, selection_score=score,
                         selection_reason=f"relevance={relevance:.3f}, repetition={repetition:.3f}")
        ranked.append(candidate)
    # Stable ties retain generation order, including when both ranking terms are ablated.
    return sorted(ranked, key=lambda task: -task["selection_score"])
