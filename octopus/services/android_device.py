import subprocess
import re
import os
import struct
import zipfile
from enum import Enum


class ActionType(Enum):
    ACTION_UNKNOWN = 0
    NOP = 1
    ACTIVATE = 2
    BACK = 3
    CLICK = 4
    LONG_CLICK = 5
    SCROLL_TOP_DOWN = 6
    SCROLL_BOTTOM_UP = 7
    SCROLL_LEFT_RIGHT = 8
    SCROLL_RIGHT_LEFT = 9
    ACTION_DOWN = 10
    ACTION_MOVE = 11
    ACTION_UP = 12
    SCROLL = 13
    INPUT = 14


def execute_adb(adb_command):
    # print(adb_command)
    result = subprocess.run(adb_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    print(f"Command execution failed: {adb_command}")
    print(result.stderr)
    return "ERROR"


def execute_adb_args(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def install_apk(device_id: str, apk_path: str):
    return execute_adb_args(["adb", "-s", str(device_id), "install", "-r", apk_path])


def _read_length8(data: bytes, offset: int):
    first = data[offset]
    if first & 0x80:
        return ((first & 0x7F) << 8) | data[offset + 1], offset + 2
    return first, offset + 1


def _read_length16(data: bytes, offset: int):
    first = struct.unpack_from("<H", data, offset)[0]
    if first & 0x8000:
        second = struct.unpack_from("<H", data, offset + 2)[0]
        return ((first & 0x7FFF) << 16) | second, offset + 4
    return first, offset + 2


def _decode_string_pool(data: bytes, offset: int):
    header_size = struct.unpack_from("<H", data, offset + 2)[0]
    string_count = struct.unpack_from("<I", data, offset + 8)[0]
    flags = struct.unpack_from("<I", data, offset + 16)[0]
    strings_start = struct.unpack_from("<I", data, offset + 20)[0]
    offsets_start = offset + header_size
    strings_base = offset + strings_start
    is_utf8 = bool(flags & 0x00000100)

    strings = []
    for index in range(string_count):
        string_offset = struct.unpack_from("<I", data, offsets_start + index * 4)[0]
        pos = strings_base + string_offset
        if is_utf8:
            _, pos = _read_length8(data, pos)
            byte_length, pos = _read_length8(data, pos)
            raw = data[pos:pos + byte_length]
            strings.append(raw.decode("utf-8", errors="replace"))
        else:
            char_length, pos = _read_length16(data, pos)
            raw = data[pos:pos + char_length * 2]
            strings.append(raw.decode("utf-16le", errors="replace"))
    return strings


def _string_at(strings: list, index: int):
    if index == 0xFFFFFFFF or index < 0 or index >= len(strings):
        return ""
    return strings[index]


def _parse_binary_android_manifest(manifest_bytes: bytes):
    strings = []
    package_name = ""
    activities = set()
    components = {
        "activity": set(),
        "activity-alias": set(),
        "service": set(),
        "receiver": set(),
        "provider": set(),
    }
    offset = 8
    while offset + 8 <= len(manifest_bytes):
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", manifest_bytes, offset)
        if chunk_size <= 0:
            break

        if chunk_type == 0x0001:
            strings = _decode_string_pool(manifest_bytes, offset)
        elif chunk_type == 0x0102 and strings:
            element_name_index = struct.unpack_from("<I", manifest_bytes, offset + 20)[0]
            element_name = _string_at(strings, element_name_index)
            attr_start = struct.unpack_from("<H", manifest_bytes, offset + 24)[0]
            attr_size = struct.unpack_from("<H", manifest_bytes, offset + 26)[0]
            attr_count = struct.unpack_from("<H", manifest_bytes, offset + 28)[0]
            attrs = {}
            attr_base = offset + 16 + attr_start
            for attr_index in range(attr_count):
                attr_offset = attr_base + attr_index * attr_size
                if attr_offset + 20 > len(manifest_bytes):
                    continue
                attr_name_index = struct.unpack_from("<I", manifest_bytes, attr_offset + 4)[0]
                raw_value_index = struct.unpack_from("<I", manifest_bytes, attr_offset + 8)[0]
                data_type = struct.unpack_from("<B", manifest_bytes, attr_offset + 15)[0]
                data_value = struct.unpack_from("<I", manifest_bytes, attr_offset + 16)[0]
                attr_name = _string_at(strings, attr_name_index)
                if raw_value_index != 0xFFFFFFFF:
                    attr_value = _string_at(strings, raw_value_index)
                elif data_type == 0x03:
                    attr_value = _string_at(strings, data_value)
                else:
                    attr_value = str(data_value)
                attrs[attr_name] = attr_value

            if element_name == "manifest" and attrs.get("package"):
                package_name = attrs.get("package", "")
            if element_name in components and attrs.get("name"):
                components[element_name].add(attrs["name"])
                if element_name in {"activity", "activity-alias"}:
                    activities.add(attrs["name"])

        offset += chunk_size

    return package_name, activities, components


def get_manifest_info_from_apk(apk_path: str):
    if not apk_path or not os.path.exists(apk_path):
        return {"package": "", "activities": set(), "components": {}, "source": "apk_missing"}
    try:
        with zipfile.ZipFile(apk_path) as archive:
            manifest_bytes = archive.read("AndroidManifest.xml")
        package_name, activities, components = _parse_binary_android_manifest(manifest_bytes)
        return {
            "package": package_name,
            "activities": activities,
            "components": components,
            "source": "binary_android_manifest",
        }
    except Exception:
        return {"package": "", "activities": set(), "components": {}, "source": "binary_manifest_parse_error"}


def get_package_from_apk(apk_path: str):
    commands = [
        ["aapt", "dump", "badging", apk_path],
        ["aapt2", "dump", "badging", apk_path],
        ["apkanalyzer", "manifest", "application-id", apk_path],
    ]
    for cmd in commands:
        try:
            result = execute_adb_args(cmd)
        except FileNotFoundError:
            continue
        if result["returncode"] != 0:
            continue
        output = result["stdout"]
        package_match = re.search(r"package:\s+name='([^']+)'", output)
        if package_match:
            return package_match.group(1)
        output = output.strip()
        if re.match(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$", output):
            return output
    manifest_info = get_manifest_info_from_apk(apk_path)
    if manifest_info.get("package"):
        return manifest_info["package"]
    return ""


class AndroidController:

    def __init__(self, device, ip=None):
        self.device = device
        self.width, self.height = self.get_device_size()
        self.backslash = "\\"
        if ip is not None:
            self.ip = ip
        else:
            self.ip = self.get_device_ip()

    def get_device_size(self):
        adb_command = f"adb -s {self.device} shell wm size"
        result = execute_adb(adb_command)
        if result != "ERROR":
            result = result.splitlines()[-1]
            return map(int, result.split(": ")[1].split("x"))
        return 0, 0

    def get_device_ip(self):
        # Prefer modern command first: parse src IP from `ip route`.
        route_result = execute_adb(f"adb -s {self.device} shell ip route")
        if route_result != "ERROR":
            src_match = re.search(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b", route_result)
            if src_match:
                ip = src_match.group(1)
                print("get device {} ip success, ip is:{}".format(self.device, ip))
                return ip

        # Fallback for older Android/tooling: parse `ifconfig` output.
        ifconfig_result = execute_adb(f"adb -s {self.device} shell ifconfig")
        if ifconfig_result != "ERROR":
            for match in re.finditer(r"\b(?:inet addr:|inet )(\d{1,3}(?:\.\d{1,3}){3})\b", ifconfig_result):
                candidate_ip = match.group(1)
                if candidate_ip != "127.0.0.1":
                    print("get device {} ip success, ip is:{}".format(self.device, candidate_ip))
                    return candidate_ip

        # Legacy fallback: parse `netcfg` without shell pipes.
        netcfg_result = execute_adb(f"adb -s {self.device} shell netcfg")
        if netcfg_result != "ERROR":
            for line in netcfg_result.splitlines():
                if "wlan0" in line or "rmnet0" in line:
                    match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})/\d{1,2}", line)
                    if match:
                        ip = match.group(1)
                        print("get device {} ip success, ip is:{}".format(self.device, ip))
                        return ip

        raise Exception("get ip error, please set device ip manually!")

    def get_activity(self):
        result = execute_adb(f"adb -s {self.device} shell dumpsys window windows")
        if result == "ERROR":
            return result
        for line in result.splitlines():
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                return line.strip()
        return ""


    def back(self):
        adb_command = f"adb -s {self.device} shell input keyevent KEYCODE_BACK"
        ret = execute_adb(adb_command)
        return ret

    def tap(self, tl, br):
        x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
        adb_command = f"adb -s {self.device} shell input tap {x} {y}"
        ret = execute_adb(adb_command)
        return ret

    def tap_point(self, x: float, y: float):
        # x = int(x * self.width)
        # y = int(y * self.height)
        adb_command = f"adb -s {self.device} shell input tap {x} {y}"
        ret = execute_adb(adb_command)
        return ret

    def text(self, input_str):
        adb_command = f"adb -s {self.device} shell am broadcast -a ADB_INPUT_TEXT --es msg '{input_str}'"
        ret = execute_adb(adb_command)
        return ret

    def long_press(self, tl, br, duration=1000):
        x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x} {y} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def long_press_point(self, x: float, y: float, duration=1000):
        x = int(x * self.width)
        y = int(y * self.height)
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x} {y} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe(self, x, y, direction, dist="short", quick=False):
        unit_dist = int(self.width / 10)
        if dist == "long":
            unit_dist *= 3
        elif dist == "medium":
            unit_dist *= 2
        # x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
        if direction == "up":
            offset = 0, -2 * unit_dist
        elif direction == "down":
            offset = 0, 2 * unit_dist
        elif direction == "left":
            offset = -1 * unit_dist, 0
        elif direction == "right":
            offset = unit_dist, 0
        else:
            return "ERROR"
        duration = 100 if quick else 400
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x + offset[0]} {y + offset[1]} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe_point(self, start, end, duration=400):
        start_x, start_y = int(start[0] * self.width), int(start[1] * self.height)
        end_x, end_y = int(end[0] * self.width), int(end[1] * self.height)
        adb_command = f"adb -s {self.device} shell input swipe {start_x} {start_y} {end_x} {end_y} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def execute_action(self, action_type, bounds=None, text=""):
        if action_type == ActionType.BACK:
            self.back()
            return
        if bounds is None or len(bounds) < 4:
            # Fallback for input action without reliable target element.
            if action_type == ActionType.INPUT and text:
                self.text(text)
            return
        x = (bounds[0] + bounds[2]) // 2
        y = (bounds[1] + bounds[3]) // 2
        # print("{}:({},{}):{}".format(action_type.name, x, y, text))
        if action_type == ActionType.CLICK:
            self.tap_point(x, y)
        elif action_type == ActionType.LONG_CLICK:
            self.long_press_point(x, y)
        elif action_type == ActionType.SCROLL_LEFT_RIGHT:
            self.swipe(x, y, "right")
        elif action_type == ActionType.SCROLL_RIGHT_LEFT:
            self.swipe(x, y, "left")
        elif action_type == ActionType.SCROLL_TOP_DOWN:
            self.swipe(x, y, "down")
        elif action_type == ActionType.SCROLL_BOTTOM_UP:
            self.swipe(x, y, "up")
        elif action_type == ActionType.INPUT:
            self.tap_point(x, y)
            self.text(text)


def list_all_devices():
    adb_command = "adb devices"
    device_list = []
    result = execute_adb(adb_command)
    if result != "ERROR":
        devices = result.split("\n")[1:]
        for d in devices:
            device_list.append(d.split()[0])

    return device_list


if __name__ == "__main__":
    device = list_all_devices()[0]
    print(device)
    controller = AndroidController(device)
    controller.tap([864, 2077], [1080, 2224])
