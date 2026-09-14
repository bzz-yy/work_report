"""Fill the retained monthly DOCX; unresolved values remain editable placeholders.

Only document.xml and settings.xml are rewritten.  Collection is deliberately
separate: this module never calls a platform or reuses historical report values.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from zipfile import ZipFile
import os
import uuid

from lxml import etree as ET
from photo_fill import embed_photos

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
# Units may be adjacent in the retained template (notably F015MW).
TOKEN = re.compile(r"(?<![A-Za-z0-9])F\d{3}(?![0-9])")
FILLABLE = {"real", "request", "config", "derived", "fixed"}


def tag(name):
    return f"{{{W}}}{name}"


def text_of(node):
    return "".join(node.xpath(".//w:t/text()", namespaces=NS))


def field_item(value, default_status="missing"):
    if isinstance(value, dict):
        return value
    return {"value": value, "status": default_status}


def is_available(item):
    return (item.get("status") in FILLABLE and item.get("value") is not None
            and (item.get("value") != "" or item.get("status") == "fixed"))


def display_value(field_id, item, label, in_table):
    if field_id == "F090":
        return "【待填：当期现场照片】", False
    if not is_available(item):
        return "待填" if in_table else f"【待填：{label}】", False
    value = item["value"]
    if isinstance(value, (dict, list, tuple, bool)):
        raise ValueError(f"{field_id}需要单个可显示的值")
    rendered = format(value, ".12g") if isinstance(value, float) else str(value)
    if field_id == "F013":
        rendered = rendered.strip().rstrip("%％").rstrip() + "%"
    return rendered, True


def set_property(parent, name, value):
    element = parent.find(tag(name))
    if element is None:
        element = ET.SubElement(parent, tag(name))
    element.set(tag("val"), value)
    return element


def replacement_run(source_run, value, available, original_rpr=None):
    run = ET.Element(tag("r"))
    source_rpr = source_run.find(tag("rPr"))
    if original_rpr:
        rpr = ET.fromstring(original_rpr.encode("utf-8"))
        run.append(rpr)
    elif source_rpr is not None:
        rpr = deepcopy(source_rpr)
        run.append(rpr)
    else:
        rpr = ET.SubElement(run, tag("rPr"))
    if not available:
        set_property(rpr, "color", "7F6000")
        set_property(rpr, "highlight", "yellow")
    for n, line in enumerate(value.split("\n")):
        if n:
            ET.SubElement(run, tag("br"))
        t = ET.SubElement(run, tag("t"))
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = line
    return run


def replace_paragraph(paragraph, values, labels, original_rprs, used):
    """Replace whole field tokens even when Word split one across text runs."""
    nodes = paragraph.xpath(".//w:t", namespaces=NS)
    joined = "".join(n.text or "" for n in nodes)
    offsets = []
    pos = 0
    for node in nodes:
        offsets.append((pos, pos + len(node.text or ""), node))
        pos += len(node.text or "")
    in_table = any(p.tag == tag("tc") for p in paragraph.iterancestors())
    for match in reversed(list(TOKEN.finditer(joined))):
        fid = match.group()
        if fid not in labels:
            raise ValueError(f"模板存在未定义字段：{fid}")
        item = field_item(values.get(fid))
        rendered, available = display_value(fid, item, labels[fid], in_table)
        touched = [(a, b, n) for a, b, n in offsets if a < match.end() and b > match.start()]
        a, b, first = touched[0]
        first_run = first.getparent()
        if first_run.tag != tag("r") or len(first_run.findall(tag("t"))) != 1:
            raise ValueError(f"{fid}不支持的文本槽结构，需核验模板")
        original_text = first.text or ""
        prefix = original_text[:match.start() - a]
        suffix = original_text[match.end() - a:] if len(touched) == 1 else ""
        first.text = prefix
        for begin, end, node in touched[1:]:
            node.text = (node.text or "")[max(0, match.end() - begin):]
        new_run = replacement_run(first_run, rendered, available, original_rprs.get(fid))
        first_run.addnext(new_run)
        if suffix:
            new_run.addnext(replacement_run(first_run, suffix, True))
        used.append({"field_id": fid, "status": item.get("status", "missing"), "filled": available})


def replace_region(node, values, labels, original_rprs, used):
    for paragraph in node.xpath(".//w:p", namespaces=NS):
        replace_paragraph(paragraph, values, labels, original_rprs, used)


def month_number(record):
    value = field_item(record.get("F052"), "request").get("value")
    if value is None:
        return None
    match = re.fullmatch(r"(?:20\d{2}[-年])?(0?[1-9]|1[0-2])月?", str(value).strip())
    return int(match.group(1)) if match else None


def group_records(group, payload, period):
    """Build a complete year-to-date skeleton without copying monthly values."""
    status = payload.get("status", "missing")
    records = payload.get("records") or []
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError(f"{group['key']}的records必须为对象列表")
    prepared = [{fid: field_item(value, status) for fid, value in row.items()} for row in records]
    if group["key"] != "records.1":
        return prepared or [{}]
    final_month = int(period.split("-")[1])
    by_month = {}
    total = None
    for record in prepared:
        m = month_number(record)
        label = str(field_item(record.get("F052")).get("value", "")).strip()
        explicit_year = re.match(r"(20\d{2})[-年]", label)
        if explicit_year and explicit_year.group(1) != period[:4]:
            raise ValueError("发电量台账年份与报告年份不一致")
        if label in {"累计", "合计"}:
            if total is not None:
                raise ValueError("发电量台账有重复累计行")
            total = record
        elif m is None or m > final_month or m in by_month:
            raise ValueError("发电量台账月份重复、无法识别或超出报告月")
        else:
            by_month[m] = record
    result = []
    for month in range(1, final_month + 1):
        row = by_month.get(month, {})
        row["F052"] = {"value": str(month), "status": "request"}
        result.append(row)
    total = total or {}
    total["F052"] = {"value": "累计", "status": "request"}
    result.append(total)
    return result


def expand_groups(root, mapping, collected, period, labels, original_rprs, used):
    tables = root.xpath("/w:document/w:body/w:tbl", namespaces=NS)
    # Resolve row elements before any expansion: two groups share the same table.
    slots = [(g, tables[g["table_index"]].findall(tag("tr"))[g["prototype_row_index"]])
             for g in mapping["repeat_groups"]]
    counts = {}
    for group, prototype in slots:
        payload = collected.get("repeat_groups", {}).get(group["key"], {})
        records = group_records(group, payload, period)
        table = prototype.getparent()
        at = table.index(prototype)
        for n, row_values in enumerate(records):
            row = deepcopy(prototype)
            row_properties=row.find(tag('trPr'))
            if row_properties is None:
                row_properties=ET.Element(tag('trPr'));row.insert(0,row_properties)
            set_property(row_properties,'cantSplit','1')
            # Cloned paragraphs may not share identifiers/bookmarks with the source.
            for node in row.iter():
                for attr in list(node.attrib):
                    if attr.endswith("}paraId") or attr.endswith("}textId"):
                        del node.attrib[attr]
            for bookmark in row.xpath(".//w:bookmarkStart | .//w:bookmarkEnd", namespaces=NS):
                bookmark.getparent().remove(bookmark)
            for height in row.xpath("w:trPr/w:trHeight[@w:hRule='exact']", namespaces=NS):
                height.set(tag("hRule"), "atLeast")
            # No cross-record vertical merge is inferred from an unverified template.
            for merge in row.xpath("w:tc/w:tcPr/w:vMerge", namespaces=NS):
                merge.getparent().remove(merge)
            replace_region(row, row_values, labels, original_rprs, used)
            table.insert(at + n, row)
        table.remove(prototype)
        counts[group["key"]] = len(records)
    return counts


def combined_table_sections(root, mapping):
    """Capture source row identities before expansion changes row offsets.

    Only the mapped header/prototype pairs are eligible. Unknown intervening
    rows or vertical merges need a reviewed layout instead of inferred splits.
    """
    tables = root.xpath("/w:document/w:body/w:tbl", namespaces=NS)
    grouped = {}
    for group in mapping["repeat_groups"]:
        grouped.setdefault(group["table_index"], []).append(group)
    sections = []
    for index, groups in grouped.items():
        if len(groups) < 2:
            continue
        table = tables[index]
        rows = table.findall(tag("tr"))
        groups = sorted(groups, key=lambda group: group["prototype_row_index"])
        if (len(rows) != 2 * len(groups)
                or [group["prototype_row_index"] for group in groups] != list(range(1, len(rows), 2))
                or any(node.tag not in {tag("tblPr"), tag("tblGrid"), tag("tr")} for node in table)
                or table.xpath(".//w:vMerge", namespaces=NS)):
            raise ValueError("共享循环表不是独立表头及原型行结构，需核验分段布局")
        headers = []
        for group in groups:
            prototype_index = group["prototype_row_index"]
            header = rows[prototype_index - 1]
            prototype_fields = {fid for paragraph in rows[prototype_index].xpath(".//w:p", namespaces=NS)
                                for fid in TOKEN.findall(text_of(paragraph))}
            if (not text_of(header).strip() or TOKEN.search(text_of(header))
                    or prototype_fields != set(group["field_ids"])):
                raise ValueError("共享循环表的表头或原型字段与映射不一致")
            headers.append(header)
        sections.append((table, headers))
    return sections


def split_combined_tables(sections):
    """Give independent record groups their own repeating header in the output.

    Call after every source-indexed operation, including photo filling. Moving
    the existing rows preserves their text, cells, bookmarks and column widths.
    """
    repairs = []
    for table, headers in sections:
        previous = table
        for header in headers[1:]:
            new_table = ET.Element(tag("tbl"), attrib=dict(table.attrib))
            for child in table:
                if child.tag in {tag("tblPr"), tag("tblGrid")}:
                    new_table.append(deepcopy(child))
            # Start from the current remaining section, not an expanded row number.
            for row in list(previous)[previous.index(header):]:
                new_table.append(row)
            # Adjacent Word tables can coalesce on save. A minimal paragraph
            # keeps them distinct, while staying with the next section header.
            separator = ET.Element(tag("p"))
            props = ET.SubElement(separator, tag("pPr"))
            set_property(props, "snapToGrid", "0")
            set_property(props, "keepNext", "1")
            ET.SubElement(props, tag("spacing"), {
                tag("before"): "0", tag("after"): "0", tag("line"): "20", tag("lineRule"): "exact"})
            previous.addnext(separator)
            separator.addnext(new_table)
            previous = new_table
        for header in headers:
            props = header.find(tag("trPr"))
            if props is None:
                props = ET.Element(tag("trPr"))
                header.insert(0, props)
            set_property(props, "tblHeader", "1")
            set_property(props, "cantSplit", "1")
            for paragraph in header.xpath(".//w:p", namespaces=NS):
                props = paragraph.find(tag("pPr"))
                if props is None:
                    props = ET.Element(tag("pPr"))
                    paragraph.insert(0, props)
                set_property(props, "keepNext", "1")
        repairs.append("共享循环表按独立表头拆分，各段表头随首行并跨页重复")
    return repairs


def new_paragraph(text, *, bold=False, page_break=False, keep_lines=False):
    """An appendix paragraph matching the source's Song type and normal rhythm."""
    p = ET.Element(tag("p"))
    pp = ET.SubElement(p, tag("pPr"))
    set_property(pp, "snapToGrid", "0")
    set_property(pp, "widowControl", "1")
    if keep_lines:
        set_property(pp, "keepLines", "1")
    if page_break:
        ET.SubElement(pp, tag("pageBreakBefore"))
    spacing = ET.SubElement(pp, tag("spacing"))
    spacing.set(tag("after"), "120")
    if bold:
        spacing.set(tag("before"), "160")
    spacing.set(tag("line"), "300")
    spacing.set(tag("lineRule"), "auto")
    if bold:
        ET.SubElement(pp, tag("keepNext"))
    r = ET.SubElement(p, tag("r"))
    rp = ET.SubElement(r, tag("rPr"))
    fonts = ET.SubElement(rp, tag("rFonts"))
    for name in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(tag(name), "宋体")
    set_property(rp, "sz", "24" if bold else "21")
    set_property(rp, "szCs", "24" if bold else "21")
    set_property(rp, "color", "000000")
    if bold:
        ET.SubElement(rp, tag("b"))
    ET.SubElement(r, tag("t")).text = text
    return p


