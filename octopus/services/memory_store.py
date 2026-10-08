import copy
import threading
from collections import defaultdict
from datetime import datetime
from time import time


def _now():
    return datetime.now().isoformat(timespec="milliseconds")


class StepMemory:
    """Task-scoped execution evidence. This is the only memory cleared per episode."""

    def __init__(self):
        self.task_id = ""
        self.records = []

    def begin(self, task_id=""):
        self.task_id = str(task_id or "")
        self.records = []

    def add(self, event_type, **payload):
        record = {"ts": _now(), "event": str(event_type or "step")}
        record.update(copy.deepcopy(payload))
        self.records.append(record)
        return record

    def clear(self):
        self.task_id = ""
        self.records = []

    def snapshot(self):
        return {"task_id": self.task_id, "records": copy.deepcopy(self.records)}


class ExplorationMemory:
    """Campaign-scoped UI graph, frontier, attempts, paths, and coverage feedback."""

    def __init__(self):
        self._lock = threading.RLock()
        self.ui_states = {}
        self.transitions = {}
        self.action_attempts = defaultdict(lambda: defaultdict(int))
        self.known_paths = {}
        self.peer_events = []
        self._next_event_id = 1
        self.coverage_feedback = {
            "known_activities": set(),
            "visited_activities": set(),
            "known_code_units": set(),
            "visited_code_units": set(),
        }

    @staticmethod
    def _action_key(action):
        if isinstance(action, dict):
            action_type = str(action.get("type") or action.get("action_type") or "action").strip().lower()
            target = str(
                action.get("target_label")
                or action.get("label")
                or action.get("resource_id")
                or action.get("target")
                or ""
            ).strip().lower()
            return f"{action_type}:{target}".strip(":")
        return str(action or "").strip().lower()

    def record_state(self, signature, device, package_name, activity, controls, available_actions,
                     coverage_feedback=None):
        with self._lock:
            signature = str(signature or "").strip()
            if not signature:
                return None
            node = self.ui_states.get(signature)
            if node is None:
                node = {
                    "signature": signature,
                    "device": str(device),
                    "package": str(package_name or ""),
                    "activity": str(activity or ""),
                    "controls": copy.deepcopy(controls or []),
                    "available_actions": sorted(set(available_actions or [])),
                    "visit_count": 0,
                    "first_seen": _now(),
                    "last_seen": "",
                    "coverage_feedback": {},
                }
                self.ui_states[signature] = node
                self.known_paths.setdefault(signature, [])
            else:
                node["controls"] = copy.deepcopy(controls or node.get("controls", []))
                node["available_actions"] = sorted(
                    set(node.get("available_actions", [])) | set(available_actions or [])
                )
            node["visit_count"] += 1
            node["last_seen"] = _now()
            if coverage_feedback:
                node["coverage_feedback"].update(copy.deepcopy(coverage_feedback))
            return copy.deepcopy(node)

    def record_action_attempt(self, state_signature, action, outcome=None):
        with self._lock:
            state_signature = str(state_signature or "").strip()
            action_key = self._action_key(action)
            if not state_signature or not action_key:
                return 0
            self.action_attempts[state_signature][action_key] += 1
            node = self.ui_states.get(state_signature)
            if node is not None:
                node.setdefault("action_outcomes", {})[action_key] = copy.deepcopy(outcome or {})
            return self.action_attempts[state_signature][action_key]

    def record_transition(self, previous_signature, action, next_signature, device, outcome=None,
                          replayable=None):
        with self._lock:
            previous_signature = str(previous_signature or "").strip()
            next_signature = str(next_signature or "").strip()
            action_key = self._action_key(action) or "observe"
            if not previous_signature or not next_signature:
                return None
            self.record_action_attempt(previous_signature, action_key, outcome=outcome)
            key = f"{previous_signature}|{action_key}|{next_signature}|{device}"
            transition = self.transitions.get(key)
            if transition is None:
                transition = {
                    "previous_state": previous_signature,
                    "action": action_key,
                    "next_state": next_signature,
                    "device": str(device),
                    "attempt_count": 0,
                    "first_seen": _now(),
                    "last_seen": "",
                    "outcomes": [],
                }
                self.transitions[key] = transition
            transition["attempt_count"] += 1
            transition["last_seen"] = _now()
            if outcome:
                transition["outcomes"].append(copy.deepcopy(outcome))
                transition["outcomes"] = transition["outcomes"][-10:]

            if replayable is None:
                replayable = action_key.split(":", 1)[0] in {"tap", "input", "back"}
            transition["replayable"] = bool(replayable)

            previous_node = self.ui_states.get(previous_signature, {})
            next_node = self.ui_states.get(next_signature, {})
            same_device = str(previous_node.get("device", device)) == str(
                next_node.get("device", device)
            ) == str(device)
            previous_path = self.known_paths.get(previous_signature)
            next_path = self.known_paths.get(next_signature)
            if replayable and same_device and previous_path is not None:
                candidate_path = copy.deepcopy(previous_path) + [{
                    "state": previous_signature,
                    "action": action_key,
                    "device": str(device),
                    "next_state": next_signature,
                }]
                if next_path is None or not next_path or len(candidate_path) < len(next_path):
                    self.known_paths[next_signature] = candidate_path
            return copy.deepcopy(transition)

    def record_peer_event(self, device, event_type, value, package_name="", ttl_seconds=30):
        """Record a peer effect once; lifecycle changes are serialized by this lock."""
        with self._lock:
            now = time()
            event = {
                "event_id": f"event_{self._next_event_id}",
                "device": str(device),
                "type": str(event_type or "peer_event").strip().lower(),
                "value": str(value or "").strip(),
                "package": str(package_name or "").strip(),
                "status": "pending",
                "created_at": now,
                "expires_at": now + max(0.1, float(ttl_seconds or 30)),
            }
            self._next_event_id += 1
            self.peer_events.append(event)
            return copy.deepcopy(event)

    def _expire_events(self, now=None):
        now = time() if now is None else float(now)
        for event in self.peer_events:
            if event["status"] == "pending" and event["expires_at"] <= now:
                event["status"] = "expired"
                event["expired_at"] = now

    @staticmethod
    def _event_matches(event, device="", event_type="", expected="", package_name=""):
        if device and str(event.get("device")) != str(device):
            return False
        if package_name and str(event.get("package")) != str(package_name):
            return False
        if event_type and str(event.get("type")) != str(event_type).strip().lower():
            return False
        return not expected or str(expected).lower() in str(event.get("value", "")).lower()

    def pending_events(self, **match):
        with self._lock:
            self._expire_events()
            return [
                copy.deepcopy(event) for event in self.peer_events
                if event["status"] == "pending" and self._event_matches(event, **match)
            ]

    def claim_event(self, task_id, **match):
        """Atomically claim the earliest eligible event for one task or handoff."""
        with self._lock:
            self._expire_events()
            for event in self.peer_events:
                if event["status"] != "pending" or not self._event_matches(event, **match):
                    continue
                event.update(status="claimed", claimed_by=str(task_id), claimed_at=time())
                return copy.deepcopy(event)
            return None

    def consume_event(self, event_id):
        with self._lock:
            for event in self.peer_events:
                if event["event_id"] == str(event_id) and event["status"] == "claimed":
                    event.update(status="consumed", consumed_at=time())
                    return copy.deepcopy(event)
            return None

    def event_history(self):
        with self._lock:
            self._expire_events()
            return copy.deepcopy(self.peer_events)

    def update_coverage(self, category, values):
        with self._lock:
            target = self.coverage_feedback.get(str(category))
            if target is None:
                return
            target.update(str(value) for value in (values or []) if str(value or "").strip())

    def frontier(self, limit=20):
        with self._lock:
            rows = []
            for signature, node in self.ui_states.items():
                attempted = self.action_attempts.get(signature, {})
                untried = [
                    action for action in node.get("available_actions", [])
                    if int(attempted.get(self._action_key(action), 0)) == 0
                ]
                if not untried:
                    continue
                rows.append({
                    "signature": signature,
                    "device": node.get("device", ""),
                    "package": node.get("package", ""),
                    "activity": node.get("activity", ""),
                    "visit_count": int(node.get("visit_count", 0)),
                    "untried_actions": untried,
                    "known_path": copy.deepcopy(self.known_paths.get(signature, [])),
                })
            rows.sort(key=lambda item: (item["visit_count"], item["signature"]))
            return rows[:max(0, int(limit))]

    def no_progress_actions(self, limit=20):
        with self._lock:
            rows = []
            for signature, node in self.ui_states.items():
                for action, count in self.action_attempts.get(signature, {}).items():
                    outcome = node.get("action_outcomes", {}).get(action, {})
                    if count > 1 and not bool(outcome.get("changed", False)):
                        rows.append({
                            "signature": signature,
                            "action": action,
                            "attempt_count": int(count),
                        })
            rows.sort(key=lambda item: (-item["attempt_count"], item["signature"], item["action"]))
            return rows[:max(0, int(limit))]

    def planner_summary(self, limit=12):
        with self._lock:
            known_activities = set(self.coverage_feedback["known_activities"])
            visited_activities = set(self.coverage_feedback["visited_activities"])
            known_code = set(self.coverage_feedback["known_code_units"])
            visited_code = set(self.coverage_feedback["visited_code_units"])
            return {
                "frontier_states": self.frontier(limit=limit),
                "uncovered_activities": sorted(known_activities - visited_activities)[:limit],
                "uncovered_code_units": sorted(known_code - visited_code)[:limit],
                "no_progress_actions": self.no_progress_actions(limit=limit),
                "known_paths": [
                    {"signature": signature, "path": copy.deepcopy(path)}
                    for signature, path in sorted(self.known_paths.items(), key=lambda item: (len(item[1]), item[0]))
                    if path
                ][:limit],
                "state_count": len(self.ui_states),
                "transition_count": len(self.transitions),
                "pending_peer_events": self.pending_events(),
            }

    def snapshot(self):
        with self._lock:
            return {
                "ui_states": copy.deepcopy(self.ui_states),
                "transitions": copy.deepcopy(self.transitions),
                "action_attempts": {
                    signature: dict(attempts)
                    for signature, attempts in self.action_attempts.items()
                },
                "known_paths": copy.deepcopy(self.known_paths),
                "frontier": self.frontier(),
                "peer_events": self.event_history(),
                "coverage_feedback": {
                    key: set(value) for key, value in self.coverage_feedback.items()
                },
            }


