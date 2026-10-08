import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


OCTOPUS_COVERAGE_TAG = "MADROID_COVERAGE"
_METHOD_PATTERN = re.compile(r"METHOD=(<.+?>)")
_DEX_NAME_PATTERN = re.compile(r"classes(?:\d+)?\.dex$")
_CONST_STRING_PATTERN = re.compile(r'const-string(?:/jumbo)?\s+v\d+,\s+"(.*)"')


def _normalize_unit(value: str):
    value = re.sub(r"\s+", " ", str(value or "").strip())
    if not value:
        return ""
    match = _METHOD_PATTERN.search(value)
    if match:
        return match.group(1)
    return value.strip(" ,;")


class AndroidLogCoverageParser:
    """Parse the fixed log protocol inserted by the AndroidLog instrumenter."""

    tag = OCTOPUS_COVERAGE_TAG

    def parse_line(self, line: str):
        line = str(line or "").strip()
        if self.tag not in line:
            return ""
        return _normalize_unit(line)


def _find_dexdump():
    executable = "dexdump.exe" if os.name == "nt" else "dexdump"
    candidates = []
    for sdk_root in (
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path.home() / "AppData" / "Local" / "Android" / "Sdk"),
    ):
        if not sdk_root:
            continue
        build_tools = Path(sdk_root) / "build-tools"
        if build_tools.is_dir():
            candidates.extend(build_tools.glob(f"*/{executable}"))

    adb_path = shutil.which("adb")
    if adb_path:
        build_tools = Path(adb_path).resolve().parent.parent / "build-tools"
        if build_tools.is_dir():
            candidates.extend(build_tools.glob(f"*/{executable}"))

    path_candidate = shutil.which("dexdump") or shutil.which(executable)
    if path_candidate:
        candidates.append(Path(path_candidate))

    existing = [path for path in candidates if path.is_file()]
    if not existing:
        raise RuntimeError("Android SDK dexdump was not found; install Android SDK Build-Tools.")
    return str(sorted(set(existing), key=lambda path: str(path))[-1])


def _extract_units_from_dex(dexdump_path: str, dex_path: str):
    """Read only instrumentation calls from dexdump output without writing its large dump to disk."""
    units = set()
    command = [dexdump_path, "-d", dex_path]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    method_strings = []
    has_coverage_call = False

    def flush_method():
        if not has_coverage_call:
            return
        for value in method_strings:
            unit = _normalize_unit(value)
            if unit.startswith("<") and unit.endswith(">"):
                units.add(unit)

    assert process.stdout is not None
    for line in process.stdout:
        if "|[" in line and "] " in line:
            flush_method()
            method_strings = []
            has_coverage_call = False

        string_match = _CONST_STRING_PATTERN.search(line)
        if string_match:
            method_strings.append(string_match.group(1))

        if "invoke-" in line and "LLogCheckerClass;.log:" in line:
            has_coverage_call = True
    flush_method()

    stderr = process.stderr.read() if process.stderr else ""
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"dexdump failed for {os.path.basename(dex_path)}: {stderr.strip()}")
    return units


def extract_androidlog_instrumented_units(apk_path: str):
    """Return the exact method signatures passed to LogCheckerClass.log in an APK."""
    apk_path = os.path.abspath(apk_path)
    if not apk_path or not os.path.isfile(apk_path):
        raise ValueError("A readable instrumented APK is required for code coverage.")

    dexdump_path = _find_dexdump()
    with zipfile.ZipFile(apk_path) as archive, tempfile.TemporaryDirectory(prefix="octopus-coverage-") as temp_dir:
        dex_entries = sorted(name for name in archive.namelist() if _DEX_NAME_PATTERN.fullmatch(name))
        if not dex_entries:
            raise RuntimeError("The APK does not contain any DEX files.")
        units = set()
        for dex_name in dex_entries:
            dex_path = os.path.join(temp_dir, os.path.basename(dex_name))
            with archive.open(dex_name) as source, open(dex_path, "wb") as target:
                shutil.copyfileobj(source, target)
            units.update(_extract_units_from_dex(dexdump_path, dex_path))
    return units