def append_completion_notes(root, mapping, collected, used, period):
    body = root.find(tag("body"))
    final_section = body.find(tag("sectPr"))
    at = body.index(final_section) if final_section is not None else len(body)
    # The reference ends with an empty appendix section. Use that existing page
    # instead of inserting a page break after its otherwise empty paragraph.
    page_break = True
    preceding = list(body)[:at]
    section_ends = [i for i, node in enumerate(preceding) if node.find("w:pPr/w:sectPr", NS) is not None]
    if section_ends:
        tail = preceding[section_ends[-1] + 1:]
        if all(node.tag == tag("p") and not text_of(node).strip()
               and not node.xpath(".//w:drawing | .//w:pict | .//w:fldChar | .//w:bookmarkStart", namespaces=NS)
               for node in tail):
            for node in tail:
                body.remove(node)
            at = body.index(final_section) if final_section is not None else len(body)
            page_break = False
    notes = [new_paragraph("数据来源与待填写事项", bold=True, page_break=page_break),
             new_paragraph(f"报告期间为 {period}。黄色“待填”位置请补入对应电站和期间的数据。空白不代表零值，未取得记录不代表无事项。年度台账按 1 月至报告月列示，累计值另行核对。")]
    snapshot = collected.get("platform_snapshot") or {}
    has_snapshot_values = False
    if snapshot:
        notes.append(new_paragraph("Power+ 当前档案", bold=True))
        fetched = snapshot.get("captured_at") or snapshot.get("fetched_at") or collected.get("fetched_at") or collected.get("collected_at")
        scope = f"查询时间：{str(fetched).replace('T', ' ')}。" if fetched else ""
        items = snapshot.get("items", snapshot.get("fields", []))
        if isinstance(items, dict):
            items = [{"label": key, **(value if isinstance(value, dict) else {"value": value, "status": "real"})} for key, value in items.items()]
        has_snapshot_values = any(isinstance(item, dict) and is_available({"status": "real", **item})
                                  and isinstance(item.get("value"), (str, int, float)) for item in items)
        explanation = ("以下为查询时的平台档案，仅供核对；容量等当前档案值不自动作为历史报告月份的数据。"
                       if has_snapshot_values else "本次未取得可核验的当前档案，不能据此填写容量等属性。")
        notes.append(new_paragraph(scope + explanation))
        for item in items:
            if not isinstance(item, dict) or not is_available({"status": "real", **item}):
                continue
            value = item.get("value")
            if isinstance(value, (str, int, float)):
                label = item.get("label") or item.get("name") or item.get("key", "档案字段")
                unit = item.get("unit") or ""
                unit = f"（{unit}）" if "待" in unit else unit
                notes.append(new_paragraph(f"{label}：{value}{unit}"))
        if snapshot.get("note"):
            notes.append(new_paragraph(str(snapshot["note"])))
    for observation in collected.get("observations", []):
        if observation.get("status") == "real" and isinstance(observation.get("value"), (str, int, float)):
            notes.append(new_paragraph(f"{observation.get('label', '平台核验')}：{observation['value']}。{observation.get('reason') or ''}"))
    real = [(f, item) for f, item in collected.get("fields", {}).items()
            if isinstance(item, dict) and item.get("status") == "real" and is_available(item) and f != "F090"]
    labels = {f["id"]: f["label"] for f in mapping["fields"]}
    def reader_reason(reason):
        # The detailed runtime JSON keeps stable IDs; the report uses field names.
        return TOKEN.sub(lambda match: labels.get(match.group(), '相关字段'),str(reason))
    if real:
        notes.append(new_paragraph("已取得的报告数据", bold=True))
        for fid, item in real:
            source = item.get("source", "Power+")
            if isinstance(source, dict):
                source = source.get("label") or source.get("method") or source.get("platform") or "Power+"
            value, _ = display_value(fid, item, labels[fid], False)
            unit = item.get("unit") or ""
            notes.append(new_paragraph(f"{labels[fid]}：{value}{'' if unit and value.endswith(unit) else unit}；来源：{source}。"))
    notes.append(new_paragraph("待填写项目", bold=True, page_break=has_snapshot_values))
    missing = {item["field_id"] for item in used if not item["filled"]}
    pending = []
    for definition in mapping["fields"]:
        fid = definition["id"]
        if fid not in missing or definition["kind"] == "循环列":
            continue
        item = field_item(collected.get("fields", {}).get(fid))
        reason=item.get("reason") or "尚未取得当期有效数据或人工材料"
        missing_inputs=(item.get('source') or {}).get('missing_inputs',[])
        if missing_inputs:
            details=list(dict.fromkeys(labels.get(v['field_id'],'相关字段')+'（'+v['period']+'）' for v in missing_inputs))
            reason='缺少计算输入：'+'、'.join(details)+'；具体来源与依赖见缺项指引。'
        pending.append({"field_id": fid, "label": definition["label"], "reason": reason})
    # Group by reason so that a 90-field template does not create 90 prose lines.
    by_reason = {}
    for item in pending:
        by_reason.setdefault(item["reason"], []).append(item["label"])
    for reason, field_labels in by_reason.items():
        notes.append(new_paragraph("、".join(field_labels) + "：" + reader_reason(reason).rstrip("。") + "。"))
    pending_groups = {}
    for group in mapping["repeat_groups"]:
        if not missing.intersection(group["field_ids"]):
            continue
        payload = collected.get("repeat_groups", {}).get(group["key"], {})
        label = labels[group["field_ids"][0]].split("·")[0]
        reason = payload.get("reason") or "按当期真实清单补充，现有占位行不是实际记录数量"
        pending_groups.setdefault(reason, []).append(label)
    for reason, group_labels in pending_groups.items():
        notes.append(new_paragraph("、".join(group_labels) + "：" + reader_reason(reason).rstrip("。") + "。"))
    notes.append(new_paragraph("目录与附件", bold=True))
    notes.append(new_paragraph("编辑完成后，请在 Word 中更新整个目录并核对页码。照片按当期真实材料补充；巡检附表是否另行交付待确认。", keep_lines=True))
    for p in notes:
        body.insert(at, p)
        at += 1
    return pending


