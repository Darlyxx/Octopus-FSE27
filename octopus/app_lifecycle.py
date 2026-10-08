"""Install, launch, and inspect Android applications."""

import os
import re
from time import sleep

from octopus.services import android_device, trace_writer
from octopus.services.coverage_store import CoverageStore
def _resolve_app_package(apk_path: str, explicit_package: str):
    if explicit_package:
        return explicit_package.strip()
    if not apk_path:
        return ""
    package_name = android_device.get_package_from_apk(apk_path)
    if not package_name:
        print("[APK Warning] Could not infer package name from APK. Pass --app-package to enable launch/activity coverage.")
    return package_name


def _install_apk_on_devices(device_ids: list, apk_path: str):
    if not apk_path:
        return []
    apk_path = os.path.abspath(apk_path)
    if not os.path.exists(apk_path):
        raise FileNotFoundError(f"APK not found: {apk_path}")
    results = []
    for device_id in device_ids:
        result = android_device.install_apk(device_id, apk_path)
        ok = result.get("returncode") == 0 and "Failure" not in result.get("stdout", "")
        install_result = {
            "device_id": device_id,
            "apk_path": apk_path,
            "ok": ok,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
        }
        results.append(install_result)
        trace_writer.log_event("apk_install", **install_result)
        if not ok:
            raise RuntimeError(
                f"APK install failed on {device_id}: {install_result['stderr'] or install_result['stdout']}"
            )
    return results


def _launch_app_on_devices(devices: dict, device_count: int, package_name: str):
    if not package_name:
        return
    for i in range(device_count):
        try:
            devices[f"d{i + 1}"].app_start(package_name)
            trace_writer.log_event("app_launch", device_index=i + 1, package=package_name)
        except Exception as e:
            trace_writer.log_event("app_launch_error", device_index=i + 1, package=package_name, error=str(e))
    sleep(2)




def _infer_app_package_from_contexts(contexts: list):
    ignored_packages = {"unknown", "com.android.systemui", "com.android.launcher", "com.google.android.apps.nexuslauncher"}
    for ctx in contexts or []:
        package_name = str(ctx.get("package", "")).strip()
        if package_name and package_name not in ignored_packages:
            return package_name
    return ""


def _normalize_component_name(package_name: str, raw_name: str):
    raw_name = str(raw_name or "").strip()
    if not raw_name:
        return ""
    if "/" in raw_name:
        raw_name = raw_name.split("/", 1)[1]
    if raw_name.startswith("."):
        return f"{package_name}{raw_name}"
    if raw_name.startswith(package_name):
        return raw_name
    if "." not in raw_name:
        return f"{package_name}.{raw_name}"
    return raw_name


def _extract_apk_manifest_activities(apk_path: str, package_name: str):
    apk_path = os.path.abspath(apk_path) if apk_path else ""
    if not apk_path or not os.path.exists(apk_path):
        return set(), "apk_missing"

    manifest_info = android_device.get_manifest_info_from_apk(apk_path)
    manifest_package = manifest_info.get("package") or package_name
    manifest_activities = {
        _normalize_component_name(manifest_package, activity)
        for activity in manifest_info.get("activities", set())
    }
    manifest_activities = {activity for activity in manifest_activities if activity}
    if manifest_activities:
        return manifest_activities, manifest_info.get("source", "binary_android_manifest")

    commands = [
        ("aapt_badging", ["aapt", "dump", "badging", apk_path]),
        ("aapt2_badging", ["aapt2", "dump", "badging", apk_path]),
        ("apkanalyzer_manifest", ["apkanalyzer", "manifest", "print", apk_path]),
    ]
    for source, cmd in commands:
        try:
            result = android_device.execute_adb_args(cmd)
        except FileNotFoundError:
            continue
        if result.get("returncode") != 0:
            continue
        output = result.get("stdout", "")
        activities = set()
        for match in re.finditer(r"(?:launchable-activity|activity|activity-alias):\s+name='([^']+)'", output):
            normalized = _normalize_component_name(package_name, match.group(1))
            if normalized:
                activities.add(normalized)
        for match in re.finditer(
            r"<(?:activity|activity-alias)\b[^>]*\bandroid:name=[\"']([^\"']+)[\"']",
            output
        ):
            normalized = _normalize_component_name(package_name, match.group(1))
            if normalized:
                activities.add(normalized)
        if activities:
            return activities, source
    return set(), "apk_manifest_unavailable"


def _extract_package_activities(package_name: str, dumpsys_text: str):
    activities = set()
    if not package_name or not dumpsys_text or dumpsys_text == "ERROR":
        return activities

    package_pattern = re.escape(package_name)
    component_pattern = re.compile(rf"{package_pattern}/([A-Za-z0-9_.$]+)")
    fqcn_pattern = re.compile(rf"\b({package_pattern}\.[A-Za-z0-9_.$]+)\b")
    for line in dumpsys_text.splitlines():
        line_components = set()
        for match in component_pattern.finditer(line):
            line_components.add(_normalize_component_name(package_name, match.group(1)))
        for match in fqcn_pattern.finditer(line):
            line_components.add(_normalize_component_name(package_name, match.group(1)))
        for component in line_components:
            lower_line = line.lower()
            lower_component = component.lower()
            if "activity" in lower_line or lower_component.endswith("activity"):
                activities.add(component)
    return activities


def _register_static_activity_baseline(coverage_store: CoverageStore, device_ids: list,
                                       package_name: str, apk_path: str = ""):
    metadata = {
        "package": package_name or "",
        "source": "runtime_observation",
        "apk_path": os.path.abspath(apk_path) if apk_path else "",
        "apk_activity_source": "",
        "apk_known_activities": 0,
        "device_known_activities": 0,
        "known_activities": 0,
    }
    if not package_name:
        return metadata

    apk_activities, apk_activity_source = _extract_apk_manifest_activities(apk_path, package_name)
    metadata["apk_activity_source"] = apk_activity_source

    all_activities = set()
    all_activities.update(apk_activities)
    device_activities = set()
    for device_id in device_ids:
        result = android_device.execute_adb_args(
            ["adb", "-s", str(device_id), "shell", "dumpsys", "package", package_name]
        )
        if result.get("returncode") != 0:
            trace_writer.log_event(
                "package_static_activity_error",
                device_id=device_id,
                package=package_name,
                stderr=result.get("stderr", "")
            )
            continue
        dumpsys_text = result.get("stdout", "")
        activities = _extract_package_activities(package_name, dumpsys_text)
        device_activities.update(activities)
    all_activities.update(device_activities)

    coverage_store.set_known_activities(all_activities)
    if apk_activities and device_activities:
        source = "apk_manifest+adb_dumpsys_package"
    elif apk_activities:
        source = "apk_manifest"
    elif device_activities:
        source = "adb_dumpsys_package"
    else:
        source = "runtime_observation"
    metadata.update({
        "source": source,
        "apk_known_activities": len(apk_activities),
        "device_known_activities": len(device_activities),
        "known_activities": len(all_activities),
        "known_activity_list": sorted(all_activities),
    })
    trace_writer.log_event("package_static_activity_baseline", **metadata)
    return metadata
