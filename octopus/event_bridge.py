"""Collect and route cross-device Android events."""

import json
import os
import re
import subprocess
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep, time
from urllib import error as urlerror
from urllib import request as urlrequest

from octopus.services import android_device, trace_writer
from octopus.services.instrumentation import AndroidLogCoverageParser
from octopus.services.coverage_store import CoverageStore

LOGCAT_EVENT_PATTERNS = (
    "toast", "permission", "grant", "denied", "dialog", "activitytaskmanager",
    "windowmanager", "displayed", "resumed", "notification", "incoming", "invite",
)
ACCESSIBILITY_EVENT_TYPES = (
    "TYPE_WINDOW_STATE_CHANGED",
    "TYPE_WINDOW_CONTENT_CHANGED",
    "TYPE_NOTIFICATION_STATE_CHANGED",
    "TYPE_VIEW_TEXT_CHANGED",
    "TYPE_WINDOWS_CHANGED",
)


def _device_number(value):
    match = re.search(r"d+", str(value or ""))
    return int(match.group()) if match else 0

class DeviceEventBus:
    def __init__(self, pool=None, device_index=0, package_name=""):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reasons = deque(maxlen=64)
        self._last_signal_ts = 0.0
        self._pool = pool
        self._device_index = device_index
        self._package_name = package_name

    def signal(self, reason: str):
        with self._lock:
            now = time()
            # De-bounce very dense same-source events.
            if str(reason).startswith("logcat:") and (now - self._last_signal_ts) < 0.05:
                return
            self._last_signal_ts = now
            self._reasons.append(reason)
            self._event.set()
            if self._pool is not None:
                event_type = str(reason or "peer_event").split(":", 1)[0]
                self._pool.record_peer_event(
                    f"Device{self._device_index}",
                    event_type,
                    reason,
                    package_name=self._package_name,
                )

    def wait(self, timeout: float):
        return self._event.wait(timeout)

    def consume_all(self):
        with self._lock:
            reasons = list(self._reasons)
            self._reasons.clear()
            self._event.clear()
            return reasons


def wait_for_task_peer_event(pool, task_record, event_buses, timeout=0.5):
    conditions = (
        task_record.get("verification_conditions", [])
        if isinstance(task_record, dict) else []
    )
    peer_conditions = [
        condition for condition in conditions
        if isinstance(condition, dict) and condition.get("type") == "peer_event"
    ]
    if not peer_conditions:
        sleep(timeout)
        return None
    task_id = pool.get_step_memory().get("task_id", "")
    for condition in peer_conditions:
        target = _device_number(condition.get("device"))
        bus = event_buses.get(target)
        if bus:
            bus.wait(timeout)
        event = pool.claim_peer_event(
            task_id,
            device=f"Device{target}",
            expected=condition.get("expected", ""),
        )
        if event:
            return pool.consume_peer_event(event["event_id"])
    return None