def mark_fields_for_update(settings):
    set_property(settings, "updateFields", "true")


def clear_stale_toc(root):
    """Retain the TOC fields, but do not publish old pages or absent appendices."""
    removed = 0
    cleared = 0
    for sdt in root.xpath(".//w:sdt[w:sdtPr/w:docPartObj/w:docPartGallery[@w:val='Table of Contents']]", namespaces=NS):
        for paragraph in sdt.xpath("w:sdtContent/w:p", namespaces=NS):
            if "六. 附表" in text_of(paragraph):
                paragraph.getparent().remove(paragraph)
                removed += 1
                continue
            awaiting_separator = False
            in_result = False
            for run in paragraph.findall(tag("r")):
                instruction = "".join(run.xpath("w:instrText/text()", namespaces=NS))
                if "PAGEREF" in instruction:
                    awaiting_separator = True
                field = run.find(tag("fldChar"))
                if field is not None:
                    kind = field.get(tag("fldCharType"))
                    if kind == "separate" and awaiting_separator:
                        in_result = True
                        awaiting_separator = False
                    elif kind == "end":
                        in_result = False
                if in_result:
                    for node in run.findall(tag("t")):
                        node.text = "待更新"
                        cleared += 1
    return {"removed_absent_appendix_entries": removed, "cleared_cached_page_numbers": cleared}


