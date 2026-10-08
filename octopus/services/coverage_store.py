from collections import defaultdict
import hashlib
import json
import re
import threading

from octopus.services.memory_store import ExplorationMemory


_DYNAMIC_PATTERNS = (
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE), "<time>"),
    (re.compile(r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b"), "<date>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE), "<id>"),
    (re.compile(r"\b(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)[a-z0-9_-]{16,}\b", re.IGNORECASE), "<id>"),
    (re.compile(r"\b\d{4,}\b"), "<num>"),
    (re.compile(r"(?<!\w)\d+\s+(?:unread|new|items?|messages?|notifications?)(?!\w)", re.IGNORECASE), "<count>"),
)


def _normalize_dynamic_text(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for pattern, replacement in _DYNAMIC_PATTERNS:
        text = pattern.sub(replacement, text)
    # Standalone counters are screen noise; short digits embedded in labels remain meaningful.
    if re.fullmatch(r"\d+", text):
        return "<count>"
    return text


def _resource_tail(value):
    value = str(value or "").strip()
    return value.split(":id/")[-1].split("/")[-1]


class CoverageStore:
    def __init__(self, exploration_memory=None):
        self._lock = threading.RLock()
        self.activities = defaultdict(set)
        self.known_activities = set()
        self.code_units = defaultdict(set)
        self.known_code_units = set()
        self.exploration = exploration_memory or ExplorationMemory()

    @staticmethod
    def _device_key(device_id):
        return str(device_id)

    @staticmethod
    def _normalize_activity(activity: str, package_name: str = ""):
        activity = str(activity or "").strip()
        package_name = str(package_name or "").strip()
        if not activity or activity == "unknown":
            return ""
        if activity.startswith(".") and package_name and package_name != "unknown":
            return f"{package_name}{activity}"
        return activity

    @staticmethod
    def _normalize_code_unit(unit: str):
        unit = re.sub(r"\s+", " ", str(unit or "").strip())
        if not unit or unit.lower() == "unknown":
            return ""
        return unit

    @staticmethod
    def normalize_controls(all_comps):
        """Return a stable, bounds-free set of visible/actionable control descriptors."""
        if isinstance(all_comps, list):
            raw_items = all_comps
        elif isinstance(all_comps, dict):
            raw_items = [all_comps]
        else:
            raw_items = []
            for raw_line in str(all_comps or "").splitlines():
                line = re.sub(r"\bbounds\s*[:=]\s*['\"]?\[[^\]]+\]['\"]?", "", raw_line, flags=re.IGNORECASE)
                fields = {}
                for key, value in re.findall(
                    r"(label|content_desc|content-desc|resource_id|resource-id|class|id|text)\s*:\s*['\"]([^'\"]*)['\"]",
                    line,
                    flags=re.IGNORECASE,
                ):
                    fields[key.lower().replace("-", "_")] = value
                for key, value in re.findall(
                    r"(clickable|editable|enabled|focusable|long-clickable)\s*=\s*(true|false)",
                    line,
                    flags=re.IGNORECASE,
                ):
                    fields[key.lower().replace("-", "_")] = value.lower() == "true"
                if fields:
                    raw_items.append(fields)

        normalized = []
        seen = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            def value(*names):
                for name in names:
                    if name in item:
                        return item.get(name)
                    if f"@{name}" in item:
                        return item.get(f"@{name}")
                return ""

            text = _normalize_dynamic_text(value("label", "text", "id"))
            content_desc = _normalize_dynamic_text(value("content_desc", "content-desc"))
            resource_id = _resource_tail(value("resource_id", "resource-id"))
            class_name = str(value("class") or "").strip().split(".")[-1]
            clickable = str(value("clickable")).lower() == "true" if not isinstance(value("clickable"), bool) else bool(value("clickable"))
            editable_value = value("editable")
            editable = (
                bool(editable_value) if isinstance(editable_value, bool)
                else str(editable_value).lower() == "true"
            ) or class_name.endswith("EditText")
            enabled_value = value("enabled")
            enabled = True if enabled_value == "" else (
                bool(enabled_value) if isinstance(enabled_value, bool)
                else str(enabled_value).lower() == "true"
            )
            if not any((text, content_desc, resource_id, clickable, editable)):
                continue
            control = {
                "text": text,
                "content_desc": content_desc,
                "resource_id": resource_id,
                "class": class_name,
                "clickable": clickable,
                "editable": editable,
                "enabled": enabled,
            }
            key = json.dumps(control, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                normalized.append(control)
        normalized.sort(key=lambda item: (
            item["resource_id"], item["text"], item["content_desc"], item["class"],
            item["clickable"], item["editable"],
        ))
        return normalized

    @classmethod
    def available_actions(cls, controls):
        actions = []
        for control in controls or []:
            target = (
                control.get("text")
                or control.get("content_desc")
                or control.get("resource_id")
                or control.get("class")
            )
            if not target or not control.get("enabled", True):
                continue
            if control.get("clickable"):
                actions.append(f"tap:{str(target).strip().lower()}")
            if control.get("editable"):
                actions.append(f"input:{str(target).strip().lower()}")
        return sorted(set(actions))

    @classmethod
    def build_state_signature(cls, device_id, package_name, activity, all_comps):
        controls = cls.normalize_controls(all_comps)
        payload = {
            "device": str(device_id or ""),
            "package": str(package_name or "").strip().lower(),
            "activity": cls._normalize_activity(activity, package_name).lower(),
            "controls": controls,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return digest

    def set_known_activities(self, activities):
        normalized_values = []
        with self._lock:
            for activity in activities or []:
                normalized = self._normalize_activity(activity)
                if normalized:
                    self.known_activities.add(normalized)
                    normalized_values.append(normalized)
        self.exploration.update_coverage("known_activities", normalized_values)

    def set_known_code_units(self, units):
        normalized_values = []
        with self._lock:
            for unit in units or []:
                normalized = self._normalize_code_unit(unit)
                if normalized:
                    self.known_code_units.add(normalized)
                    normalized_values.append(normalized)
        self.exploration.update_coverage("known_code_units", normalized_values)

    def record_activity(self, device_id, activity: str, package_name: str = ""):
        activity_key = self._normalize_activity(activity, package_name)
        if activity_key:
            with self._lock:
                self.activities[self._device_key(device_id)].add(activity_key)
            self.exploration.update_coverage("visited_activities", [activity_key])

    def record_code_unit(self, device_id, unit: str):
        unit_key = self._normalize_code_unit(unit)
        if unit_key:
            with self._lock:
                self.code_units[self._device_key(device_id)].add(unit_key)
            self.exploration.update_coverage("visited_code_units", [unit_key])

    def record_code_units(self, device_id, units):
        normalized = []
        with self._lock:
            for unit in units or []:
                unit_key = self._normalize_code_unit(unit)
                if unit_key:
                    self.code_units[self._device_key(device_id)].add(unit_key)
                    normalized.append(unit_key)
        self.exploration.update_coverage("visited_code_units", normalized)

    def record_action_attempt(self, state_signature, action, outcome=None):
        return self.exploration.record_action_attempt(state_signature, action, outcome=outcome)

    def record_observation(self, device_id, signature: str, all_comps: str, previous_signature: str = "",
                           activity: str = "", package_name: str = "", action_label=None,
                           action_outcome=None, coverage_feedback=None):
        self.record_activity(device_id, activity, package_name)
        controls = self.normalize_controls(all_comps)
        stable_signature = self.build_state_signature(device_id, package_name, activity, all_comps)
        # Compatibility: callers may provide a precomputed stable signature. Reject old ad-hoc strings.
        if re.fullmatch(r"[0-9a-f]{64}", str(signature or "")):
            stable_signature = str(signature)
        self.exploration.record_state(
            stable_signature,
            device_id,
            package_name,
            self._normalize_activity(activity, package_name),
            controls,
            self.available_actions(controls),
            coverage_feedback=coverage_feedback,
        )
        if previous_signature:
            self.exploration.record_transition(
                previous_signature,
                action_label or "observe",
                stable_signature,
                device_id,
                outcome=action_outcome,
            )
        return stable_signature

    def snapshot(self, include_exploration=True):
        with self._lock:
            snapshot = {
                "activities": {key: set(value) for key, value in self.activities.items()},
                "known_activities": set(self.known_activities),
                "code_units": {key: set(value) for key, value in self.code_units.items()},
                "known_code_units": set(self.known_code_units),
            }
        if include_exploration:
            snapshot["exploration"] = self.exploration.snapshot()
        return snapshot


class CoverageMetrics:
    @staticmethod
    def _safe_union(value_sets):
        items = [item for item in value_sets if item]
        if not items:
            return set()
        return set().union(*items)

    @staticmethod
    def delta(before, after):
        before = before or {}
        after = after or {}
        result = {}
        for key in set(before) | set(after):
            before_item = before.get(key, {}) if isinstance(before.get(key, {}), dict) else {}
            after_item = after.get(key, {}) if isinstance(after.get(key, {}), dict) else {}
            row = {}
            for metric in ("activity_cov", "code_cov", "num_activities", "num_code_units"):
                row[metric] = (after_item.get(metric) or 0) - (before_item.get(metric) or 0)
            result[key] = row
        return result

    @staticmethod
    def _classes(units):
        return {match.group(1) for unit in units
                if (match := re.match(r"^<([^:]+):.*>$", unit))}

    def compute(self, store):
        snapshot = store.snapshot(include_exploration=False)
        activities = snapshot["activities"]
        units = snapshot["code_units"]
        known_activities = snapshot["known_activities"]
        known_methods = snapshot["known_code_units"]
        known_classes = self._classes(known_methods)
        result = {}
        scopes = {"__overall__": (self._safe_union(activities.values()), self._safe_union(units.values()))}
        scopes.update({key: (activities.get(key, set()), units.get(key, set()))
                       for key in sorted(set(activities) | set(units), key=str)})
        for key, (observed_activities, observed_methods) in scopes.items():
            hit_methods = observed_methods & known_methods
            hit_classes = self._classes(hit_methods)
            result[key] = {
                "activity_cov": len(observed_activities & known_activities) / len(known_activities)
                                if known_activities else None,
                "activity_coverage_denominator_known": bool(known_activities),
                "code_cov": len(hit_methods) / len(known_methods) if known_methods else 0.0,
                "method_cov": len(hit_methods) / len(known_methods) if known_methods else None,
                "class_cov": len(hit_classes) / len(known_classes) if known_classes else None,
                "code_coverage_denominator_known": bool(known_methods),
                "num_activities": len(observed_activities),
                "covered_activities": len(observed_activities & known_activities),
                "known_activities": len(known_activities),
                "num_code_units": len(observed_methods),
                "covered_methods": len(hit_methods),
                "known_code_units": len(known_methods),
                "covered_classes": len(hit_classes),
                "known_classes": len(known_classes),
                "out_of_scope_activities": len(observed_activities - known_activities),
                "out_of_scope_methods": len(observed_methods - known_methods),
            }
        return result
