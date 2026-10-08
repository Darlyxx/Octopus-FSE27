"""Track task budgets, action history, and device switching."""

import json
import threading
from time import time

from octopus.services import model_client, memory_store, trace_writer
class RuntimeBudget:
    def __init__(self, max_actions=0, max_llm_calls=0, max_total_tokens=0):
        self._lock = threading.RLock()
        self.max_actions = max(0, int(max_actions or 0))
        self.max_llm_calls = max(0, int(max_llm_calls or 0))
        self.max_total_tokens = max(0, int(max_total_tokens or 0))
        self.action_count = 0

    def record_actions(self, count=1):
        with self._lock:
            self.action_count += max(0, int(count or 0))
            return self.action_count

    def snapshot(self, llm_usage=None):
        usage = dict(llm_usage or model_client.get_usage())
        with self._lock:
            return {
                "action_count": self.action_count,
                "max_actions": self.max_actions,
                "max_llm_calls": self.max_llm_calls,
                "max_total_tokens": self.max_total_tokens,
                **usage,
            }

    def exhaustion_reason(self, llm_usage=None):
        snapshot = self.snapshot(llm_usage)
        if self.max_actions and snapshot["action_count"] >= self.max_actions:
            return "action_budget_exhausted"
        if self.max_llm_calls and snapshot["llm_call_count"] >= self.max_llm_calls:
            return "llm_call_budget_exhausted"
        if self.max_total_tokens and snapshot["total_tokens"] >= self.max_total_tokens:
            return "token_budget_exhausted"
        return ""


class ExecutionLedger:
    def __init__(self, runtime_budget=None):
        self._lock = threading.RLock()
        self.runtime_budget = runtime_budget
        self.begin("", {}, {}, {})

    def begin(self, task_id, start_global_state, coverage_before, llm_usage_before):
        with self._lock:
            self.task_id = str(task_id or "")
            self.started_at = time()
            self.start_global_state = json.loads(json.dumps(start_global_state or {}, default=str))
            self.coverage_before = json.loads(json.dumps(coverage_before or {}, default=str))
            self.llm_usage_before = dict(llm_usage_before or {})
            self.evidence = []
            self.action_count = 0
            self.device_switch_count = 0
            self.event_triggered_switch_count = 0
            self.consecutive_no_progress = 0
            self.consecutive_grounding_failures = 0
            self.operator_end_requested = False

    def record_actions(self, operations, changed=None):
        operations = [dict(item) for item in (operations or []) if isinstance(item, dict)]
        count = len([item for item in operations if str(item.get("type", "")).lower() != "nop"])
        with self._lock:
            for item in operations:
                if changed is not None:
                    item.setdefault("changed", bool(changed))
                self.evidence.append(item)
            self.action_count += count
        if self.runtime_budget is not None:
            self.runtime_budget.record_actions(count)

    def record_switch(self, from_device, to_device, reason, source):
        event_triggered = any(
            marker in str(source or "").lower()
            for marker in ("event", "interrupt", "accessibility", "logcat")
        )
        with self._lock:
            self.device_switch_count += 1
            if event_triggered:
                self.event_triggered_switch_count += 1
            self.evidence.append({
                "type": "device_switch",
                "device": f"Device{to_device}",
                "from_device": f"Device{from_device}",
                "reason": str(reason or ""),
                "source": str(source or ""),
                "event_triggered": event_triggered,
            })

    def record_evidence(self, evidence):
        with self._lock:
            self.evidence.append(dict(evidence or {}))

    def record_progress(self, changed):
        with self._lock:
            self.consecutive_no_progress = 0 if changed else self.consecutive_no_progress + 1
            return self.consecutive_no_progress

    def record_grounding(self, failed):
        with self._lock:
            self.consecutive_grounding_failures = (
                self.consecutive_grounding_failures + 1 if failed else 0
            )
            return self.consecutive_grounding_failures

    def no_progress_count(self):
        with self._lock:
            return self.consecutive_no_progress

    def mark_operator_end(self, device):
        with self._lock:
            self.operator_end_requested = True
            self.evidence.append({
                "type": "operator_end_request",
                "device": f"Device{device}",
                "note": "request only; not success evidence",
            })

    def snapshot(self):
        with self._lock:
            return {
                "task_id": self.task_id,
                "started_at": self.started_at,
                "start_global_state": json.loads(json.dumps(self.start_global_state)),
                "coverage_before": json.loads(json.dumps(self.coverage_before)),
                "llm_usage_before": dict(self.llm_usage_before),
                "evidence": json.loads(json.dumps(self.evidence, default=str)),
                "action_count": self.action_count,
                "device_switch_count": self.device_switch_count,
                "event_triggered_switch_count": self.event_triggered_switch_count,
                "consecutive_no_progress": self.consecutive_no_progress,
                "consecutive_grounding_failures": self.consecutive_grounding_failures,
                "operator_end_requested": self.operator_end_requested,
            }


class RuntimeSwitchState:
    def __init__(self, cooldown_seconds: float = 0.6, ledger=None):
        self._lock = threading.RLock()
        self._last_switch_ts = 0.0
        self._cooldown_seconds = cooldown_seconds
        self._ledger = ledger

    def switch_to_device(self, pool: memory_store.MemoryPool, target_device: int,
                         reason: str, source: str = "manual", cooldown: bool = True):
        now = time()
        with self._lock:
            total_devices = pool.get_device_total_num()
            if target_device < 1 or target_device > total_devices:
                return False
            current_device = pool.get_current_device()
            if current_device == target_device:
                return False
            if cooldown and (now - self._last_switch_ts) < self._cooldown_seconds:
                return False
            pool._set_current_device_direct(target_device)
            self._last_switch_ts = now
            trace_writer.log_event(
                "runtime_device_switch",
                source=source,
                from_device=current_device,
                to_device=target_device,
                reason=reason
            )
            pool.record_step_event(
                "device_switch",
                from_device=current_device,
                to_device=target_device,
                reason=reason,
                source=source,
            )
            if self._ledger is not None:
                self._ledger.record_switch(current_device, target_device, reason, source)
            print(f"[Parallel Switch] {source}: device{current_device} -> device{target_device} ({reason})")
            return True

    def execute_if_current(self, pool: memory_store.MemoryPool, device_index: int, callback):
        with self._lock:
            if pool.get_current_device() != int(device_index):
                return False, None
            return True, callback()