def _post_json(endpoint: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urlrequest.urlopen(req, timeout=1.2) as resp:
        resp.read()


def _env_enabled(name: str, default: str = "1"):
    return os.getenv(name, default).strip().lower() not in ("0", "false", "off")


def start_accessibility_http_router(event_buses: dict, stop_event: threading.Event):
    host = os.getenv("OCTOPUS_EVENT_HTTP_HOST", "127.0.0.1")
    port = int(os.getenv("OCTOPUS_EVENT_HTTP_PORT", "18765"))
    endpoint = f"http://{host}:{port}/accessibility_event"

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/accessibility_event":
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8", errors="ignore"))
                device_index = int(payload.get("device_index", 0))
                event_type = str(payload.get("event_type", ""))
                event_text = str(payload.get("event_text", ""))
                bus = event_buses.get(device_index)
                if bus is not None:
                    bus.signal(f"acc:{event_type}:{event_text[:80]}")
                self.send_response(200)
                self.end_headers()
            except Exception:
                self.send_response(400)
                self.end_headers()

        def log_message(self, format, *args):
            return

    def _serve():
        server = ThreadingHTTPServer((host, port), _Handler)
        server.timeout = 0.5
        trace_writer.log_event("accessibility_http_router_start", host=host, port=port)
        try:
            while not stop_event.is_set():
                server.handle_request()
        finally:
            try:
                server.server_close()
            except Exception:
                pass
            trace_writer.log_event("accessibility_http_router_stop", host=host, port=port)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t, endpoint


def _extract_accessibility_event(line: str):
    if not line:
        return None, ""
    for evt in ACCESSIBILITY_EVENT_TYPES:
        if evt in line:
            # best-effort text extraction
            text_match = re.search(r"text[:=]\s*(.+?)(?:,\s*\w+[:=]|$)", line, flags=re.IGNORECASE)
            event_text = text_match.group(1).strip() if text_match else ""
            return evt, event_text
    return None, ""


def start_accessibility_event_helper(device_id: str, device_index: int, endpoint: str,
                                     stop_event: threading.Event):
    def _run():
        cmd = ["adb", "-s", str(device_id), "shell", "uiautomator", "events"]
        process = None
        while not stop_event.is_set():
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                while not stop_event.is_set():
                    line = process.stdout.readline() if process.stdout else ""
                    if not line:
                        break
                    event_type, event_text = _extract_accessibility_event(line.strip())
                    if not event_type:
                        continue
                    payload = {
                        "device_index": int(device_index),
                        "device_id": str(device_id),
                        "event_type": event_type,
                        "event_text": event_text,
                        "raw": line.strip()[:240],
                        "ts": datetime.now().isoformat(timespec="seconds")
                    }
                    try:
                        _post_json(endpoint, payload)
                    except (urlerror.URLError, TimeoutError, OSError):
                        # Router may not be ready or transiently unavailable.
                        pass
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=1)
            except Exception as e:
                trace_writer.log_event(
                    "accessibility_helper_error",
                    device_id=device_id,
                    device_index=device_index,
                    error=str(e)
                )
            sleep(0.35)
        try:
            if process and process.poll() is None:
                process.terminate()
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def start_logcat_event_listener(device_id: str, bus: DeviceEventBus, stop_event: threading.Event):
    def _run():
        cmd = [
            "adb", "-s", str(device_id), "logcat", "-v", "brief", "-T", "1",
            "ActivityTaskManager:I", "WindowManager:I", "NotificationManager:I", "Toast:I", "*:S"
        ]
        process = None
        while not stop_event.is_set():
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                while not stop_event.is_set():
                    line = process.stdout.readline() if process.stdout else ""
                    if not line:
                        break
                    lower_line = line.strip().lower()
                    if any(keyword in lower_line for keyword in LOGCAT_EVENT_PATTERNS):
                        bus.signal(f"logcat:{lower_line[:160]}")
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=1)
            except Exception as e:
                trace_writer.log_event(
                    "logcat_listener_error",
                    device_id=device_id,
                    error=str(e)
                )
            sleep(0.35)
        try:
            if process and process.poll() is None:
                process.terminate()
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def start_androidlog_coverage_listener(device_id: str, device_index: int, coverage_store: CoverageStore,
                                       stop_event: threading.Event,
                                       parser: AndroidLogCoverageParser,
                                       clear_logcat: bool = True):
    if clear_logcat:
        android_device.execute_adb_args(["adb", "-s", str(device_id), "logcat", "-c"])

    listener_ready = threading.Event()

    def _run():
        cmd = [
            "adb", "-s", str(device_id), "logcat", "-v", "brief",
            f"{parser.tag}:V", "*:S"
        ]

        process = None
        while not stop_event.is_set():
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                listener_ready.set()
                while not stop_event.is_set():
                    line = process.stdout.readline() if process.stdout else ""
                    if not line:
                        break
                    raw_line = line.strip()
                    unit = parser.parse_line(raw_line)
                    if not unit:
                        continue
                    coverage_store.record_code_unit(device_index, unit)
                    trace_writer.log_event(
                        "code_coverage_hit", device_index=device_index,
                        device_id=device_id, unit=unit, raw=raw_line,
                    )
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=1)
            except Exception as e:
                listener_ready.set()
                trace_writer.log_event(
                    "androidlog_coverage_listener_error",
                    device_id=device_id,
                    device_index=device_index,
                    error=str(e)
                )
            sleep(0.35)
        try:
            if process and process.poll() is None:
                process.terminate()
        except Exception:
            pass
        finally:
            listener_ready.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    listener_ready.wait(timeout=5)
    return t