class TaskMemory:
    """Campaign-scoped generated, rejected, and completed task records."""

    def __init__(self):
        self.records = []

    def add(self, record):
        item = copy.deepcopy(record or {})
        task_record = item.get("task_record") if isinstance(item.get("task_record"), dict) else {}
        task_id = str(item.get("task_id") or task_record.get("task_id") or f"task_{len(self.records) + 1}")
        item["task_id"] = task_id
        item.setdefault("recorded_at", _now())
        self.records.append(item)
        return copy.deepcopy(item)

    def recent(self, limit=20):
        return copy.deepcopy(self.records[-max(0, int(limit)):])

    def snapshot(self):
        return copy.deepcopy(self.records)


class MemoryPool:
    def __init__(self, method=None):
        from octopus.experiment_config import MethodOptions

        self.method = method or MethodOptions()
        self.task_candidates = {}
        self.task_event_ids = set()
        self._lock = threading.RLock()
        self.current_device = 1
        self.is_info1_ok = False
        self.is_info2_ok = False

        self.overview_task = ""
        self.device_type_list = []
        self.device_ip_list = []

        self.device_total_num = 1
        self.device_sub_task_list = []

        # Legacy action memory remains public for compatibility with existing agents.
        self.memory_pool_list = []
        self.task_history = []
        self.step_memory = StepMemory()
        self.exploration_memory = ExplorationMemory()
        self.task_memory = TaskMemory()
        self.current_task_record = None
        self._switch_scheduler = None

    def align_1(self, overview_task: str, device_type_list: list, device_ip_list: list):
        with self._lock:
            self.overview_task = overview_task
            self.device_type_list = list(device_type_list or [])
            self.device_ip_list = list(device_ip_list or [])
            self.is_info1_ok = True

    def align_2(self, device_total_num: int, device_sub_task_list: list, first_device_id: int):
        with self._lock:
            self.device_total_num = int(device_total_num)
            self.device_sub_task_list = list(device_sub_task_list or [])
            self._set_current_device_direct(first_device_id)
            self.is_info2_ok = True

    def register_switch_scheduler(self, scheduler):
        with self._lock:
            self._switch_scheduler = scheduler

    def get_switch_scheduler(self):
        with self._lock:
            return self._switch_scheduler

    def _set_current_device_direct(self, device_id: int):
        with self._lock:
            self.current_device = int(device_id)
            return True

    def get_current_device(self):
        with self._lock:
            return self.current_device

    def set_current_device(self, device_id: int, reason: str = "runtime_request",
                           source: str = "memory_pool", cooldown: bool = True):
        with self._lock:
            scheduler = self._switch_scheduler
        if scheduler is not None:
            return scheduler.switch_to_device(
                self,
                int(device_id),
                reason=reason,
                source=source,
                cooldown=cooldown,
            )
        with self._lock:
            if int(device_id) < 1 or int(device_id) > max(1, int(self.device_total_num)):
                return False
            return self._set_current_device_direct(device_id)

    def get_device_total_num(self):
        with self._lock:
            return int(self.device_total_num)

    def begin_task(self, task_id="", task_record=None):
        with self._lock:
            self.task_event_ids = {event["event_id"] for event in self.peer_event_history()}
            self.memory_pool_list = []
            self.current_task_record = copy.deepcopy(task_record)
            self.step_memory.begin(task_id)

    def get_current_task_record(self):
        with self._lock:
            return copy.deepcopy(self.current_task_record)

    def get_memory_snapshot(self):
        with self._lock:
            return copy.deepcopy(self.memory_pool_list)

    def get_step_memory(self):
        with self._lock:
            return self.step_memory.snapshot()

    def record_step_event(self, event_type, **payload):
        with self._lock:
            return copy.deepcopy(self.step_memory.add(event_type, **payload))

    def clear_step_memory(self):
        with self._lock:
            self.memory_pool_list = []
            self.step_memory.clear()

    def add_memory(self, content_type: str, device_id: str, action: str, content: str):
        new_mes = {"type": content_type, "device_id": device_id, "action": action, "content": content}
        with self._lock:
            self.memory_pool_list.append(new_mes)
            self.step_memory.add(
                "operator_memory",
                content_type=str(content_type),
                device_id=str(device_id),
                action=str(action or ""),
                observation=str(content or ""),
            )

    def record_task_history(self, task, episode: int = 0):
        if isinstance(task, dict):
            history_item = copy.deepcopy(task)
            description = str(task.get("description", "")).strip()
            history_item.update({
                "episode": int(episode or 0),
                "description": description,
                "is_multi_device": bool(task.get("is_multi_device", False)),
            })
        else:
            description = str(task or "").strip()
            history_item = {
                "episode": int(episode or 0),
                "description": description,
                "is_multi_device": False,
            }
        if not description:
            return
        with self._lock:
            self.task_history.append(history_item)

    def get_task_history(self):
        with self._lock:
            return copy.deepcopy(self.task_history)

    def record_task_result(self, record):
        with self._lock:
            return self.task_memory.add(record)

    def get_task_memory(self):
        with self._lock:
            return self.task_memory.snapshot()

    def get_exploration_snapshot(self):
        with self._lock:
            return self.exploration_memory.snapshot()

    def record_peer_event(self, *args, **kwargs):
        return self.exploration_memory.record_peer_event(*args, **kwargs)

    def pending_peer_events(self, **match):
        return self.exploration_memory.pending_events(**match)

    def peer_event_history(self):
        return self.exploration_memory.event_history()

    def claim_peer_event(self, task_id, **match):
        event = self.exploration_memory.claim_event(task_id, **match)
        if event:
            self.record_step_event("peer_event_claimed", **event)
        return event

    def consume_peer_event(self, event_id):
        event = self.exploration_memory.consume_event(event_id)
        if event:
            self.record_step_event("peer_event_consumed", **event)
        return event

    def get_planner_summary(self, limit=12):
        with self._lock:
            summary = self.exploration_memory.planner_summary(limit=limit)
            recent = self.task_memory.recent(limit=limit)
            compact_recent = []
            for record in recent:
                task_record = record.get("task_record") if isinstance(record.get("task_record"), dict) else {}
                compact_recent.append({
                    "task_id": record.get("task_id", ""),
                    "task_record": {
                        "description": task_record.get("description", record.get("task", "")),
                        "is_multi_device": bool(task_record.get("is_multi_device", False)),
                        "target_control": task_record.get("target_control", ""),
                        "source_activity": task_record.get("source_activity", ""),
                    },
                    "result": record.get("result", ""),
                    "failure_category": record.get("failure_category", ""),
                    "reason": record.get("reason", ""),
                    "coverage_delta": copy.deepcopy(record.get("coverage_delta", {})),
                    "action_count": int(record.get("action_count", 0) or 0),
                    "total_tokens": int(record.get("total_tokens", 0) or 0),
                })
            summary["recent_task_results"] = compact_recent
            gains = defaultdict(list)
            for record in compact_recent:
                if record.get("result") not in {"success", "failure", "unknown"}:
                    continue
                task_record = record.get("task_record") if isinstance(record.get("task_record"), dict) else {}
                task_type = "multi_device" if task_record.get("is_multi_device") else "single_device"
                delta = record.get("coverage_delta") or {}
                overall = delta.get("__overall__", delta) if isinstance(delta, dict) else {}
                gain = float(overall.get("activity_cov", 0.0) or 0.0) + float(overall.get("code_cov", 0.0) or 0.0)
                gains[task_type].append(gain)
            summary["coverage_gain_by_task_type"] = {
                key: (sum(values) / len(values) if values else 0.0)
                for key, values in gains.items()
            }
            return summary
