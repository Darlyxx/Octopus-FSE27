"""One-step, XML-grounded device operator."""

import json
import re
from types import SimpleNamespace

import xmltodict

from octopus import action_prompts, task_schema
from octopus.services import model_client, memory_store
from octopus.services.android_device import ActionType


def _result(action_type=ActionType.NOP, status=0, **fields):
    action_fields = {
        key: value for key, value in fields.items()
        if key in {
            "device", "selection_reason", "target_label", "requested_target",
            "resource_id", "class", "text", "content_desc", "bounds",
        }
    }
    response_fields = {key: value for key, value in fields.items() if key not in action_fields}
    return SimpleNamespace(response={
        "action_infos": [{"action_type": action_type, **action_fields}],
        "status": status,
        **response_fields,
    })


class DeviceAgent:
    ACTION_PATTERNS = (
        ("tap", re.compile(r"^\[tap\]\s*\[(.+?)\]\s*$", re.I)),
        ("input", re.compile(r"^\[input\]\s*\[(.+?)\]\s*\[(.+?)\]\s*$", re.I)),
        ("switch", re.compile(r"^\[switch\]\s*\[(?:device\s*)?(\d+)\]\s*\[(.+)\]\s*$", re.I)),
        ("back", re.compile(r"^\[back\]\s*$", re.I)),
        ("end_task", re.compile(r"^\[end_task\]\s*$", re.I)),
        ("nop", re.compile(r"^\[nop\]\s*$", re.I)),
    )

    def __init__(self, device_id, execute_state=1):
        del execute_state
        self.device_id = int(device_id)
        self.model_client = model_client.GeneralGPT()
        self.feedback = ""

    @staticmethod
    def _bounds(value):
        numbers = [int(number) for number in re.findall(r"-?\d+", str(value or ""))]
        return numbers[:4] if len(numbers) >= 4 else None

    @staticmethod
    def _label(component):
        return next((
            str(component.get(key, "")).strip()
            for key in ("@text", "@content-desc", "@resource-id")
            if str(component.get(key, "")).strip()
        ), "")

    @classmethod
    def _parse_line(cls, line):
        line = str(line or "").strip()
        for kind, pattern in cls.ACTION_PATTERNS:
            match = pattern.fullmatch(line)
            if not match:
                continue
            action = {"kind": kind, "raw": line}
            if kind == "tap":
                action["target"] = match.group(1).strip()
            elif kind == "input":
                action.update(target=match.group(1).strip(), value=match.group(2).strip())
            elif kind == "switch":
                action.update(device=int(match.group(1)), message=match.group(2).strip())
            return action
        return None

    @classmethod
    def _parse_output(cls, output):
        payload = task_schema.extract_json_object(output)
        if isinstance(payload, dict):
            action = cls._parse_line(payload.get("action"))
            if action:
                action.update(objective=payload.get("objective", ""),
                              expected_effect=payload.get("expected_effect"))
            return action
        lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
        if "### Action ###" in lines:
            lines = lines[lines.index("### Action ###") + 1:]
        matches = []
        for line in lines:
            parsed = cls._parse_line(line)
            if not parsed:
                shorthand = re.fullmatch(r"\[tap\]\s+([^\[].*)", line, re.I)
                if shorthand:
                    parsed = cls._parse_line(f"[tap] [{shorthand.group(1).strip()}]")
            if parsed:
                matches.append(parsed)
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _find_target(cls, components, query, editable=False):
        query = str(query or "").casefold()
        candidates = []
        for component in components:
            if component.get("@enabled", "true") != "true":
                continue
            is_editable = (
                component.get("@editable") == "true"
                or component.get("@class") in {
                    "android.widget.EditText", "android.widget.AutoCompleteTextView"
                }
            )
            if editable != is_editable and editable:
                continue
            if not editable and component.get("@clickable") != "true":
                continue
            fields = [
                str(component.get(key, ""))
                for key in ("@text", "@content-desc", "@resource-id")
            ]
            folded = [value.casefold() for value in fields]
            score = 2 if query in folded else 1 if any(query in value for value in folded) else 0
            if score:
                candidates.append((score, component))
        best = max((score for score, _ in candidates), default=0)
        targets = [component for score, component in candidates if score == best]
        return targets[0] if len(targets) == 1 else None

    def _prompt(self, pool, activity, components):
        roles = pool.device_type_list
        local_goals = pool.device_sub_task_list
        history = pool.get_memory_snapshot()
        prompt = (
            action_prompts.prompt1
            + action_prompts.prompt2(local_goals, pool.overview_task, roles, self.device_id, history)
            + action_prompts.component_prompt(activity, components)
        )
        if pool.get_device_total_num() == 1:
            prompt += "\nThis is a single-device task; [switch] is invalid.\n"
        task = pool.get_current_task_record() or {}
        prompt += f"\nFrozen completion conditions: {json.dumps(task.get('verification_conditions', []))}\n"
        if pool.method.effect_feedback:
            prompt += (
                '\nReturn JSON instead of a bare action line: {"objective":"next local objective",'
                '"action":"[tap] [visible target]","expected_effect":{"device":"Device1",'
                '"type":"visible|foreground_activity|peer_event","expected":"observable value"}}. '
                'Predict one observable effect on a participating device. A prediction is not evidence. '
                'For switch/end_task/nop, expected_effect may be null.\n'
            )
            feedback = [record for record in pool.get_step_memory()["records"]
                        if record.get("event") == "effect_feedback"][-3:]
            prompt += f"Observed effect feedback: {json.dumps(feedback, ensure_ascii=False)}\n"
        if self.feedback:
            prompt += f"\nRuntime feedback: {self.feedback}\n"
            self.feedback = ""
        return prompt

    def _ask_action(self, prompt):
        messages = [
            {"role": "system", "content": "Choose one grounded Android test action."},
            {"role": "user", "content": prompt},
        ]
        response = self.model_client.ask_gpt_message(messages=messages)
        output = response.get("content", "")
        action = self._parse_output(output)
        if action:
            return action, output
        repair = self.model_client.ask_gpt_message(messages=messages + [
            response,
            {
                "role": "user",
                "content": (
                    "Return exactly one action line. Put every argument inside literal square "
                    "brackets: [tap] [target], not [tap] target. Add no explanation."
                ),
            },
        ])
        repaired = repair.get("content", "")
        return self._parse_output(repaired), repaired

    def _grounded_result(self, action_type, component, requested_target, **extra):
        bounds = self._bounds(component.get("@bounds"))
        if not bounds:
            return _result(grounding_failed=True)
        label = self._label(component)
        return _result(
            action_type,
            device=f"Device{self.device_id}",
            bounds=bounds,
            target_label=label,
            requested_target=requested_target,
            resource_id=component.get("@resource-id", ""),
            content_desc=component.get("@content-desc", ""),
            selection_reason=f"Grounded '{requested_target}' to '{label}'.",
            **{"class": component.get("@class", "")},
            **extra,
        )

    def task_execution(self, execute_info, pool: memory_store.MemoryPool):
        self.expected_effect = None
        result = self._task_execution(execute_info, pool)
        if pool.method.effect_feedback and self.expected_effect:
            for item in result.response.get("action_infos", []):
                item["expected_effect"] = self.expected_effect
        return result

    def _task_execution(self, execute_info, pool: memory_store.MemoryPool):
        if pool.get_current_device() != self.device_id:
            return _result()
        xml = action_prompts.xml_align(execute_info.get("xml", ""))
        components = action_prompts.getMergedComponents(xmltodict.parse(xml))
        action, raw = self._ask_action(
            self._prompt(pool, execute_info.get("activity", ""), components)
        )
        if not action:
            pool.add_memory("0", str(self.device_id), "parse_error", raw[:500])
            return _result(parse_error=True, grounding_failed=True)

        pool.add_memory("0", str(self.device_id), action["raw"], raw[:500])
        effect = task_schema.normalize_verification_condition(
            action.get("expected_effect"), pool.device_ip_list
        )
        task = pool.get_current_task_record() or {}
        if effect and effect["device"] in task.get("participating_devices", []):
            self.expected_effect = effect
        kind = action["kind"]
        if kind == "tap":
            target = self._find_target(components, action["target"])
            return (
                self._grounded_result(ActionType.CLICK, target, action["target"])
                if target else _result(grounding_failed=True)
            )
        if kind == "input":
            target = self._find_target(components, action["target"], editable=True)
            return (
                self._grounded_result(
                    ActionType.INPUT, target, action["target"], text=action["value"]
                ) if target else _result(grounding_failed=True)
            )
        if kind == "back":
            return _result(
                ActionType.BACK,
                device=f"Device{self.device_id}",
                selection_reason="Device agent selected Back.",
            )
        if kind == "nop":
            return _result()
        if kind == "end_task":
            return _result(status=1)

        target_device = action["device"]
        if (
            pool.get_device_total_num() == 1
            or target_device == self.device_id
            or not 1 <= target_device <= pool.get_device_total_num()
            or f"Device{target_device}" not in task.get("participating_devices", [])
        ):
            self.feedback = "The requested device switch is invalid."
            return _result(invalid_switch=True)

        local_goal = pool.device_sub_task_list[target_device - 1]
        goal_tokens = set(re.findall(r"[a-z0-9_]{3,}", local_goal.casefold()))
        eligible = next((
            event for event in pool.pending_peer_events(device=f"Device{target_device}")
            if goal_tokens & set(re.findall(r"[a-z0-9_]{3,}", event["value"].casefold()))
        ), None)
        claimed = (
            pool.claim_peer_event(
                pool.get_step_memory().get("task_id", ""),
                device=f"Device{target_device}",
                expected=eligible["value"],
            ) if eligible else None
        )
        switched = pool.set_current_device(
            target_device,
            reason=action["message"],
            source="operator",
        )
        if not switched:
            return _result(invalid_switch=True)
        if claimed:
            pool.consume_peer_event(claimed["event_id"])
        pool.add_memory("1", str(self.device_id), action["raw"], action["message"])
        return _result(device_switch=True)
