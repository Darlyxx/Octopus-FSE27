"""UI reduction and concise prompts for the one-action device operator."""


ACTION_FORMATS = """[tap] [component]
[input] [component] [value]
[back]
[switch] [device_number] [message]
[end_task]
[nop]"""

prompt1 = f"""# Role
You operate one Android device to advance a fixed testing task.

# Rules
- Choose exactly one action from the current UI observation.
- Tap/input only a control shown in the observation; never invent a target.
- Use `switch` only when another participating device must act.
- Use `nop` only while waiting for an expected peer effect.
- Use `end_task` only when the task's observable conditions appear satisfied.
- The final line must be exactly one of:
{ACTION_FORMATS}
"""

def xml_align(xml):
    xml = str(xml or "").strip()
    if "<hierarchy" in xml:
        return xml
    declaration = '<?xml version="1.0" encoding="UTF-8"?>'
    if xml.startswith("<?xml"):
        declaration, xml = xml.split("?>", 1)
        declaration += "?>"
    return f'{declaration}<hierarchy rotation="0">{xml}</hierarchy>'

def _children(node):
    children = node.get("node", []) if isinstance(node, dict) else []
    if isinstance(children, dict):
        return [children]
    return children if isinstance(children, list) else []


def _label(node):
    return next((
        str(node.get(key, "")).strip()
        for key in ("@text", "@content-desc", "@resource-id")
        if str(node.get(key, "")).strip()
    ), "")


def _descendant_label(node):
    labels = []
    stack = list(_children(node))
    while stack:
        child = stack.pop(0)
        label = _label(child)
        if label and label not in labels:
            labels.append(label)
        stack.extend(_children(child))
    return " ".join(labels[:4])


def getMergedComponents(jsondata):
    """Flatten app nodes and give unlabeled clickable containers a readable label."""
    root = jsondata.get("hierarchy", {}) if isinstance(jsondata, dict) else {}
    result, stack = [], [root]
    while stack:
        node = stack.pop(0)
        stack.extend(_children(node))
        if not isinstance(node, dict) or "@resource-id" not in node:
            continue
        if "com.android.systemui" in str(node.get("@package", "")):
            continue
        item = dict(node)
        item.pop("node", None)
        if not _label(item) and item.get("@clickable") == "true":
            item["@text"] = _descendant_label(node)
        result.append(item)
    return result


def _component_lines(components):
    lines = []
    for component in components:
        identity = [
            f"{name}={str(component.get(key, '')).replace(chr(10), ' ')!r}"
            for name, key in (
                ("text", "@text"),
                ("content_desc", "@content-desc"),
                ("resource_id", "@resource-id"),
                ("class", "@class"),
            )
            if str(component.get(key, "")).strip()
        ]
        if not identity:
            continue
        flags = [
            f"clickable={component.get('@clickable', 'false')}",
            f"editable={component.get('@editable', 'false')}",
            f"enabled={component.get('@enabled', 'true')}",
        ]
        lines.append(f"- {', '.join(identity + flags)}")
    return "\n".join(dict.fromkeys(lines)) or "- No actionable controls observed."


def component_text(all_components):
    return _component_lines(all_components)


def component_prompt(activity_name, all_components):
    component_text = _component_lines(all_components)
    return f"\nForeground Activity: {activity_name}\nCurrent controls:\n{component_text}\n"


def _local_goal(sub_tasks, device):
    index = int(device) - 1
    return str(sub_tasks[index]) if 0 <= index < len(sub_tasks) else ""


def _history(records, device):
    local = [record for record in records if str(record.get("device_id")) == str(device)]
    if not local:
        return "No earlier local actions."
    return "\n".join(
        f"{index}. {record.get('action', '')}: {record.get('content', '')}"
        for index, record in enumerate(local[-12:], 1)
    )


def _context_prompt(overview_task, sub_tasks, roles, device, records):
    index = int(device) - 1
    role = roles[index] if 0 <= index < len(roles) else "default user"
    return (
        f"\nFixed task: {overview_task}\n"
        f"Current device: Device{device} ({role})\n"
        f"Local goal: {_local_goal(sub_tasks, device)}\n"
        f"Local Step Memory:\n{_history(records, device)}\n"
    )


def prompt2(sub_task_list, overview_task, device_type_list, de_num, memory_pool_list):
    return _context_prompt(
        overview_task, sub_task_list, device_type_list, de_num, memory_pool_list
    )
