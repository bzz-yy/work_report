"""Explicit monthly field adapters for approved blank DOCX templates.

This is a location/token filler, not a collector or a template inference engine.
The caller must validate_collection against the base plan before invoking it and
must separately approve blank-template cleanup and final rendered pages.
"""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import tempfile
from zipfile import ZipFile

from lxml import etree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
TAG = lambda name: "{" + W + "}" + name
PART = re.compile(r"word/(?:document|header\d+|footer\d+)\.xml\Z")
TOKEN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_.-]{0,63})\}\}")
ANY_TOKEN = re.compile(r"\{\{[^{}]*\}\}")
XPATH = re.compile(r"/w:[A-Za-z]+(?:/w:[A-Za-z]+\[\d+\])*")
FILLABLE = {"real", "request", "config", "derived", "fixed"}
BASE_TEMPLATE = "NW-MONTHLY-STD-01"


def _mapping_fields(mapping):
    fields = mapping.get("fields")
    if isinstance(fields, dict):
        result = list(fields)
    elif isinstance(fields, list) and all(isinstance(f, dict) for f in fields):
        result = [f.get("id", f.get("field_id")) for f in fields]
        for field in fields:
            fid = field.get("id", field.get("field_id"))
            if field.get("placeholder", fid) not in {fid, "{{" + str(fid) + "}}"}:
                raise ValueError("字段placeholder与局部ID不同")
    else:
        raise ValueError("模板字段映射fields结构无效")
    if (not result or any(not isinstance(fid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", fid) for fid in result)
            or len(result) != len(set(result))):
        raise ValueError("模板字段ID缺失、非法或重复")
    return set(result)


def _rules_fields(rules):
    fields = rules.get("fields")
    if not isinstance(fields, dict) or not fields or any(not isinstance(v, dict) for v in fields.values()):
        raise ValueError("通用规则fields须为非空对象")
    for fid, field in fields.items():
        if field.get("field_id", fid) != fid:
            raise ValueError("规则field_id与键不一致：" + fid)
    return fields


def validate_adapter(new_rules, new_mapping, base_rules, base_mapping):
    """Validate explicit IDs, semantic dimensions, units and record group scope.

    Raises ValueError on incompatibility. No name/number similarity is used.
    Actual DOCX positions and hashes are checked by fill_mapped.
    """
    if new_rules.get('template_fixed_content') is not None:
        raise ValueError('映射变体的模板固定内容须显式适配局部位置；当前不能继承基底固定内容')
    if base_rules.get("template_id") != BASE_TEMPLATE or base_mapping.get("template_id") != BASE_TEMPLATE:
        raise ValueError("映射执行器只支持明确的NW-MONTHLY-STD-01基准")
    tid = new_rules.get("template_id")
    if not tid or tid == BASE_TEMPLATE or new_mapping.get("template_id") != tid:
        raise ValueError("新模板及规则ID不一致，或错误覆盖基准模板")
    adapter = new_rules.get("adapter", new_mapping.get("adapter"))
    if not isinstance(adapter, dict) or adapter.get("id") != "monthly_mapped_v1" or adapter.get("base_template_id") != BASE_TEMPLATE:
        raise ValueError("缺少明确monthly_mapped_v1执行适配声明")
    if "adapter" in new_rules and "adapter" in new_mapping and new_rules["adapter"] != new_mapping["adapter"]:
        raise ValueError("映射与规则的adapter声明冲突")
    layout = adapter.get("layout", {})
    if (not isinstance(layout, dict) or set(layout) - {"collapse_empty_terminal_section"}
            or any(type(value) is not bool for value in layout.values())):
        raise ValueError("adapter.layout只支持布尔collapse_empty_terminal_section")
    local, base = _rules_fields(new_rules), _rules_fields(base_rules)
    if _mapping_fields(new_mapping) != set(local) or _mapping_fields(base_mapping) != set(base):
        raise ValueError("模板映射与规则未覆盖相同字段")
    bindings = adapter.get("field_bindings")
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("field_bindings须明确至少一项可复用基准字段")
    for fid, bid in bindings.items():
        if fid not in local or not isinstance(bid, str) or bid not in base:
            raise ValueError("映射引用未知局部字段或基准字段：" + str(fid))
        ours, theirs = local[fid].get("data_definition", {}), base[bid].get("data_definition", {})
        if not ours.get("standard_id") or ours.get("standard_id") != theirs.get("standard_id"):
            raise ValueError("公共D定义不同，不能按名称或F号复用：" + fid + "/" + bid)
        op, bp = ours.get("parameters", {}), theirs.get("parameters", {})
        if not isinstance(op, dict) or not isinstance(bp, dict) or op != bp:
            raise ValueError("缺少或更改必要语义参数：" + fid + "/" + bid)
        if any(not isinstance(v, str) or not v for v in op.values()):
            raise ValueError("语义参数必须有明确取值：" + fid)
        source_ids = ours.get("input_mapping_ids", [])
        base_source_ids = theirs.get("input_mapping_ids", [])
        if (not isinstance(source_ids, list) or any(not isinstance(x, str) for x in source_ids)
                or len(source_ids) != len(set(source_ids))):
            raise ValueError("input_mapping_ids结构无效：" + fid)
        if source_ids and not set(base_source_ids) <= set(source_ids):
            raise ValueError("新字段指定的SM不覆盖基底采用的来源，不能隐式更换取法：" + fid)
        for selection_key in ("source_selection", "attachment_selection", "record_selection", "plan_selection", "photo_selection"):
            selected = local[fid].get(selection_key)
            inherited = base[bid].get(selection_key)
            if selected and selected != inherited:
                raise ValueError("新字段来源采用或记录筛选与基底角色冲突：" + fid + "/" + selection_key)
        if local[fid].get("record_source_selection"):
            raise ValueError("未支持的记录筛选键record_source_selection，请使用record_selection")
        unit, base_unit = local[fid].get("filling_rule", {}).get("unit"), base[bid].get("filling_rule", {}).get("unit")
        if not isinstance(unit, str) or not unit or unit != base_unit:
            raise ValueError("显示单位不同或未明确，当前映射执行器不隐式换算：" + fid)
    for fid, field in local.items():
        display = field.get("display")
        if display is not None:
            if (not isinstance(display, dict) or set(display) != {"suffix"}
                    or not isinstance(display["suffix"], str) or not display["suffix"].strip()
                    or display["suffix"].strip() != field.get("filling_rule", {}).get("unit")):
                raise ValueError("显示后缀须为明确报告单位，其他显示规则尚未支持：" + fid)
    base_groups = {}
    base_membership = {}
    for group in base_mapping.get("repeat_groups", []):
        key, fids = group.get("key"), group.get("field_ids", [])
        if not key or key in base_groups or not fids or len(fids) != len(set(fids)) or not set(fids) <= set(base):
            raise ValueError("基准循环组声明无效")
        base_groups[key] = group
        for bid in fids:
            if bid in base_membership:
                raise ValueError("基准字段属于多个循环组")
            base_membership[bid] = key
    groups = adapter.get("repeat_groups", [])
    if not isinstance(groups, list):
        raise ValueError("repeat_groups须为列表")
    seen_keys, seen_rows, membership = set(), set(), {}
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("循环组须为对象")
        key, fids, loc = group.get("key"), group.get("field_ids"), group.get("row_location", {})
        if key not in base_groups or key in seen_keys:
            raise ValueError("循环组未知或重复：" + str(key))
        if not isinstance(fids, list) or not fids or any(not isinstance(f, str) for f in fids) or len(fids) != len(set(fids)):
            raise ValueError("循环组局部字段为空或重复：" + key)
        if not set(fids) <= set(bindings):
            raise ValueError("循环列必须显式绑定基准字段；手填新增列暂不扩行：" + key)
        if any(base_membership.get(bindings[f]) != key for f in fids):
            raise ValueError("循环列不属于所声明基准组：" + key)
        if not isinstance(loc, dict) or not isinstance(loc.get("part"), str) or not PART.fullmatch(loc["part"]):
            raise ValueError("循环原型部件无效")
        if not isinstance(loc.get("xpath"), str) or not XPATH.fullmatch(loc["xpath"]) or not re.search(r"/w:tr\[\d+\]$", loc["xpath"]):
            raise ValueError("循环原型须定位到明确Word表格行")
        row_id = (loc["part"], loc["xpath"])
        if row_id in seen_rows:
            raise ValueError("同一原型行不能属于多个循环组")
        seen_keys.add(key)
        seen_rows.add(row_id)
        for fid in fids:
            if fid in membership:
                raise ValueError("局部循环字段不能跨组复用")
            membership[fid] = key
    for fid, bid in bindings.items():
        if (bid in base_membership) != (fid in membership):
            raise ValueError("基准循环列不能当单值，也不能遗漏循环声明：" + fid)
    return {"valid": True, "adapter_id": "monthly_mapped_v1", "base_template_id": BASE_TEMPLATE,
            "field_bindings": deepcopy(bindings), "manual_fields": sorted(set(local) - set(bindings)),
            "photo_fields": sorted(fid for fid, bid in bindings.items() if bid == "F090" or base[bid].get("filling_rule", {}).get("kind") == "图片"),
            "repeat_groups": deepcopy(groups), "repeat_membership": membership, "layout": deepcopy(layout)}


def _paragraph_nodes(paragraph):
    return [node for node in paragraph.iter(TAG("t"))
            if next((p for p in node.iterancestors() if p.tag == TAG("p")), None) is paragraph]


def _paragraph_segments(paragraph):
    """Match inspect offsets: t text, one character per tab/br/cr control."""
    segments, cursor = [], 0
    for node in paragraph.iter():
        if node.tag not in {TAG("t"), TAG("tab"), TAG("br"), TAG("cr")}:
            continue
        if next((p for p in node.iterancestors() if p.tag == TAG("p")), None) is not paragraph:
            continue
        value = (node.text or "") if node.tag == TAG("t") else ("\t" if node.tag == TAG("tab") else "\n")
        segments.append((cursor, cursor + len(value), node, value))
        cursor += len(value)
    return segments


def _text(paragraph):
    return "".join(value for _, _, _, value in _paragraph_segments(paragraph))


def _paragraphs(root):
    return list(root.iter(TAG("p")))


def _tokens(paragraph):
    text = _text(paragraph)
    if re.search(r"\{\{[^{}]*[\n\t][^{}]*\}\}", text):
        raise ValueError("占位token本体不能跨越换行或制表控制节点")
    tokens = TOKEN.findall(text)
    residual = TOKEN.sub("", text)
    if ANY_TOKEN.search(residual) or "{{" in residual or "}}" in residual:
        raise ValueError("模板有非法或未知格式的占位token")
    return tokens


def _resolve(roots, loc, expected):
    if not isinstance(loc, dict) or loc.get("part") not in roots or not isinstance(loc.get("xpath"), str) or not XPATH.fullmatch(loc["xpath"]):
        raise ValueError("模板位置非法或部件不存在")
    nodes = roots[loc["part"]].xpath(loc["xpath"], namespaces=NS)
    if len(nodes) != 1 or nodes[0].tag != TAG(expected):
        raise ValueError("模板位置不是唯一的" + expected)
    return nodes[0]


def _display(item, label, photo=False):
    if photo:
        return "【待填：当期现场照片】", False, "manual"
    if not isinstance(item, dict) or item.get("status") not in FILLABLE or item.get("value") is None or item.get("value") == "":
        return "【待填：" + label + "】", False, (item.get("status", "missing") if isinstance(item, dict) else "missing")
    value = item["value"]
    if isinstance(value, (bool, dict, list, tuple)) or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("已校验取数结果不应包含非单值或非有限数值：" + label)
    return format(value, ".12g") if isinstance(value, float) else str(value), True, item["status"]


def _result_item(fid, item, fields, bindings, photo_fields):
    field = fields[fid]
    label = field.get("filling_rule", {}).get("label", fid)
    _, available, status = _display(item, label, fid in photo_fields)
    definition = field.get("data_definition", {})
    return {"field_id": fid, "label": label, "base_field_id": bindings.get(fid),
            "standard_id": definition.get("standard_id"), "parameters": deepcopy(definition.get("parameters", {})),
            "unit": field.get("filling_rule", {}).get("unit"),
            "value": deepcopy(item["value"]) if available else None,
            "status": status if available else ("manual" if fid in photo_fields or fid not in bindings else "missing"),
            "source": deepcopy(item.get("source")) if isinstance(item, dict) else None,
            "station_id": item.get("station_id") if isinstance(item, dict) else None,
            "period": item.get("period") if isinstance(item, dict) else None,
            "filled": available, "reason": (None if available else (
                "照片位置暂由人工处理" if fid in photo_fields else
                item.get("reason", "未取得通过校验的本次值") if isinstance(item, dict) else "未取得通过校验的本次值"))}


def _replace_paragraph(paragraph, values, fields, used, photo_fields):
    text = _text(paragraph)
    offsets = [(start, end, node) for start, end, node, _ in _paragraph_segments(paragraph)]
    matches = list(TOKEN.finditer(text))
    if matches and paragraph.xpath(".//w:fldChar | .//w:instrText | .//w:fldSimple | .//w:del | .//w:ins", namespaces=NS):
        raise ValueError("占位段落含Word域或修订，须先处理模板")
    for match in reversed(matches):
        fid = match.group(1)
        if fid not in fields:
            raise ValueError("模板包含未登记字段：" + fid)
        label = fields[fid].get("filling_rule", {}).get("label", fid)
        rendered, available, status = _display(values.get(fid), label, fid in photo_fields)
        if not available and any(a.tag == TAG("tc") for a in paragraph.iterancestors()):
            rendered = "照片待补" if fid in photo_fields else "待填"
        suffix=fields[fid].get("display",{}).get("suffix")
        if available and suffix and not rendered.endswith(suffix):
            rendered += suffix
        touched = [(a, b, node) for a, b, node in offsets if a < match.end() and b > match.start()]
        if any(node.tag != TAG("t") for _, _, node in touched):
            raise ValueError("占位token本体不能跨越换行或制表控制节点：" + fid)
        if not touched or any(node.getparent().tag != TAG("r") for _, _, node in touched):
            raise ValueError("token不在支持的文本run中：" + fid)
        first_at, _, first = touched[0]
        prefix = (first.text or "")[:match.start() - first_at]
        suffix = (first.text or "")[match.end() - first_at:] if len(touched) == 1 else ""
        first.text = prefix + rendered + suffix
        first.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        for start, _, node in touched[1:]:
            node.text = (node.text or "")[max(0, match.end() - start):]
        used.append({"field_id": fid, "filled": available, "status": status})
    for node in _paragraph_nodes(paragraph):
        if "\n" not in (node.text or ""):
            continue
        parts = node.text.split("\n")
        node.text = parts[0]
        parent, at = node.getparent(), node.getparent().index(node)
        for index, value in enumerate(parts[1:]):
            parent.insert(at + 1 + index * 2, ET.Element(TAG("br")))
            new = ET.Element(TAG("t"))
            new.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            new.text = value
            parent.insert(at + 2 + index * 2, new)


def _clean_trailing_empty_paragraphs(root):
    """Remove only empty body-tail paragraphs; never a table or section."""
    body = root.find(TAG("body"))
    removed = 0
    if body is None:
        return removed
    children = list(body)
    if children and children[-1].tag == TAG("sectPr"):
        children.pop()  # Keep the document's final section properties verbatim.
    protected = ("sectPr", "drawing", "pict", "object", "fldChar", "instrText", "fldSimple",
                 "bookmarkStart", "bookmarkEnd", "sym", "noBreakHyphen", "softHyphen",
                 "footnoteReference", "endnoteReference", "commentReference", "pBdr",
                 "del", "ins", "sdt", "altChunk", "customXml", "permStart", "permEnd")
    for paragraph in reversed(children):
        if paragraph.tag != TAG("p") or _text(paragraph).strip():
            break
        if any(next(paragraph.iter(TAG(name)), None) is not None for name in protected):
            break
        if any(isinstance(node.tag, str) and ET.QName(node).namespace != W for node in paragraph.iter()):
            break  # Math/alternate rendering content cannot be classified as empty text.
        # Whitespace, tabs, ordinary breaks, page breaks and paragraph formatting
        # alone do not make a trailing empty paragraph business content.
        body.remove(paragraph)
        removed += 1
    return removed


def _empty_terminal_paragraph(paragraph, allow_section=False):
    if paragraph.tag != TAG("p") or _text(paragraph).strip():
        return False
    protected = ("drawing", "pict", "object", "fldChar", "instrText", "fldSimple", "bookmarkStart",
                 "bookmarkEnd", "sym", "noBreakHyphen", "softHyphen", "footnoteReference", "endnoteReference",
                 "commentReference", "pBdr", "del", "ins", "sdt", "altChunk", "customXml", "permStart", "permEnd", "tbl")
    if any(next(paragraph.iter(TAG(name)), None) is not None for name in protected):
        return False
    if not allow_section and next(paragraph.iter(TAG("sectPr")), None) is not None:
        return False
    return not any(isinstance(n.tag, str) and ET.QName(n).namespace != W for n in paragraph.iter())


def _section_shape(element):
    if element is None:
        return None
    return (element.tag, tuple(sorted(element.attrib.items())), (element.text or "").strip(),
            tuple(_section_shape(child) for child in element if isinstance(child.tag, str)))


def _collapse_empty_terminal_section(root):
    """Change only an explicitly approved, unused final section to continuous."""
    body = root.find(TAG("body"))
    if body is None or not len(body) or body[-1].tag != TAG("sectPr"):
        raise ValueError("末空节折叠要求明确的body末尾sectPr")
    final = body[-1]
    boundaries = [(index, child) for index, child in enumerate(body[:-1])
                  if child.tag == TAG("p") and child.find("w:pPr/w:sectPr", NS) is not None]
    if not boundaries:
        raise ValueError("没有可识别的末空节边界，不能折叠单一正文节")
    index, boundary = boundaries[-1]
    if not _empty_terminal_paragraph(boundary, allow_section=True):
        raise ValueError("末节边界段含内容/签字/字段/书签，不能折叠")
    if any(not _empty_terminal_paragraph(child) for child in list(body)[index + 1:-1]):
        raise ValueError("末节仍有正文/表格/图片/签字/字段/书签，不能折叠")
    previous = boundary.find("w:pPr/w:sectPr", NS)
    if final.findall(TAG("headerReference")) or final.findall(TAG("footerReference")):
        raise ValueError("末节含独立页眉或页脚引用，不能折叠")
    numbering = final.find(TAG("pgNumType"))
    if numbering is not None and numbering.get(TAG("start")) is not None:
        raise ValueError("末节有页码重启，不能折叠")
    if final.find(TAG("titlePg")) is not None:
        raise ValueError("末节具有独立首页用途，不能折叠")
    section_type = final.find(TAG("type"))
    before = section_type.get(TAG("val")) if section_type is not None else None
    if before not in (None, "nextPage", "continuous"):
        raise ValueError("末节为特殊分节类型，不能折叠：" + str(before))
    for name in ("pgSz", "cols"):
        if _section_shape(final.find(TAG(name))) != _section_shape(previous.find(TAG(name))):
            raise ValueError("末节纸张或列结构与前节不同，不能折叠：" + name)
    if section_type is None:
        section_type = ET.Element(TAG("type"))
        earlier = {TAG(name) for name in ("headerReference", "footerReference", "footnotePr", "endnotePr")}
        at = next((i for i, child in enumerate(final) if child.tag not in earlier), len(final))
        final.insert(at, section_type)
    section_type.set(TAG("val"), "continuous")
    return {"collapsed_empty_terminal_section": before != "continuous",
            "terminal_section_type_before": before, "terminal_section_type_after": "continuous",
            "terminal_section_preserved": True}


def _local_field_spans(paragraph):
    """Identify complete local fields, leaving unmatched outer controls alone."""
    found, stack = [], []
    for node in paragraph.iter():
        if node.tag == TAG("fldSimple"):
            instruction = node.get(TAG("instr"), "")
            found.append({"instruction": instruction, "texts": list(node.iter(TAG("t"))),
                          "controls": {node}, "complete": True})
        if any(a.tag == TAG("fldSimple") for a in node.iterancestors() if a is not paragraph):
            continue
        if node.tag == TAG("fldChar"):
            kind = node.get(TAG("fldCharType"))
            if kind == "begin":
                stack.append({"instruction": "", "texts": [], "controls": {node}, "result": False})
            elif stack and kind == "separate":
                stack[-1]["controls"].add(node)
                stack[-1]["result"] = True
            elif stack and kind == "end":
                field = stack.pop()
                field["controls"].add(node)
                found.append({**field, "complete": True})
        elif node.tag == TAG("instrText") and stack:
            stack[-1]["instruction"] += node.text or ""
            stack[-1]["controls"].add(node)
        elif node.tag == TAG("t") and stack and stack[-1]["result"]:
            stack[-1]["texts"].append(node)
    # Broken/incomplete fields cannot justify deleting a directory entry; their
    # already-identifiable cached result can still be cleared safely.
    for field in stack:
        found.append({**field, "complete": False})
    return found


def _page_ref_fields(paragraph):
    return [field for field in _local_field_spans(paragraph)
            if re.match(r"\s*PAGEREF(?:\s|$)", field["instruction"], re.I)]


def _clear_invalid_toc_item(paragraph, page_fields):
    """Clear a dead item while preserving the surrounding TOC field skeleton.

    A final TOC paragraph may contain a complete local HYPERLINK/PAGEREF pair
    followed by the unmatched end of the TOC field begun in an earlier paragraph.
    Removing that paragraph would break the whole directory. Remove only proven
    local link/page fields and their visible result; never their outer controls.
    """
    if not all(field["complete"] for field in page_fields):
        return False
    if paragraph.xpath(".//w:sectPr | .//w:drawing | .//w:pict | .//w:object | .//w:tbl", namespaces=NS):
        return False
    if any(isinstance(node.tag, str) and ET.QName(node).namespace != W for node in paragraph.iter()):
        return False
    local = _local_field_spans(paragraph)
    removable = set()
    for field in local:
        match = re.match(r"\s*([A-Za-z]+)(?:\s|$)", field["instruction"])
        command = match.group(1).upper() if match else None
        if command == "TOC":
            continue  # Even a locally complete outer TOC is structural, not an item.
        if command not in {"HYPERLINK", "PAGEREF"} or not field["complete"]:
            return False
        removable.update(field["controls"])
    visible = {TAG(name) for name in ("t", "tab", "br", "cr", "sym", "noBreakHyphen", "softHyphen")}
    removable.update(node for node in paragraph.iter() if node.tag in visible)
    for node in removable:
        if node.getparent() is not None:
            node.getparent().remove(node)
    return True


def _clean_toc_caches(root):
    sdts = root.xpath(".//w:sdt[w:sdtPr/w:docPartObj/w:docPartGallery[@w:val='Table of Contents']]", namespaces=NS)
    toc_set = set(sdts)
    bookmarks = {node.get(TAG("name")) for node in root.iter(TAG("bookmarkStart"))
                 if not any(ancestor in toc_set for ancestor in node.iterancestors())}
    cleared, removed, custom, blocked, cleared_items = 0, 0, 0, 0, 0
    for sdt in sdts:
        content = sdt.find(TAG("sdtContent"))
        if content is None:
            continue
        for paragraph in list(content.iter(TAG("p"))):
            fields = _page_ref_fields(paragraph)
            if not fields:
                if _text(paragraph).strip():
                    custom += 1
                continue
            targets = []
            for field in fields:
                match = re.match(r'\s*PAGEREF\s+(?:"([^"\s]+)"|([^\s\\]+))', field["instruction"], re.I)
                targets.append((match.group(1) or match.group(2)) if match else None)
            controls = set(paragraph.xpath(".//w:fldChar | .//w:instrText | .//w:fldSimple", namespaces=NS))
            owned = set().union(*(f["controls"] for f in fields))
            missing_target = bool(targets) and all(t and t not in bookmarks for t in targets)
            # Do not remove a row, nested table or a paragraph carrying the TOC's
            # surrounding begin/end field controls or meaningful bookmarks.
            can_remove = (paragraph.getparent() is content and controls <= owned
                          and all(f["complete"] for f in fields)
                          and not paragraph.xpath(".//w:bookmarkStart | .//w:bookmarkEnd | .//w:sectPr | .//w:drawing | .//w:pict | .//w:object", namespaces=NS))
            if missing_target and can_remove:
                content.remove(paragraph)
                removed += 1
                continue
            if missing_target and _clear_invalid_toc_item(paragraph, fields):
                cleared_items += 1
                continue
            if missing_target:
                blocked += 1
            for field in fields:
                texts = field["texts"]
                if texts:
                    texts[0].text = "待更新"
                    for node in texts[1:]:
                        node.text = ""
                    cleared += 1
    return {"toc_cache_cleared": cleared, "removed_missing_bookmark_toc_entries": removed,
            "cleared_missing_bookmark_toc_items_preserving_outer_fields": cleared_items,
            "custom_toc_entries_pending_review": custom,
            "missing_targets_retained_for_field_integrity": blocked}


def _settings_patches(payload):
    """Enable field refresh, adding the optional settings part if absent."""
    parser = ET.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    settings = (ET.fromstring(payload["word/settings.xml"], parser)
                if "word/settings.xml" in payload else ET.Element(TAG("settings"), nsmap={"w": W}))
    update = settings.find(TAG("updateFields"))
    if update is None:
        update = ET.SubElement(settings, TAG("updateFields"))
    update.set(TAG("val"), "true")
    patches = {"word/settings.xml": ET.tostring(settings, xml_declaration=True, encoding="UTF-8", standalone=True)}
    if "word/settings.xml" not in payload:
        types_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
        rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        types = ET.fromstring(payload["[Content_Types].xml"], parser)
        if not any(n.get("PartName") == "/word/settings.xml" for n in types):
            ET.SubElement(types, "{" + types_ns + "}Override", PartName="/word/settings.xml",
                          ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml")
        patches["[Content_Types].xml"] = ET.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True)
        rel_path = "word/_rels/document.xml.rels"
        relations = (ET.fromstring(payload[rel_path], parser) if rel_path in payload
                     else ET.Element("{" + rel_ns + "}Relationships", nsmap={None: rel_ns}))
        rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
        if not any(n.get("Type") == rel_type for n in relations):
            ids, number = {n.get("Id") for n in relations}, 1
            while "rIdMappedSettings" + str(number) in ids:
                number += 1
            ET.SubElement(relations, "{" + rel_ns + "}Relationship", Id="rIdMappedSettings" + str(number),
                          Type=rel_type, Target="settings.xml")
        patches[rel_path] = ET.tostring(relations, xml_declaration=True, encoding="UTF-8", standalone=True)
    return patches


def fill_mapped(plan, collected, new_template, new_mapping, new_rules, out_docx):
    """Fill an approved blank with already-validated base collection values.

    plan is the base collector plan, including template_rules_file/mapping_file.
    The new DOCX is the only package copied. Output creation is exclusive/atomic.
    """
    template, output = Path(new_template).resolve(), Path(out_docx).resolve()
    if output == template or output.exists() or output.suffix.lower() != ".docx":
        raise ValueError("输出必须是新的DOCX，不能覆盖模板或既有报告")
    base_rules = json.loads(Path(plan["template_rules_file"]).read_text(encoding="utf-8"))
    base_mapping = json.loads(Path(plan["mapping_file"]).read_text(encoding="utf-8"))
    checked = validate_adapter(new_rules, new_mapping, base_rules, base_mapping)
    if plan.get("template_id") != BASE_TEMPLATE:
        raise ValueError("取数plan不是声明的基准模板")
    if collected.get("station_id") != plan.get("station_id") or collected.get("period") != plan.get("period"):
        raise ValueError("取数结果与基准plan的电站或期间不一致")
    if collected.get("template_id") not in (None, BASE_TEMPLATE):
        raise ValueError("取数结果不是基准模板的字段空间")
    raw = template.read_bytes()
    digest = sha256(raw).hexdigest()
    if digest != new_mapping.get("template_sha256"):
        raise ValueError("新模板摘要变化，须重新检查位置映射")
    with ZipFile(template) as archive:
        infos = archive.infolist()
        if len(infos) != len(set(i.filename for i in infos)):
            raise ValueError("DOCX包含重复ZIP部件")
        payload = {i.filename: archive.read(i) for i in infos}
        comment = archive.comment
    roots = {}
    for name, content in payload.items():
        if PART.fullmatch(name):
            parser = ET.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
            root = ET.fromstring(content, parser)
            if root.getroottree().docinfo.doctype:
                raise ValueError("模板不支持XML实体声明")
            roots[name] = root
    if "word/document.xml" not in roots:
        raise ValueError("新模板缺少Word正文")
    fields, bindings = new_rules["fields"], checked["field_bindings"]
    occurrences, locations = Counter(), {}
    for part, root in roots.items():
        for paragraph in _paragraphs(root):
            tokens = _tokens(paragraph)
            occurrences.update(tokens)
            for fid in tokens:
                locations.setdefault(fid, []).append(paragraph)
    if set(occurrences) != set(fields):
        raise ValueError("模板token与字段未全覆盖：" + ",".join(sorted(set(occurrences) ^ set(fields))))
    if "positions" in new_mapping:
        expected = Counter()
        seen = set()
        for position in new_mapping["positions"]:
            token = position.get("placeholder")
            if not isinstance(token, str) or not TOKEN.fullmatch(token):
                raise ValueError("位置映射包含非法placeholder")
            loc = position.get("location", {})
            paragraph = _resolve(roots, loc, "p")
            if "start" in position or "end" in position:
                start, end = position.get("start"), position.get("end")
                if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(_text(paragraph))) or _text(paragraph)[start:end] != token:
                    raise ValueError("新空模板的位置区间与token不一致")
            key = (loc["part"], loc["xpath"], position.get("start"), position.get("end"), token)
            if key in seen:
                raise ValueError("重复登记同一个token位置")
            seen.add(key)
            expected[(id(paragraph), token[2:-2])] += 1
        actual = Counter((id(paragraph), fid) for fid, paragraphs in locations.items() for paragraph in paragraphs)
        if expected != actual:
            raise ValueError("位置映射未准确覆盖每次token出现，包括重复位置")
    slots, group_paragraphs = [], set()
    for group in checked["repeat_groups"]:
        row = _resolve(roots, group["row_location"], "tr")
        if row.getparent().tag != TAG("tbl"):
            raise ValueError("循环原型必须是表格直属行")
        # A prototype with old signatures/photos, nested rows or cross-row merge
        # cannot safely be copied merely because its text tokens were mapped.
        if row.xpath(".//w:drawing | .//w:pict | .//w:object | .//w:tbl | .//w:vMerge | .//w:fldChar | .//w:instrText", namespaces=NS):
            raise ValueError("循环原型含图片/签名候选、嵌套表格、域或纵向合并；须先适配")
        paragraphs = _paragraphs(row)
        row_fields = {fid for paragraph in paragraphs for fid in _tokens(paragraph)}
        if row_fields != set(group["field_ids"]):
            raise ValueError("循环原型token与声明字段不一致：" + group["key"])
        for fid in group["field_ids"]:
            if any(p not in paragraphs for p in locations[fid]):
                raise ValueError("循环字段出现在原型外，不能把多条记录填成一个值：" + fid)
        if any(id(p) in group_paragraphs for p in paragraphs):
            raise ValueError("循环原型嵌套或重叠")
        group_paragraphs.update(id(p) for p in paragraphs)
        slots.append((group, row))
    used, counts, placeholders, record_results = [], {}, {}, {}
    for group, prototype in slots:
        key = group["key"]
        block = collected.get("repeat_groups", {}).get(key, {})
        records = block.get("records", []) if isinstance(block, dict) else None
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise ValueError("已校验循环记录结构无效：" + key)
        counts[key], placeholders[key] = len(records), not bool(records)
        record_results[key] = []
        table, at = prototype.getparent(), prototype.getparent().index(prototype)
        for index, record in enumerate(records or [{}]):
            row = deepcopy(prototype)
            for node in row.iter():
                for attr in list(node.attrib):
                    if attr.endswith("}paraId") or attr.endswith("}textId"):
                        del node.attrib[attr]
            for bookmark in row.xpath(".//w:bookmarkStart | .//w:bookmarkEnd", namespaces=NS):
                bookmark.getparent().remove(bookmark)
            for height in row.xpath("w:trPr/w:trHeight[@w:hRule='exact']", namespaces=NS):
                height.set(TAG("hRule"), "atLeast")
            values = {}
            for fid in group["field_ids"]:
                bid = bindings[fid]
                value = record.get(bid)
                inherited = {"status": block.get("status", "missing"), "value": None,
                             "source": deepcopy(block.get("source")), "station_id": block.get("station_id"),
                             "period": block.get("period")}
                inherited.update(value if isinstance(value, dict) else {"value": value})
                values[fid] = inherited
            record_results[key].append({fid: _result_item(fid, values.get(fid), fields, bindings, checked["photo_fields"])
                                        for fid in group["field_ids"]})
            for paragraph in _paragraphs(row):
                _replace_paragraph(paragraph, values, fields, used, checked["photo_fields"])
            table.insert(at + index, row)
        table.remove(prototype)
    scalar = {fid: collected.get("fields", {}).get(bid) for fid, bid in bindings.items()
              if fid not in checked["repeat_membership"]}
    field_results = {fid: {**_result_item(fid, scalar.get(fid), fields, bindings, checked["photo_fields"]),
                          "location_kind": "scalar"} for fid in fields if fid not in checked["repeat_membership"]}
    for fid, key in checked["repeat_membership"].items():
        field_results[fid] = {**_result_item(fid, None, fields, bindings, checked["photo_fields"]),
                              "location_kind": "record", "group": key, "status": "record_collection",
                              "record_count": counts[key], "filled": any(r[fid]["filled"] for r in record_results[key])}
    for root in roots.values():
        for paragraph in _paragraphs(root):
            if TOKEN.search(_text(paragraph)):
                _replace_paragraph(paragraph, scalar, fields, used, checked["photo_fields"])
    if set(fields) != {item["field_id"] for item in used}:
        raise ValueError("填充后仍有未处理的映射字段")
    for root in roots.values():
        if any(_tokens(p) for p in _paragraphs(root)):
            raise ValueError("填充后仍有未处理token")
    toc_cleanup = _clean_toc_caches(roots["word/document.xml"])
    layout_cleanup = {"removed_trailing_empty_paragraphs": _clean_trailing_empty_paragraphs(roots["word/document.xml"]),
                      **{key: value for key, value in toc_cleanup.items() if key != "toc_cache_cleared"}}
    if checked["layout"].get("collapse_empty_terminal_section"):
        layout_cleanup.update(_collapse_empty_terminal_section(roots["word/document.xml"]))
    patches = {name: ET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True) for name, root in roots.items()}
    patches.update(_settings_patches(payload))
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mapped-", suffix=".docx", dir=output.parent)
    os.close(fd)
    try:
        with ZipFile(temporary, "w") as target:
            target.comment = comment
            for info in infos:
                target.writestr(deepcopy(info), patches.get(info.filename, payload[info.filename]))
            for name, content in patches.items():
                if name not in payload:
                    target.writestr(name, content)
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    missing = sorted({i["field_id"] for i in used if not i["filled"]})
    mapped = sorted({i["field_id"] for i in used if i["filled"]})
    return {"docx": str(output), "docx_sha256": sha256(output.read_bytes()).hexdigest(),
            "template_sha256": digest, "adapter_id": "monthly_mapped_v1",
            "mapped_fields": mapped, "missing_fields": missing, "record_counts": counts,
            "empty_group_placeholder_rows": placeholders, "photos": {"embedded": 0, "status": "manual_pending"},
            "field_results": field_results, "record_results": record_results,
            "filled_occurrences": sum(i["filled"] for i in used),
            "placeholder_occurrences": sum(not i["filled"] for i in used),
            "pending_fields": missing, "repeat_row_counts": {key: max(1, count) for key, count in counts.items()},
            "real_value_fields": sorted({i["field_id"] for i in used if i["filled"] and i["status"] == "real"}),
            "layout_cleanup": layout_cleanup, "toc_cache_cleared": toc_cleanup["toc_cache_cleared"],
            "changed_package_parts": sorted(patches), "unchanged_package_parts": len(set(payload) - set(patches)),
            "formal_report_ready": False}
