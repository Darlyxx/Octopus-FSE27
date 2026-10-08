"""Translate a task record into the small execution plan consumed by operators."""

import re

from octopus.services.memory_store import MemoryPool


def _device_number(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


class TaskDispatcher:
    def __init__(self, task_record):
        if not isinstance(task_record, dict):
            raise TypeError("TaskDispatcher requires a structured task record.")
        self.task_record = task_record
        self.overview_task = str(
            task_record.get("goal") or task_record.get("description") or ""
        ).strip()
        self.device_num = 1
        self.device_type_list = []
        self.sub_task_list = []
        self.first_device_num = 1

    def _participants(self, available):
        numbers = [
            _device_number(value)
            for value in self.task_record.get("participating_devices", [])
        ]
        numbers = sorted({number for number in numbers if 1 <= number <= available})
        if not numbers:
            raise ValueError("Task has no available participating device.")
        return numbers

    def _local_goals(self):
        goals = {
            _device_number(item.get("device")): str(item.get("goal", ""))
            for item in self.task_record.get("device_goals", [])
            if isinstance(item, dict)
        }
        return [
            goals.get(device, "Observe only.")
            for device in range(1, self.device_num + 1)
        ]

    def task_create(self, device_type_list, device_ip_list, pool=None):
        pool = pool or MemoryPool()
        available = min(len(device_type_list or []), len(device_ip_list or []))
        if available < 1:
            raise ValueError("At least one available device is required.")
        participants = self._participants(available)
        self.device_num = max(participants)
        self.device_type_list = list(device_type_list[:self.device_num])
        self.sub_task_list = self._local_goals()
        requested_first = _device_number(
            self.task_record.get("first_device") or self.task_record.get("start_device")
        )
        self.first_device_num = requested_first if requested_first in participants else participants[0]
        pool.align_1(self.overview_task, self.device_type_list, device_ip_list[:self.device_num])
        pool.align_2(self.device_num, self.sub_task_list, self.first_device_num)
        return pool
