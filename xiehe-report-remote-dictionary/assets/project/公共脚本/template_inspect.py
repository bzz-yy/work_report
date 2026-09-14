"""Read-only DOCX evidence and reviewed, position-exact template blanking.

This module never infers a field's business meaning or registers a generator.
A position is a paragraph's part + XPath and a half-open Unicode text interval.
Only approved intervals are replaced; untouched ZIP members retain their bytes.
"""
from __future__ import annotations

from collections import Counter
import csv
from difflib import SequenceMatcher
from hashlib import sha256
from io import BytesIO
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import tempfile
from zipfile import ZipFile
from urllib.parse import unquote, urlsplit

from lxml import etree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W, "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "v": "urn:schemas-microsoft-com:vml"}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PART_RE = re.compile(r"word/(?:document|header\d+|footer\d+)\.xml\Z")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]+\}\}|\$\{[^{}\n]+\}|(?<![A-Za-z0-9])[FQ]\d{3}(?!\d)|【待填[^】]*】|_{3,}")
NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)*(?:%|％)?")
LIMITATIONS = [
    "结构、占位和数字仅为字段线索，不推断公共字段ID、业务含义或来源。",
    "相似度仅用于候选排序；模板适配须核对口径、单位、循环、附件、签字和交付要求。",
    "XML结构无法证明分页与版式；保留原件，最终DOCX须渲染并逐页检查。",
    "图片只读取关系与摘要，不做OCR；嵌入对象、图表、脚注、批注等需另行审查。",
]
EXECUTOR_LIMITATION = (
    "现有月报生成器绑定F004/F005/F006等指标、F052月份台账、F059/F064记录序号、"
    "F090照片及records.*循环；同名或同号不证明含义可继承。新模板仅有位置映射仍不支持自动生成。"
)


def _tag(name):
    return f"{{{W}}}{name}"


def _digest(data):
    return sha256(data).hexdigest()


def _read_package(path):
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() != ".docx":
        raise ValueError("仅支持DOCX；旧DOC/PDF需单独保留原件并转换后再检查")
    raw = path.read_bytes()
    with ZipFile(BytesIO(raw)) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise ValueError("DOCX存在重复ZIP部件，无法确定唯一原件")
        payload = {i.filename: archive.read(i) for i in infos}
        comment = archive.comment
    if "word/document.xml" not in payload:
        raise ValueError("DOCX缺少word/document.xml")
    roots = {}
    for name, data in payload.items():
        if PART_RE.fullmatch(name):
            parser = ET.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
            root = ET.fromstring(data, parser=parser)
            if root.getroottree().docinfo.doctype:
                raise ValueError("DOCX含不支持的XML实体声明")
            roots[name] = root
    return path, raw, infos, payload, roots, comment


def _location(part, node):
    # Canonical w prefixes are independent of the source document's chosen prefixes.
    segments = []
    current = node
    while current is not None:
        q = ET.QName(current)
        if q.namespace != W:
            # Text boxes can include DrawingML ancestors. These are inspectable,
            # but are deliberately not accepted by the simple blanking locator.
            return {"part": part, "xpath": node.getroottree().getpath(node), "blankable": False}
        parent = current.getparent()
        index = ""
        if parent is not None:
            peers = [child for child in parent if child.tag == current.tag]
            index = f"[{peers.index(current) + 1}]"
        segments.append(f"w:{q.localname}{index}")
        current = parent
    return {"part": part, "xpath": "/" + "/".join(reversed(segments)), "blankable": True}


def _paragraph_segments(paragraph):
    result = []
    cursor = 0
    for node in paragraph.iter():
        if node.tag not in {_tag("t"), _tag("tab"), _tag("br"), _tag("cr")}:
            continue
        owner = next((a for a in node.iterancestors() if a.tag == _tag("p")), None)
        if owner is not paragraph:
            continue
        value = node.text or "" if node.tag == _tag("t") else ("\t" if node.tag == _tag("tab") else "\n")
        result.append((cursor, cursor + len(value), node, value))
        cursor += len(value)
    return result


def _paragraph_text(paragraph):
    return "".join(s[3] for s in _paragraph_segments(paragraph))


def _clues(text):
    clues = [{"kind": "placeholder", "start": m.start(), "end": m.end(), "text": m.group()}
             for m in PLACEHOLDER_RE.finditer(text)]
    for m in NUMBER_RE.finditer(text):
        if not any(c["start"] < m.end() and c["end"] > m.start() for c in clues):
            clues.append({"kind": "number_candidate", "start": m.start(), "end": m.end(), "text": m.group()})
    if ":" in text or "：" in text:
        clues.append({"kind": "label_value_candidate", "text": text})
    return sorted(clues, key=lambda c: c.get("start", -1))


def _rels_path(part):
    p = PurePosixPath(part)
    return str(p.parent / "_rels" / (p.name + ".rels"))


