"""Parse Android UI hierarchies into compact task context."""

import re

import xmltodict

from octopus import action_prompts
from octopus.services.coverage_store import CoverageStore


def state_signature(activity, components, package_name="", device_id=""):
    return CoverageStore.build_state_signature(
        device_id, package_name, activity, components
    )
def get_all_comps(xml: str):
    if "<hierarchy rotation=" in xml:
        align_xml = xml
    else:
        align_xml = action_prompts.xml_align(xml)
    xml_dict = xmltodict.parse(align_xml)
    all_comps = action_prompts.getMergedComponents(xml_dict)
    return action_prompts.component_text(all_comps)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clean_component_value(value: str):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text


def _resource_id_tail(value: str):
    text = _clean_component_value(value)
    if not text:
        return ""
    return text.split(":id/")[-1].split("/")[-1]


def _is_generic_resource_id(value: str):
    text = _resource_id_tail(value).lower()
    if not text:
        return True
    generic_parts = (
        "layout", "container", "root", "content", "list", "wrap", "divider",
        "toolbar", "scroller", "icon", "avatar", "image", "progress", "pager",
    )
    return any(part in text for part in generic_parts)


def _append_unique(values: list, value: str, max_items: int = 4):
    value = _clean_component_value(value)
    if not value or value in values:
        return
    if len(values) < max_items:
        values.append(value)


def _node_children(node: dict):
    if not isinstance(node, dict):
        return []
    return [child for child in _as_list(node.get("node")) if isinstance(child, dict)]


def _collect_descendant_component_fields(node: dict):
    texts = []
    descs = []
    resource_ids = []
    stack = [node]
    while stack:
        current = stack.pop(0)
        _append_unique(texts, current.get("@text", ""))
        _append_unique(descs, current.get("@content-desc", ""))
        resource_id = _resource_id_tail(current.get("@resource-id", ""))
        if resource_id and not _is_generic_resource_id(resource_id):
            _append_unique(resource_ids, resource_id)
        stack[0:0] = _node_children(current)
    return texts, descs, resource_ids


def _humanize_target(value):
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", str(value or ""))).strip()


def _normalized_target_text(value):
    return re.sub(r"\s+", " ", re.sub(
        r"[^\w]+", " ", _humanize_target(value).casefold(), flags=re.UNICODE
    )).strip()


def _target_display_label(text: str, content_desc: str, resource_id: str):
    text = _clean_component_value(text)
    content_desc = _clean_component_value(content_desc)
    resource_id = _resource_id_tail(resource_id)
    if text and content_desc:
        text_norm = _normalized_target_text(text)
        desc_norm = _normalized_target_text(content_desc)
        if re.search(r"\d", text) and not re.search(r"\d", content_desc):
            return content_desc
        if text_norm.startswith("my ") and desc_norm and desc_norm in text_norm:
            return text
        return text
    if text:
        return text
    if content_desc:
        return content_desc
    return _humanize_target(resource_id)


def _format_task_component_line(component: dict):
    fields = []
    for key in ("label", "content_desc", "resource_id", "class"):
        value = _clean_component_value(component.get(key, ""))
        if value:
            escaped = value.replace("'", "\\'")
            fields.append(f"{key}:'{escaped}'")
    fields.append(f"clickable={str(component.get('clickable', False)).lower()}")
    if component.get("editable"):
        fields.append("editable=true")
    return ", ".join(fields)


def get_task_ui_summary(xml: str):
    if "<hierarchy rotation=" in xml:
        align_xml = xml
    else:
        align_xml = action_prompts.xml_align(xml)
    xml_dict = xmltodict.parse(align_xml)
    root = xml_dict.get("hierarchy", {})
    stack = [root]
    components = []
    seen = set()
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        stack[0:0] = _node_children(node)
        package_name = str(node.get("@package", "") or "")
        if "com.android.systemui" in package_name:
            continue
        clickable = str(node.get("@clickable", "")).lower() == "true"
        editable = str(node.get("@editable", "")).lower() == "true"
        class_name = _clean_component_value(node.get("@class", ""))
        if not (clickable or editable or class_name.endswith("EditText")):
            continue
        texts, descs, resource_ids = _collect_descendant_component_fields(node)
        label = _target_display_label(
            texts[0] if texts else "",
            descs[0] if descs else "",
            resource_ids[0] if resource_ids else "",
        )
        if not label:
            continue
        resource_id = resource_ids[0] if resource_ids else ""
        signature = (
            _normalized_target_text(label),
            _normalized_target_text(descs[0] if descs else ""),
            _normalized_target_text(resource_id),
        )
        if signature in seen:
            continue
        seen.add(signature)
        components.append({
            "label": label,
            "content_desc": descs[0] if descs else "",
            "resource_id": resource_id,
            "class": class_name,
            "clickable": clickable,
            "editable": editable,
        })
    return "\n".join(_format_task_component_line(component) for component in components)


def compact_ui_summary(summary: str, max_total_chars: int = 1400, max_line_chars: int = 180):
    compact_lines = []
    for raw_line in str(summary or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if len(line) > max_line_chars:
            line = line[:max_line_chars] + "...(truncated)"
        compact_lines.append(line)
        if sum(len(item) + 1 for item in compact_lines) >= max_total_chars:
            compact_lines.append("...(truncated)")
            break
    return "\n".join(compact_lines)

def collect_device_contexts(device_ids: list, device_types: list, devices: dict, only=None):
    contexts = []
    for i, device_id in enumerate(device_ids):
        if only is not None and f"Device{i + 1}" != only:
            continue
        role_text = device_types[i] if i < len(device_types) else "default user"
        try:
            device = devices[f"d{i + 1}"]
            xml = device.dump_hierarchy(compressed=False, pretty=False)
            app_info = device.app_current()
            package_name = app_info.get("package", "unknown")
            activity_name = app_info.get("activity", "unknown")
            state_ui_summary = get_task_ui_summary(xml)
            comp_summary = compact_ui_summary(state_ui_summary)

            context_item = {
                "device_index": i + 1,
                "device_id": device_id,
                "role": role_text,
                "package": package_name,
                "activity": activity_name,
                "ui_summary": comp_summary,
                "state_ui_summary": state_ui_summary,
                "all_comps": get_all_comps(xml),
            }
            contexts.append(context_item)
        except Exception as e:
            contexts.append({
                "device_index": i + 1,
                "device_id": device_id,
                "role": role_text,
                "context_collection_error": str(e)
            })
    return contexts