def sync_toc_pages(docx: Path, pdf: Path) -> dict:
    """Patch TOC display pages from this document's actual rendered PDF outline.

    A missing or ambiguous destination leaves all caches unchanged. This never
    asks LibreOffice to save the Word file and never changes a field instruction.
    """
    from pypdf import PdfReader

    docx, pdf = Path(docx), Path(pdf)
    if docx.stem != pdf.stem:
        return {"updated": False, "reason": "PDF文件名与报告不一致，未更新目录"}
    reader = PdfReader(pdf)
    destinations = {}

    def normalize(value):
        return re.sub(r"\s+", "", str(value))

    def visit(items):
        for item in items:
            if isinstance(item, list):
                visit(item)
            else:
                page = reader.get_destination_page_number(item)
                if page is not None and 0 <= page < len(reader.pages):
                    destinations.setdefault(normalize(item.get("/Title", "")), set()).add(page)

    visit(reader.outline)
    with ZipFile(docx) as archive:
        original_xml = archive.read("word/document.xml")
        root = ET.fromstring(original_xml)
        slots = []
        unresolved = []
        for paragraph in root.xpath(".//w:sdt[w:sdtPr/w:docPartObj/w:docPartGallery[@w:val='Table of Contents']]/w:sdtContent/w:p", namespaces=NS):
            title = []
            bookmark = None
            in_result = False
            caches = []
            for run in paragraph.xpath(".//w:r", namespaces=NS):
                instruction = "".join(run.xpath("w:instrText/text()", namespaces=NS))
                match = re.search(r"\bPAGEREF\s+(\S+)", instruction)
                if match:
                    bookmark = match.group(1)
                field = run.find(tag("fldChar"))
                if field is not None and bookmark:
                    kind = field.get(tag("fldCharType"))
                    if kind == "separate":
                        in_result = True
                    elif kind == "end":
                        in_result = False
                nodes = run.findall(tag("t"))
                if in_result:
                    caches.extend(nodes)
                elif bookmark is None:
                    title.extend(node.text or "" for node in nodes)
            if bookmark:
                label = "".join(title).strip()
                targets = destinations.get(normalize(label), set())
                if len(targets) != 1 or not caches:
                    unresolved.append({'title':label,'bookmark':bookmark,'reason':'目录项无法唯一对应PDF目标','nodes':caches})
                    continue
                page_index = next(iter(targets))
                slots.append({"bookmark": bookmark, "title": label, "page": reader.page_labels[page_index],
                              "physical_page": page_index + 1, "nodes": caches,
                              "previous": "".join(node.text or "" for node in caches)})
        if not slots and not unresolved:
            return {"updated": False, "reason": "没有可更新的PAGEREF目录缓存"}
        evidence = [{key: value for key, value in slot.items() if key != "nodes"} for slot in slots]
        unresolved_evidence=[{k:v for k,v in item.items() if k!='nodes'} for item in unresolved]
        unresolved_changed=any(''.join(n.text or '' for n in item['nodes']) not in {'','待更新'} for item in unresolved)
        if all(slot['previous']==slot['page'] for slot in slots) and not unresolved_changed:
            return {'updated':False,'reason':'可定位目录已与PDF目标页码一致','entries':evidence,'unresolved_entries':unresolved_evidence}
        for item in unresolved:
            if item['nodes']:
                item['nodes'][0].text='待更新'
                for node in item['nodes'][1:]:node.text=''
        for slot in slots:
            slot["nodes"][0].text = slot["page"]
            for node in slot["nodes"][1:]:
                node.text = ""
        patched = ET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        temp = docx.with_name(docx.name + ".toc-" + uuid.uuid4().hex)
        try:
            with ZipFile(temp, "x") as output:
                for entry in archive.infolist():
                    content = patched if entry.filename == "word/document.xml" else archive.read(entry.filename)
                    output.writestr(deepcopy(entry), content)
            with ZipFile(temp) as output:
                if any(archive.read(name) != output.read(name) for name in archive.namelist() if name != "word/document.xml"):
                    raise ValueError("目录更新意外改动其他文档部件")
            os.replace(temp, docx)
        finally:
            if temp.exists():
                temp.unlink()
    return {"updated": True, "source": "实际PDF outline目标及页面标签", "pdf": str(pdf.resolve()),
            "pdf_sha256": sha256(pdf.read_bytes()).hexdigest(), "entries": evidence, "unresolved_entries": unresolved_evidence}