def _xml(data):
    parser = ET.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
    root = ET.fromstring(data, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError("DOCX含不支持的XML实体声明")
    return root


def _rels(part, payload):
    rel_path = _rels_path(part)
    if rel_path not in payload:
        return {}
    root = _xml(payload[rel_path])
    ids = [n.get("Id") for n in root]
    if len(ids) != len(set(ids)):
        raise ValueError("DOCX存在重复关系ID，无法唯一定位图片")
    return {n.get("Id"): {"target": n.get("Target"), "mode": n.get("TargetMode", "Internal"), "type": n.get("Type")}
            for n in root}


def _asset_target(part, target):
    if not isinstance(target, str):
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    target = unquote(parsed.path)
    asset = posixpath.normpath(posixpath.join(str(PurePosixPath(part).parent), target)) if not target.startswith("/") else target.lstrip("/")
    return None if asset.startswith("../") or asset == ".." else asset


def _has_image_resource_reference(node):
    return (any(node.get("{" + NS["r"] + "}" + key) for key in ("embed", "link", "id"))
            or any(value and ET.QName(key).localname in {"src", "href", "relid"} for key, value in node.attrib.items()))


def _image_records(roots, payload, *, internal=False):
    images = []
    occurrence_counts = Counter()
    for part, root in sorted(roots.items()):
        rels = _rels(part, payload)
        for node in root.xpath(".//a:blip | .//v:imagedata", namespaces=NS):
            refs = [node.get("{" + NS["r"] + "}" + k) for k in ("embed", "link", "id")]
            refs = [rid for rid in refs if rid]
            if not _has_image_resource_reference(node):
                continue
            rid = refs[0] if refs else None
            rel = rels.get(rid, {})
            asset = _asset_target(part, rel.get("target")) if rel.get("mode") == "Internal" else None
            paragraph = next((a for a in node.iterancestors() if a.tag == _tag("p")), None)
            container = next((a for a in node.iterancestors() if a.tag in {_tag("drawing"), _tag("pict")}), None)
            occurrence = occurrence_counts[(part, rid)]
            occurrence_counts[(part, rid)] += 1
            issue = None
            if len(refs) != 1 or not asset or asset not in payload or not str(rel.get("type", "")).endswith("/image"):
                issue = "外链、缺失或不明确的图片关系不支持修改"
            elif not asset.startswith("word/media/"):
                issue = "不在word/media下的图片部件需另行核验"
            elif container is None or container.getparent() is None or container.getparent().tag != _tag("r"):
                issue = "图片不在独立drawing/pict文字run内"
            elif len(container.xpath(".//a:blip | .//v:imagedata", namespaces=NS)) != 1 or any(
                    value != rid and str(rels.get(value, {}).get("type", "")).endswith("/image")
                    for child in container.iter() for key, value in child.attrib.items() if ET.QName(key).namespace == NS["r"]):
                issue = "同一drawing/pict含多个图片引用，需先拆分或核验"
            elif container.xpath(".//w:t | .//w:object | .//w:txbxContent", namespaces=NS):
                issue = "图片容器还包含文本或嵌入对象，不能整体删除"
            elif any(ET.QName(a).localname in {"AlternateContent", "del", "ins", "moveFrom", "moveTo"} for a in node.iterancestors()):
                issue = "图片位于兼容分支或修订中，需人工核验"
            locator = {"part": part, "relationship_id": rid, "occurrence_index": occurrence,
                       "paragraph": _location(part, paragraph) if paragraph is not None else None,
                       "asset_sha256": _digest(payload[asset]) if asset in payload else None}
            item = {"image_index": len(images), **locator, "locator": locator,
                    "relationship": rel, "asset": asset,
                    "asset_bytes": len(payload[asset]) if asset in payload else None,
                    "container": _location(part, container) if container is not None else None,
                    "relationship_supported": len(refs) == 1 and bool(asset in payload and str(rel.get("type", "")).endswith("/image")),
                    "edit_supported": issue is None, "unsupported_reason": issue, "static_role": None,
                    "review_status": "unreviewed"}
            if internal:
                item.update({"_node": node, "_container": container, "_paragraph": paragraph})
            images.append(item)
    return images


def _remove_unused_image_resources(payload, roots, actions):
    """Prune only removed images' now-unused relationships and media globally."""
    changed, removed = set(), set()
    candidate_assets = set()
    if not any(action["action"] != "retain_static" for action in actions):
        return changed, removed
    for action in actions:
        if action["action"] == "retain_static":
            continue
        part, rid = action["image"]["part"], action["image"]["relationship_id"]
        candidate_assets.add(action["image"]["asset"])
        if any(value == rid for node in roots[part].iter() for key, value in node.attrib.items()
               if ET.QName(key).namespace == NS["r"]):
            continue
        rel_path = _rels_path(part)
        if rel_path not in payload:
            continue
        rel_root = _xml(payload[rel_path])
        for rel in list(rel_root):
            if rel.get("Id") == rid:
                rel_root.remove(rel)
                changed.add(rel_path)
        if len(rel_root):
            payload[rel_path] = ET.tostring(rel_root, encoding="UTF-8", xml_declaration=True, standalone=True)
        else:
            del payload[rel_path]
            removed.add(rel_path)
    referenced = set()
    for name, data in payload.items():
        if not name.endswith(".rels"):
            continue
        p = PurePosixPath(name)
        if p.parent.name != "_rels":
            continue
        owner = str(p.parent.parent / p.name[:-5])
        for rel in _xml(data):
            if rel.get("TargetMode", "Internal") == "Internal":
                target = _asset_target(owner, rel.get("Target"))
                if target:
                    referenced.add(target)
    for asset in candidate_assets - referenced:
        if asset in payload:
            del payload[asset]
            removed.add(asset)
    if removed and "[Content_Types].xml" in payload:
        content_types = _xml(payload["[Content_Types].xml"])
        touched = False
        for child in list(content_types):
            if (child.get("PartName") or "").lstrip("/") in removed:
                content_types.remove(child)
                touched = True
        if touched:
            payload["[Content_Types].xml"] = ET.tostring(content_types, encoding="UTF-8", xml_declaration=True, standalone=True)
            changed.add("[Content_Types].xml")
    return changed, removed


def inspect_docx(path):
    """Return locatable text and structural evidence without modifying any file."""
    path, raw, _, payload, roots, _ = _read_package(path)
    paragraphs, tables, repeats, sections = [], [], [], []
    images = _image_records(roots, payload)
    decorative_drawings = []
    for part, root in sorted(roots.items()):
        for node in root.xpath(".//a:blip | .//v:imagedata", namespaces=NS):
            if _has_image_resource_reference(node):
                continue
            paragraph = next((a for a in node.iterancestors() if a.tag == _tag("p")), None)
            decorative_drawings.append({"part": part, "node_xpath": node.getroottree().getpath(node),
                    "paragraph": _location(part, paragraph) if paragraph is not None else None,
                    "kind": "unbound_" + ET.QName(node).localname, "reason": "无关系ID且未关联可取回图片资源；作为装饰或未完成图形结构待复核，不作为真实图片"})
    for part, root in sorted(roots.items()):
        for paragraph in root.iter(_tag("p")):
            text = _paragraph_text(paragraph)
            style = paragraph.find("w:pPr/w:pStyle", NS)
            clues = _clues(text)
            fields = paragraph.xpath(".//w:instrText/text() | .//w:fldSimple/@w:instr", namespaces=NS)
            if fields:
                clues.append({"kind": "word_field", "instructions": fields})
            paragraphs.append({"location": _location(part, paragraph), "text": text,
                               "text_sha256": _digest(text.encode()), "style": style.get(_tag("val")) if style is not None else None,
                               "in_table": any(a.tag == _tag("tc") for a in paragraph.iterancestors()),
                               "field_clues": clues})
        for table in root.iter(_tag("tbl")):
            rows = []
            for row in table.findall(_tag("tr")):
                cells = []
                for cell in row.findall(_tag("tc")):
                    span, merge = cell.find("w:tcPr/w:gridSpan", NS), cell.find("w:tcPr/w:vMerge", NS)
                    direct_paragraphs = [p for p in cell.iter(_tag("p"))
                                         if next(a for a in p.iterancestors() if a.tag == _tag("tc")) is cell]
                    cells.append({"location": _location(part, cell), "text": "\n".join(_paragraph_text(p) for p in direct_paragraphs),
                                  "paragraphs": [_location(part, p) for p in direct_paragraphs],
                                  "grid_span": int(span.get(_tag("val"))) if span is not None else 1,
                                  "v_merge": merge.get(_tag("val"), "continue") if merge is not None else None})
                rows.append({"location": _location(part, row), "xml_sha256": _digest(ET.tostring(row, with_tail=False)), "cells": cells})
            item = {"location": _location(part, table), "row_count": len(rows),
                    "physical_cell_counts": [len(row["cells"]) for row in rows], "rows": rows}
            tables.append(item)
            shapes = Counter(tuple((c["grid_span"], c["v_merge"]) for c in r["cells"]) for r in rows)
            repeated = [i for i, row in enumerate(rows) if shapes[tuple((c["grid_span"], c["v_merge"]) for c in row["cells"])] > 1]
            if repeated:
                repeats.append({"kind": "similar_row_shape", "table": item["location"], "row_indices": repeated,
                                "reason": "多行有相同物理单元格/合并结构；是否循环及原型行需人工判断", "confirmed": False})
        for section in root.iter(_tag("sectPr")):
            props = {}
            for key in ("pgSz", "pgMar", "cols", "type"):
                element = section.find(_tag(key))
                props[key] = {ET.QName(k).localname: v for k, v in element.attrib.items()} if element is not None else None
            sections.append({"location": _location(part, section), "properties": props})
    unsupported = [p for p in payload if re.match(r"word/(?:embeddings/|charts/|footnotes\.xml|endnotes\.xml|comments\.xml)", p)]
    return {"schema_version": 1, "source": {"path": str(path), "sha256": _digest(raw)},
            "summary": {"paragraph_count": len(paragraphs), "table_count": len(tables), "image_occurrences": len(images),
                        "section_count": len(sections), "header_footer_parts": [p for p in roots if p != "word/document.xml"],
                        "placeholder_occurrences": sum(c["kind"] == "placeholder" for p in paragraphs for c in p["field_clues"]),
                        "unsupported_parts": unsupported,
                        "uninspected_media_parts": [name for name in payload if name.startswith("word/media/") and not name.endswith("/") and name not in {i["asset"] for i in images}]},
            "paragraphs": paragraphs, "tables": tables, "images": images, "decorative_drawings": decorative_drawings, "sections": sections,
            "repeat_candidates": repeats, "limitations": list(LIMITATIONS), "business_fit": "unreviewed"}


def _normal(text):
    text = PLACEHOLDER_RE.sub("#", text)
    return re.sub(r"\s+", "", NUMBER_RE.sub("#", text))


def _ratio(a, b):
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _within(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("报告索引或模板路径越出运行项目")
    return path


def _mapping_fields(mapping):
    fields = mapping.get("fields", [])
    if isinstance(fields, list):
        return [{k: f[k] for k in ("id", "label", "kind", "unit", "placeholder") if k in f} for f in fields]
    if isinstance(fields, dict):
        return [{"id": key, "location_count": len(value) if isinstance(value, list) else 1} for key, value in fields.items()]
    return []


def compare_templates(project_root, sample_path, report_type):
    """Rank only the requested report's templates; never make a business decision."""
    project_root = Path(project_root).resolve(strict=True)
    with (project_root / "报告索引.csv").open(encoding="utf-8-sig", newline="") as handle:
        matches = [r for r in csv.DictReader(handle) if r["报告类型"] == report_type]
    if len(matches) != 1:
        raise ValueError("报告类型未知或报告索引不唯一；请先明确服务分类及报告类型")
    report_dir = _within(project_root, matches[0]["报告目录"])
    sample = inspect_docx(sample_path)
    sample_text = [_normal(p["text"]) for p in sample["paragraphs"] if p["text"].strip()]
    sample_shapes = [tuple(tuple((c["grid_span"], c["v_merge"]) for c in r["cells"]) for r in t["rows"]) for t in sample["tables"]]
    candidates = []
    for template_file in sorted((report_dir / "模板").glob("*/*.docx")):
        template_file = _within(project_root, str(template_file))
        evidence = inspect_docx(template_file)
        mapping_file = template_file.parent / "模板字段映射.json"
        mapping = json.loads(mapping_file.read_text(encoding="utf-8")) if mapping_file.exists() else {}
        texts = [_normal(p["text"]) for p in evidence["paragraphs"] if p["text"].strip()]
        shapes = [tuple(tuple((c["grid_span"], c["v_merge"]) for c in r["cells"]) for r in t["rows"]) for t in evidence["tables"]]
        components = {"normalized_text": _ratio("\n".join(sample_text), "\n".join(texts)),
                      "table_structure": _ratio(sample_shapes, shapes),
                      "sections": _ratio([json.dumps(s["properties"], sort_keys=True) for s in sample["sections"]],
                                         [json.dumps(s["properties"], sort_keys=True) for s in evidence["sections"]])}
        differences = []
        raw_a = [p for p in sample["paragraphs"] if p["text"].strip()]
        raw_b = [p for p in evidence["paragraphs"] if p["text"].strip()]
        for operation, a, b, c, d in SequenceMatcher(None, sample_text, texts, autojunk=False).get_opcodes():
            if operation != "equal":
                differences.append({"kind": "text", "operation": operation,
                                    "sample": [{"location": p["location"], "text": p["text"]} for p in raw_a[a:b]],
                                    "existing": [{"location": p["location"], "text": p["text"]} for p in raw_b[c:d]]})
        if sample_shapes != shapes:
            differences.append({"kind": "table_structure", "sample": [t["physical_cell_counts"] for t in sample["tables"]],
                                "existing": [t["physical_cell_counts"] for t in evidence["tables"]]})
        if components["sections"] < 1:
            differences.append({"kind": "sections", "sample": sample["sections"], "existing": evidence["sections"]})
        if sample["summary"]["image_occurrences"] != evidence["summary"]["image_occurrences"]:
            differences.append({"kind": "image_count", "sample": sample["summary"]["image_occurrences"], "existing": evidence["summary"]["image_occurrences"]})
        template_id = mapping.get("template_id", template_file.parent.name)
        candidates.append({"template_id": template_id, "template_file": str(template_file.relative_to(project_root)),
                           "template_sha256": evidence["source"]["sha256"],
                           "mapping_sha256_matches": evidence["source"]["sha256"] == mapping.get("template_sha256") if mapping else None,
                           "score": round(100 * (components["normalized_text"] * .6 + components["table_structure"] * .3 + components["sections"] * .1), 2),
                           "score_components": {k: round(v, 4) for k, v in components.items()}, "differences": differences,
                           "fields": _mapping_fields(mapping), "review_required": True, "business_fit": "unreviewed",
                           "executor": {"existing_monthly_contract": report_type == "运维月报" and template_id == "NW-MONTHLY-STD-01",
                                        "new_sample_supported": False, "limitation": EXECUTOR_LIMITATION}})
    return {"schema_version": 1, "sample": sample["source"], "sample_summary": sample["summary"], "report_type": report_type,
            "candidates": sorted(candidates, key=lambda c: (-c["score"], c["template_id"])),
            "business_fit": "unreviewed", "limitations": list(LIMITATIONS),
            "score_policy": "60%文本线索+30%表格结构+10%分节参数；数字与占位归一仅用于排序，无通过阈值"}


def _clean_metadata(payload, roots):
    """Clear metadata values without changing visible Word parts or their caches."""
    field_pattern = re.compile(r"(?<![A-Za-z])(?:DOCPROPERTY|AUTHOR|TITLE|SUBJECT|KEYWORDS|COMMENTS|CREATEDATE|SAVEDATE|LASTSAVEDBY|REVNUM)(?![A-Za-z])", re.I)
    for part, root in roots.items():
        for paragraph in root.iter(_tag("p")):
            instructions = ["".join(paragraph.xpath(".//w:instrText/text()", namespaces=NS))]
            instructions.extend(paragraph.xpath(".//w:fldSimple/@w:instr", namespaces=NS))
            if any(field_pattern.search(instruction) for instruction in instructions):
                raise ValueError("正文或页眉页脚存在元数据字段引用；须先逐位置核验其字段及缓存值，不能随模板保留")
    roles = {"docProps/core.xml": "core", "docProps/app.xml": "app", "docProps/custom.xml": "custom"}
    root_rels = payload.get("_rels/.rels")
    if root_rels:
        suffixes = {"/metadata/core-properties": "core", "/extended-properties": "app", "/custom-properties": "custom"}
        for rel in _xml(root_rels):
            role = next((value for suffix, value in suffixes.items() if str(rel.get("Type", "")).endswith(suffix)), None)
            if role:
                target = _asset_target(".", rel.get("Target"))
                if rel.get("TargetMode", "Internal") != "Internal" or not target or target not in payload:
                    raise ValueError("元数据部件引用缺失或为外链，须先单独核验")
                roles[target] = role
    cleanup, changed = [], set()
    for part, role in roles.items():
        if part not in payload:
            continue
        root = _xml(payload[part])
        touched = False
        if role in {"core", "app"}:
            for child in list(root):
                key = ET.QName(child).localname if isinstance(child.tag, str) else "xml_comment"
                cleanup.append({"part": part, "key": key, "action": "removed"})
                root.remove(child)
                touched = True
        else:
            # Keeping property names/pids preserves the legal part and indirect
            # references; replacing all values avoids retaining unknown VT data.
            for number, prop in enumerate(root, 1):
                if not isinstance(prop.tag, str):
                    root.remove(prop)
                    touched = True
                    cleanup.append({"part": part, "key": "xml_comment", "action": "removed"})
                    continue
                key = prop.get("name") or "property_" + str(number)
                for child in list(prop):
                    prop.remove(child)
                for attribute in list(prop.attrib):
                    if attribute not in {"fmtid", "pid", "name"}:
                        del prop.attrib[attribute]
                prop.text = None
                prop.tail = None
                ET.SubElement(prop, "{http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes}lpwstr")
                cleanup.append({"part": part, "key": key, "action": "value_cleared"})
                touched = True
        if root.text and root.text.strip():
            root.text = None
            cleanup.append({"part": part, "key": "root_text", "action": "removed"})
            touched = True
        if touched:
            payload[part] = ET.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
            changed.add(part)
    return cleanup, changed


def _validate_removed_merge_groups(table, selected_rows):
    rows = table.findall(_tag("tr"))
    selected_here = set(rows) & selected_rows
    if not any(row.xpath("./w:tc/w:tcPr/w:vMerge", namespaces=NS) for row in selected_here):
        return
    # Every remaining row is free of vertical-merge declarations. Therefore
    # no restart/continue member can survive this removal, regardless of
    # unrelated horizontal spans in the retained header or ordinary rows.
    # Nested/unsafe content in selected rows was already rejected by the caller.
    remaining_rows = [row for row in rows if row not in selected_here]
    if not any(row.xpath(".//w:vMerge", namespaces=NS) for row in remaining_rows):
        return
    if table.xpath("./w:tr/w:tc/w:tcPr/w:gridSpan | ./w:tr/w:tc/w:tcPr/w:hMerge | ./w:tr/w:trPr/w:gridBefore | ./w:tr/w:trPr/w:gridAfter", namespaces=NS):
        raise ValueError("gridSpan/hMerge或列偏移与vMerge组合暂不支持删除")
    cell_counts = {len(row.findall(_tag("tc"))) for row in rows}
    if len(cell_counts) != 1:
        raise ValueError("vMerge表格各行列数不一致，无法安全核验合并组")
    groups, active = [], {}
    for row in rows:
        for column, cell in enumerate(row.findall(_tag("tc"))):
            merge = cell.find("w:tcPr/w:vMerge", NS)
            value = merge.get(_tag("val"), "continue") if merge is not None else None
            if value in {None, "restart"} and column in active:
                groups.append(active.pop(column))
            if value == "restart":
                active[column] = [row]
            elif value == "continue":
                if column not in active:
                    raise ValueError("vMerge存在无restart的continue，须先修复原件")
                active[column].append(row)
            elif value is not None:
                raise ValueError("未知vMerge合并状态")
    groups.extend(active.values())
    for group in groups:
        group_rows = set(group)
        if selected_here & group_rows and not group_rows <= selected_here:
            raise ValueError("vMerge合并组只能完整删除，不能留下restart或continue的部分组")


def _insert_text(paragraph, offset, token, *, break_before=False):
    segments = _paragraph_segments(paragraph)
    for start, end, node, value in segments:
        if node.tag == _tag("t") and start <= offset <= end:
            if break_before:
                run = node.getparent()
                if run.tag != _tag("r"):
                    raise ValueError("换行插入须位于独立文字run内")
                node.text = value[:offset - start]
                line_break = ET.Element(_tag("br"))
                text_node = ET.Element(_tag("t")); text_node.set(XML_SPACE, "preserve")
                text_node.text = token + value[offset - start:]
                at = run.index(node) + 1
                run.insert(at, line_break); run.insert(at + 1, text_node)
            else:
                node.text = value[:offset - start] + token + value[offset - start:]
            node.set(XML_SPACE, "preserve")
            return
    if segments:
        for start, end, node, _ in segments:
            if offset in {start, end} and node.getparent().tag == _tag("r"):
                text_node = ET.Element(_tag("t")); text_node.set(XML_SPACE, "preserve"); text_node.text = token
                run = node.getparent()
                at = run.index(node) + (offset == end)
                if break_before:
                    run.insert(at, ET.Element(_tag("br"))); at += 1
                run.insert(at, text_node)
                return
        raise ValueError("插入位置无法定位到独立文本槽")
    if paragraph.xpath(".//w:drawing | .//w:pict | .//w:object | .//w:sdt", namespaces=NS):
        raise ValueError("空段落含图片或控件，文字下标不能唯一定位插入位置")
    run = ET.SubElement(paragraph, _tag("r"))
    if break_before:
        ET.SubElement(run, _tag("br"))
    text_node = ET.SubElement(run, _tag("t")); text_node.set(XML_SPACE, "preserve"); text_node.text = token


def blank_from_plan(original, out, positions):
    """Create a new DOCX from an explicitly reviewed source-digest/position plan.

positions = {source_sha256: str, positions: [{location: {part, xpath},
 start: int, end: int, expected_text: str, placeholder: '{{N001}}'}]}.
image_actions optionally reviews exact image occurrences (retain_static/remove/placeholder).
row_actions optionally removes reviewed exact rows after source and row digest checks.
No inferred field IDs, dictionary changes or unapproved row/photo deletion.
"""
    source, raw, infos, payload, roots, comment = _read_package(original)
    output = Path(out).resolve()
    if output == source or output.exists():
        raise ValueError("输出须为新的DOCX路径，不能覆盖原件或已有文件")
    if output.suffix.lower() != ".docx":
        raise ValueError("输出必须为DOCX")
    if not isinstance(positions, dict) or positions.get("source_sha256") != _digest(raw):
        raise ValueError("原件摘要不匹配；须基于当前原件重新复核位置计划")
    entries = positions.get("positions", [])
    image_actions = positions.get("image_actions", [])
    row_actions = positions.get("row_actions", [])
    if not all(isinstance(x, list) for x in (entries, image_actions, row_actions)) or not (entries or image_actions or row_actions):
        raise ValueError("positions、image_actions和row_actions必须为列表，至少有一个批准操作")
    resolved, by_paragraph = [], {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("每个批准位置必须为对象")
        loc = entry.get("location", {})
        if not isinstance(loc, dict):
            raise ValueError("location必须包含part和xpath")
        part, xpath = loc.get("part"), loc.get("xpath")
        if part not in roots or not isinstance(xpath, str) or not re.fullmatch(r"/w:[A-Za-z]+(?:/w:[A-Za-z]+\[\d+\])*", xpath):
            raise ValueError("不支持的部件或XPath位置；须使用inspect返回的可留白段落位置")
        nodes = roots[part].xpath(xpath, namespaces=NS)
        if len(nodes) != 1 or nodes[0].tag != _tag("p"):
            raise ValueError("批准位置不存在或不是唯一段落")
        paragraph = nodes[0]
        if any(a.tag == _tag("sdt") and a.find("w:sdtPr/w:dataBinding", NS) is not None for a in paragraph.iterancestors()):
            raise ValueError("批准位置位于数据绑定内容控件内；须先核验其独立数据来源")
        if paragraph.xpath(".//w:fldChar | .//w:instrText | .//w:fldSimple | .//w:del | .//w:ins | .//w:moveFrom | .//w:moveTo", namespaces=NS):
            raise ValueError("批准位置含Word域或修订；先人工处理并重新检查原件")
        start, end = entry.get("start"), entry.get("end")
        text_action = entry.get("action", "placeholder")
        if text_action not in {"placeholder", "clear", "insert_placeholder"}:
            raise ValueError("文本action仅支持placeholder、clear或insert_placeholder")
        if "break_before" in entry and (text_action != "insert_placeholder" or type(entry["break_before"]) is not bool):
            raise ValueError("break_before只允许用于insert_placeholder且必须为布尔值")
        text = _paragraph_text(paragraph)
        if type(start) is not int or type(end) is not int:
            raise ValueError("批准位置字符区间无效")
        if text_action == "insert_placeholder":
            if not (0 <= start == end <= len(text)):
                raise ValueError("insert_placeholder必须使用start=end的插入点")
        elif not (0 <= start < end <= len(text)):
            raise ValueError("批准位置字符区间无效")
        if not isinstance(entry.get("expected_text"), str) or text[start:end] != entry["expected_text"]:
            raise ValueError("批准位置原文不匹配")
        token = entry.get("placeholder")
        if text_action == "clear":
            if token is not None:
                raise ValueError("clear动作不能同时声明placeholder")
            token = ""
        elif not isinstance(token, str) or not re.fullmatch(r"\{\{[A-Za-z][A-Za-z0-9_.-]{0,63}\}\}", token):
            raise ValueError("局部placeholder须为{{自定义ID}}，不能自动沿用既有F号语义")
        key = (part, xpath)
        for old in by_paragraph.setdefault(key, []):
            overlaps = start < old["end"] and end > old["start"]
            if start == end and old["start"] == old["end"]:
                overlaps = start == old["start"]
            elif start == end:
                overlaps = old["start"] < start < old["end"]
            elif old["start"] == old["end"]:
                overlaps = start < old["start"] < end
            if overlaps:
                raise ValueError("批准位置重复或重叠")
        segments = [s for s in _paragraph_segments(paragraph) if s[0] < end and s[1] > start]
        if text_action != "insert_placeholder" and (not segments or any(s[2].tag != _tag("t") for s in segments)):
            raise ValueError("批准范围跨越制表符或换行，须拆为独立文本位置")
        if sum(min(end, b) - max(start, a) for a, b, _, _ in segments) != end - start:
            raise ValueError("批准位置无法完整定位到文本")
        item = {**entry, "action": text_action, "placeholder": token, "paragraph": paragraph}
        by_paragraph[key].append(item)
        resolved.append(item)
    image_records = _image_records(roots, payload, internal=True)
    resolved_images, seen_images, seen_containers = [], set(), set()
    roles = {"logo": "static_brand", "static_illustration": "static_diagram",
             "static_brand": "static_brand", "static_diagram": "static_diagram"}
    for action in image_actions:
        if not isinstance(action, dict) or type(action.get("image_index")) is not int:
            raise ValueError("图片动作必须给出唯一image_index和完整图片位置")
        if "break_before" in action:
            raise ValueError("break_before只允许用于insert_placeholder")
        index = action["image_index"]
        if index < 0 or index >= len(image_records):
            raise ValueError("图片image_index不存在")
        image = image_records[index]
        if index in seen_images:
            raise ValueError("图片动作重复或歧义")
        seen_images.add(index)
        for key in ("part", "relationship_id", "occurrence_index", "asset_sha256"):
            if action.get(key) != image.get(key) or (key == "occurrence_index" and type(action.get(key)) is not int):
                raise ValueError("图片位置或摘要不匹配；需重新核验原件")
        if "paragraph" in action and action["paragraph"] != image["paragraph"]:
            raise ValueError("图片段落位置不匹配")
        if not image["asset_sha256"] or not image["relationship_supported"] or image["relationship"].get("mode") != "Internal":
            raise ValueError("未知或外链图片不支持修改或认定静态")
        kind = action.get("action")
        if kind not in {"retain_static", "remove", "placeholder"}:
            raise ValueError("未知图片动作")
        if not isinstance(action.get("reason"), str) or not action["reason"].strip():
            raise ValueError("图片动作需要明确复核依据reason")
        if kind == "retain_static":
            if action.get("static_role") not in roles:
                raise ValueError("保留图片须明确static_brand或static_diagram，不能默认将历史照片或签名当静态")
        else:
            if not image["edit_supported"]:
                raise ValueError("不支持图片删除或占位：" + str(image["unsupported_reason"]))
            container = image["_container"]
            if container in seen_containers:
                raise ValueError("多个动作指向同一图片容器，存在歧义")
            seen_containers.add(container)
            if any(item["paragraph"] is container or container in item["paragraph"].iterancestors() for item in resolved):
                raise ValueError("图片动作与文字位置相互覆盖")
        if kind == "placeholder":
            token = action.get("placeholder")
            if not isinstance(token, str) or not re.fullmatch(r"\{\{[A-Za-z][A-Za-z0-9_.-]{0,63}\}\}", token):
                raise ValueError("图片placeholder须为{{自定义ID}}")
            if token in {item["placeholder"] for item in resolved}:
                raise ValueError("图片占位ID不能与文本位置ID混用")
        resolved_images.append({**action, "image": image, "static_role": roles.get(action.get("static_role"))})
    resolved_rows, selected_rows = [], set()
    image_actions_by_index = {action["image_index"]: action for action in resolved_images}
    for action in row_actions:
        if not isinstance(action, dict) or action.get("action") != "remove":
            raise ValueError("row_actions仅支持明确的remove动作")
        if "break_before" in action:
            raise ValueError("break_before只允许用于insert_placeholder")
        loc = action.get("location", {})
        if not isinstance(loc, dict):
            raise ValueError("行location必须包含part和xpath")
        part, xpath = loc.get("part"), loc.get("xpath")
        if part not in roots or not isinstance(xpath, str) or not re.fullmatch(r"/w:[A-Za-z]+(?:/w:[A-Za-z]+\[\d+\])*", xpath):
            raise ValueError("不支持的行部件或XPath位置")
        nodes = roots[part].xpath(xpath, namespaces=NS)
        if len(nodes) != 1 or nodes[0].tag != _tag("tr"):
            raise ValueError("批准行不存在或不是唯一表格行")
        row = nodes[0]
        if action.get("row_sha256") != _digest(ET.tostring(row, with_tail=False)):
            raise ValueError("原始行XML摘要不匹配")
        if not isinstance(action.get("reason"), str) or not action["reason"].strip():
            raise ValueError("行删除需要明确复核依据reason")
        if row.getparent().tag != _tag("tbl"):
            raise ValueError("不支持非直接表格子行的删除")
        if row in selected_rows or any(row in old.iterancestors() or old in row.iterancestors() for old in selected_rows):
            raise ValueError("行动作重复或存在父子重叠")
        if row.xpath(".//w:tbl//w:vMerge", namespaces=NS):
            raise ValueError("被删行包含嵌套表格vMerge，需单独核验")
        if row.xpath(".//w:object | .//w:del | .//w:ins | .//w:moveFrom | .//w:moveTo", namespaces=NS):
            raise ValueError("被删行含嵌入对象或修订，需另行核验")
        if any(row in item["paragraph"].iterancestors() for item in resolved):
            raise ValueError("不能删除仍承载声明文本placeholder的行")
        if any(PLACEHOLDER_RE.search(_paragraph_text(paragraph)) for paragraph in row.iter(_tag("p"))):
            raise ValueError("被删行已有placeholder，须先明确保留原型位置")
        row_images = [image for image in image_records if row in image["_node"].iterancestors()]
        for image in row_images:
            image_action = image_actions_by_index.get(image["image_index"])
            if image_action is None or image_action["action"] != "remove":
                raise ValueError("被删行的图片须明确remove，不能删除未审查、静态保留或照片placeholder")
        known_containers = {image["_container"] for image in row_images}
        if any(container not in known_containers for container in row.xpath(".//w:drawing | .//w:pict", namespaces=NS)):
            raise ValueError("被删行含未识别图形，需另行核验")
        selected_rows.add(row)
        resolved_rows.append({**action, "_row": row})
    per_table = Counter(row.getparent() for row in selected_rows)
    for table, count in per_table.items():
        _validate_removed_merge_groups(table, selected_rows)
        if count >= len(table.findall(_tag("tr"))):
            raise ValueError("不能删除表格全部行，须保留有效结构")
    changed = set()
    for (part, _), entries_for_p in by_paragraph.items():
        for entry in sorted(entries_for_p, key=lambda e: (e["start"], e["end"]), reverse=True):
            start, end, token = entry["start"], entry["end"], entry["placeholder"]
            if entry["action"] == "insert_placeholder":
                _insert_text(entry["paragraph"], start, token, break_before=entry.get("break_before", False))
                changed.add(part)
                continue
            segments = [s for s in _paragraph_segments(entry["paragraph"]) if s[0] < end and s[1] > start]
            for index, (a, b, node, value) in enumerate(segments):
                prefix, suffix = value[:max(0, start - a)], value[min(len(value), end - a):]
                node.text = prefix + (token if index == 0 else "") + suffix
                node.set(XML_SPACE, "preserve")
            changed.add(part)
    for action in resolved_images:
        if action["action"] == "retain_static":
            continue
        image = action["image"]
        container = image["_container"]
        run = container.getparent()
        if action["action"] == "placeholder":
            text_node = ET.Element(_tag("t"))
            text_node.set(XML_SPACE, "preserve")
            text_node.text = action["placeholder"]
            run.insert(run.index(container), text_node)
        run.remove(container)
        changed.add(image["part"])
    for action in resolved_rows:
        row = action["_row"]
        row.getparent().remove(row)
        changed.add(action["location"]["part"])
    for part in changed:
        payload[part] = ET.tostring(roots[part], encoding="UTF-8", xml_declaration=True, standalone=True)
    resource_changes, removed = _remove_unused_image_resources(payload, roots, resolved_images)
    changed.update(resource_changes)
    metadata_cleanup, metadata_changes = _clean_metadata(payload, roots)
    changed.update(metadata_changes)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".blank-", suffix=".docx", dir=output.parent)
    os.close(fd)
    try:
        with ZipFile(tmp, "w") as archive:
            archive.comment = comment
            for info in infos:
                if info.filename in payload:
                    archive.writestr(info, payload[info.filename])
        # Exclusive publication prevents a concurrently-created output being overwritten.
        os.link(tmp, output)
    finally:
        Path(tmp).unlink(missing_ok=True)
    fields = []
    for item in resolved:
        if item["action"] == "clear":
            continue
        fields.append({"local_id": item["placeholder"][2:-2], "placeholder": item["placeholder"],
                       "location": item["location"], "source_range": {"start": item["start"], "end": item["end"]},
                       "source_text": item["expected_text"], "data_definition": None, "semantic_review": "pending"})
    image_review = []
    for action in resolved_images:
        image = action["image"]
        image_review.append({k: action[k] for k in ("image_index", "part", "relationship_id", "occurrence_index", "asset_sha256", "action", "reason", "static_role")})
        if action["action"] == "placeholder":
            fields.append({"local_id": action["placeholder"][2:-2], "placeholder": action["placeholder"],
                           "kind": "image", "location": image["paragraph"], "image_locator": image["locator"],
                           "data_definition": None, "semantic_review": "pending"})
    return {"schema_version": 1, "source": {"path": str(source), "sha256": _digest(raw)},
            "template_file": str(output), "template_sha256": _digest(output.read_bytes()), "changed_parts": sorted(changed - removed),
            "removed_parts": sorted(removed), "image_review": image_review, "metadata_cleanup": metadata_cleanup,
            "row_review": [{k: action[k] for k in ("location", "row_sha256", "action", "reason")} for action in resolved_rows],
            "cleared_positions": [{"location": item["location"], "start": item["start"], "end": item["end"]} for item in resolved if item["action"] == "clear"],
            "unreviewed_image_indices": [i["image_index"] for i in image_records if i["image_index"] not in seen_images],
            "status": "blank_template_review_required", "fields": fields, "executor": "unsupported",
            "executor_limitation": EXECUTOR_LIMITATION, "render_review": "pending", "formal_report_ready": False,
            "limitations": list(LIMITATIONS) + ["仅批准的文本、图片和表格行被处理；未批准的旧值、图片、签字和附件仍保留，发布前须逐项复核。"]}