def remove_plain_spacers_before(heading):
    """Drop only format-only gaps before a newly imposed page break.

    A gap can spill onto a page of its own just before pageBreakBefore starts
    another page. Runs, markers, sections and explicit pagination are retained.
    """
    allowed = {tag(name) for name in ("jc", "rPr", "spacing", "snapToGrid", "keepNext")}
    previous = heading.getprevious()
    removed = 0
    while previous is not None and previous.tag == tag("p"):
        props = previous.find(tag("pPr"))
        if (any(node.tag != tag("pPr") for node in previous)
                or (props is not None and any(node.tag not in allowed for node in props))):
            break
        before = previous.getprevious()
        previous.getparent().remove(previous)
        previous = before
        removed += 1
    return removed


def repair_template_layout(root, mapping):
    """Repair observed reference defects in the output copy, keeping page geometry."""
    body = root.find(tag("body"))
    children = list(body)
    notice_at = next(i for i, node in enumerate(children) if text_of(node) == "阅前须知")
    for node in children[:notice_at]:
        if node.tag != tag("p"):
            continue
        pp = node.find(tag("pPr"))
        if pp is None:
            pp = ET.SubElement(node, tag("pPr"))
        set_property(pp, "snapToGrid", "0")
        # Remove both modern and legacy copies of the same empty cover frame.
        for picture in node.xpath(".//w:pict[not(.//w:t)] | .//w:drawing[not(.//w:t)]", namespaces=NS):
            picture.getparent().remove(picture)
        spacing = pp.find(tag("spacing"))
        if spacing is None:
            spacing = ET.SubElement(pp, tag("spacing"))
        value = text_of(node).strip()
        if not value:
            spacing.attrib.clear()
            for key, val in {"before": "0", "after": "0", "line": "80", "lineRule": "exact"}.items():
                spacing.set(tag(key), val)
        elif value in {"内部资料", "禁止外传"}:
            for t in node.xpath(".//w:t", namespaces=NS):
                t.text = (t.text or "").strip()
            ind = pp.find(tag("ind"))
            if ind is not None:
                pp.remove(ind)
            set_property(pp, "jc", "right")
            spacing.attrib.clear()
            spacing.set(tag("line"), "240")
            spacing.set(tag("lineRule"), "auto")
        elif value == "F001":
            spacing.set(tag("before"), "1440")
            spacing.set(tag("after"), "360")
        elif "运维月报" in value:
            spacing.set(tag("before"), "0")
            spacing.set(tag("after"), "1000")
        else:
            spacing.set(tag("before"), "120")
            spacing.set(tag("after"), "120")
            spacing.set(tag("line"), "300")
            spacing.set(tag("lineRule"), "auto")
    for paragraph in root.xpath(".//w:p", namespaces=NS):
        text = text_of(paragraph)
        inside_table = any(n.tag == tag("tc") for n in paragraph.iterancestors())
        pp = paragraph.find(tag("pPr"))
        if pp is None:
            pp = ET.Element(tag("pPr"))
            paragraph.insert(0, pp)
        if TOKEN.search(text) or inside_table or text.startswith("接收到报告及附录"):
            set_property(pp, "snapToGrid", "0")
        if text == "阅前须知":
            set_property(pp, "snapToGrid", "0")
            spacing = pp.find(tag("spacing"))
            if spacing is None:
                spacing = ET.SubElement(pp, tag("spacing"))
            spacing.set(tag("before"), "300")
            spacing.set(tag("after"), "240")
        if re.match(r"^(?:[一二三四五]\. |[1-4]\.[1-5])", text) or text in {"运行概况", "阅前须知", "下月工作计划", "本月故障处理情况："}:
            set_property(pp, "keepNext", "1")
        if paragraph.getnext() is not None and paragraph.getnext().tag == tag("tbl") and text:
            set_property(pp, "keepNext", "1")
    tables = body.findall(tag("tbl"))
    for index, table in enumerate(tables):
        if index == mapping["photo_region"]["table_index"]:
            continue
        # Dynamic tables must flow with their headings; floating anchors ignore keepNext.
        table_props = table.find(tag('tblPr'))
        if table_props is not None:
            floating_tables=list(table_props.findall(tag('tblpPr')))
            for floating in floating_tables:
                table_props.remove(floating)
            if floating_tables:
                heading=table.getprevious()
                if heading is not None and heading.tag==tag('p') and text_of(heading).strip():
                    pp=heading.find(tag('pPr'))
                    if pp is None:
                        pp=ET.Element(tag('pPr'));heading.insert(0,pp)
                    set_property(pp,'pageBreakBefore','1')
                    set_property(pp,'keepNext','1')
                    remove_plain_spacers_before(heading)
        first_row = table.find(tag("tr"))
        trpr = first_row.find(tag("trPr"))
        if trpr is None:
            trpr = ET.Element(tag("trPr"))
            first_row.insert(0, trpr)
        set_property(trpr, "tblHeader", "1")
        for cell in table.xpath("w:tr/w:tc", namespaces=NS):
            props = cell.find(tag("tcPr"))
            width = props.find(tag("tcW")) if props is not None else None
            if width is not None and int(width.get(tag("w"), "9999")) <= 817:
                margins = props.find(tag("tcMar"))
                if margins is None:
                    margins = ET.SubElement(props, tag("tcMar"))
                for edge in ("left", "right"):
                    element = margins.find(tag(edge))
                    if element is None:
                        element = ET.SubElement(margins, tag(edge))
                    element.set(tag("w"), "40")
                    element.set(tag("type"), "dxa")
    photos = tables[mapping["photo_region"]["table_index"]]
    heading = photos.getprevious()
    set_property(heading.find(tag("pPr")), "pageBreakBefore", "1")
    remove_plain_spacers_before(heading)
    for row in photos.findall(tag("tr")):
        props = row.find(tag("trPr"))
        if props is None:
            props = ET.Element(tag("trPr"))
            row.insert(0, props)
        height = props.find(tag("trHeight"))
        if height is None:
            height = ET.SubElement(props, tag("trHeight"))
        height.set(tag("val"), "2200")
        height.set(tag("hRule"), "atLeast")
        set_property(props, "cantSplit", "1")
        for cell in row.findall(tag("tc")):
            set_property(cell.find(tag("tcPr")), "vAlign", "center")
            for paragraph in cell.findall(tag("p")):
                set_property(paragraph.find(tag("pPr")), "jc", "center")
    return ["封面空段与须知网格行距修复", "动态表格随文排版、窄列内边距与标题随表", "新分页标题前的纯格式空段清理", "八个照片槽统一高度并独立起页"]


def fill(plan: dict, collected: dict, out_path: Path) -> dict:
    """Create a new DOCX from a resolved plan and explicitly classified values."""
    template = Path(plan["template_file"])
    out_path = Path(out_path).resolve()
    if out_path == template.resolve() or out_path.exists():
        raise ValueError("输出必须为新文件，不覆盖模板或已有报告")
    if plan.get("station_id") and collected.get("station_id") not in (None, plan["station_id"]):
        raise ValueError("取数结果与请求电站不一致")
    if collected.get("period") not in (None, plan["period"]):
        raise ValueError("取数结果与请求月份不一致")
    mapping_path = template.parent / "模板字段映射.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    digest = sha256(template.read_bytes()).hexdigest()
    if digest != mapping["template_sha256"] or digest != plan["template_sha256"]:
        raise ValueError("共享模板摘要变化，须先核验映射")
    labels = {f["id"]: f["label"] for f in mapping["fields"]}
    original_rprs = {item["field_id"]: item["original_rPr_xml"] for item in mapping.get("placeholder_display_overrides", [])}
    with ZipFile(template) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        settings = ET.fromstring(z.read("word/settings.xml"))
        sections_before = [ET.tostring(node) for node in root.xpath(".//w:sectPr", namespaces=NS)]
        layout_repairs = repair_template_layout(root, mapping)
        combined_sections = combined_table_sections(root, mapping)
        used = []
        counts = expand_groups(root, mapping, collected, plan["period"], labels, original_rprs, used)
        replace_region(root, collected.get("fields", {}), labels, original_rprs, used)
        photo_patches,photo_result=embed_photos(root,mapping,collected.get('photos',{}),z,used)
        layout_repairs.extend(split_combined_tables(combined_sections))
        missed = set(labels) - {item["field_id"] for item in used}
        if missed:
            raise ValueError("映射字段未在模板找到：" + ",".join(sorted(missed)))
        unresolved_tokens = TOKEN.findall(text_of(root))
        if unresolved_tokens:
            raise ValueError("存在未处理的字段标记：" + ",".join(sorted(set(unresolved_tokens))))
        pending = append_completion_notes(root, mapping, collected, used, plan["period"])
        toc = clear_stale_toc(root)
        mark_fields_for_update(settings)
        sections_after = [ET.tostring(node) for node in root.xpath(".//w:sectPr", namespaces=NS)]
        if sections_before != sections_after:
            raise ValueError("分节或页面设置发生非预期改变")
        patches = {"word/document.xml": ET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True),
                   "word/settings.xml": ET.tostring(settings, xml_declaration=True, encoding="UTF-8", standalone=True)}
        patches.update(photo_patches)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(out_path, "x") as output:
            for entry in z.infolist():
                # ZipFile mutates ZipInfo.header_offset while writing; copying
                # it keeps the source archive readable for the fidelity check.
                output.writestr(deepcopy(entry), patches.get(entry.filename, z.read(entry.filename)))
            for name,payload in patches.items():
                if name not in z.namelist():output.writestr(name,payload)
        with ZipFile(out_path) as output:
            preserved = [entry.filename for entry in z.infolist() if entry.filename not in patches]
            if any(z.read(name) != output.read(name) for name in preserved):
                raise ValueError("保留的文档部件发生非预期改变")
    return {"docx": str(out_path), "template_sha256": digest, "placeholder_occurrences": sum(not item["filled"] for item in used),
            "filled_occurrences": sum(item["filled"] for item in used), "repeat_row_counts": counts,
            "pending_fields": sorted({item["field_id"] for item in used if not item["filled"]}),
            "pending_details": pending, "sections_preserved": len(sections_before),
            "unchanged_package_parts": len(preserved), "changed_package_parts": list(patches),
            "toc": toc, "layout_repairs": layout_repairs,
            "photos": photo_result,
            "real_value_fields":sorted({item['field_id'] for item in used if item['filled'] and item['status']=='real'}),
            "field_refresh": "Word打开后更新整个目录", "formal_report_ready": False}
