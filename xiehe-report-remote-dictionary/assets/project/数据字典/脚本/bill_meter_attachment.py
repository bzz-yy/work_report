"""Finite, read-only parser for verified electricity-bill meter workbooks.

No Excel evaluation, cached-value fallback, macros, historical-report replay or
station constants. Meter scope and approved periods come from a station policy.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import posixpath
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


METHOD = "power.electricitybill.attachment_meter.v1"
GENERATION_SHEET = "01 分布式光伏发电电量记录单（与客户结算用）"
EXPORT_SHEET = "02 分布式光伏上网电量记录单（与客户结算用）"
NOTICE_SHEET = "05 分布式光伏电费通知单（与客户结算用）"
EXAMPLE_SHEET = "03 分布式光伏其他光伏站用电量单（与客户结算用）"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CACHE_TOLERANCE = Decimal("0.000001")
METRICS = ("generation_kwh", "consumption_kwh", "grid_kwh", "station_use_kwh")


class AttachmentContractError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise AttachmentContractError(code, message)


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))


def decimal(value):
    require(value is not None and not isinstance(value, bool), "MISSING_INPUT", "缺少数字输入")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AttachmentContractError("INVALID_NUMBER", "原始单元格不是数字") from exc
    require(number.is_finite(), "INVALID_NUMBER", "原始单元格不是有限数值")
    return number


def period_valid(period):
    return isinstance(period, str) and re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", period) is not None


def validate_policy(policy, station_code):
    require(isinstance(policy, dict), "POLICY_ERROR", "计量政策不是对象")
    require(str(policy.get("station_code")) == str(station_code), "POLICY_ERROR", "计量政策串站")
    require(policy.get("schema_version") == 1, "POLICY_ERROR", "不支持的计量政策版本")
    for key in ("allowed_container_periods", "allowed_metric_periods"):
        periods = policy.get(key)
        require(isinstance(periods, list) and periods and len(periods) == len(set(periods))
                and all(period_valid(p) for p in periods), "POLICY_ERROR", "政策期间无效：" + key)
    aliases = policy.get("station_aliases")
    require(isinstance(aliases, list) and aliases and all(isinstance(a, str) and len(normalized(a)) >= 2
                                                       for a in aliases), "POLICY_ERROR", "政策缺电站名称依据")
    for key in ("generation_meters", "export_meters"):
        meters = policy.get(key)
        require(isinstance(meters, list) and len(meters) == 2, "POLICY_ERROR", "有限模板必须明确两块表：" + key)
        require(all(isinstance(m, dict) and set(m) == {"meter_id", "multiplier"} for m in meters),
                "POLICY_ERROR", "表号/倍率政策字段错误")
        ids = [m["meter_id"] for m in meters]
        require(all(isinstance(i, str) and re.fullmatch(r"\d{8,30}", i) for i in ids)
                and len(set(ids)) == 2, "POLICY_ERROR", "政策表号缺失或重复")
        require(all(decimal(m["multiplier"]) > 0 for m in meters), "POLICY_ERROR", "政策倍率必须大于0")
    auxiliary = policy.get("auxiliary_station_use")
    if auxiliary is not None:
        required = {"sheet_name", "date_column", "value_column", "first_data_row", "unit_header_cell",
                    "unit_header_text", "title_cell", "title_text", "purpose_text", "allowed_business_periods"}
        require(isinstance(auxiliary, dict) and set(auxiliary) == required, "POLICY_ERROR", "辅助月台账政策字段不完整")
        require(all(re.fullmatch(r"[A-Z]{1,3}", auxiliary[k] or "") for k in ("date_column", "value_column")),
                "POLICY_ERROR", "辅助台账列无效")
        require(isinstance(auxiliary["first_data_row"], int) and not isinstance(auxiliary["first_data_row"], bool)
                and auxiliary["first_data_row"] >= 1, "POLICY_ERROR", "辅助台账起始行无效")
        require(all(period_valid(p) for p in auxiliary["allowed_business_periods"]), "POLICY_ERROR", "辅助台账业务月份无效")
    return policy


def load_meter_policy(path, station_code):
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise AttachmentContractError("POLICY_ERROR", "计量政策文件不存在、不可读或不是有效JSON") from exc
    return validate_policy(policy, station_code)


def download_bill_attachment(client, url, directory):
    """Use the CLI's auth only at the verified API host, never at storage."""
    require(isinstance(url, str), "ATTACHMENT_URI", "附件下载URI不是字符串")
    parsed = urllib.parse.urlsplit(url)
    parameters = urllib.parse.parse_qs(parsed.query)
    require(parsed.scheme == "https" and parsed.hostname == "powersaber-xhyw.cnecloud.com"
            and parsed.port in {None, 443}
            and parsed.path == "/api/blade-resource/minio/endpoint/getFile"
            and set(parameters) == {"fileName"} and len(parameters["fileName"]) == 1
            and not parsed.username and not parsed.password and not parsed.fragment,
            "ATTACHMENT_URI", "附件下载URI不是已核验的Power+文件接口")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(urllib.request.Request(url, headers=client._headers()), timeout=40)
    except urllib.error.HTTPError as redirect:
        if redirect.code not in {301, 302, 303, 307, 308}:
            raise
        location = urllib.parse.urljoin(url, redirect.headers["Location"])
        storage = urllib.parse.urlsplit(location)
        require(storage.scheme == "https" and storage.hostname == "powers3-xhyw.cnecloud.com"
                and storage.port in {None, 443} and not storage.username and not storage.password and not storage.fragment,
                "ATTACHMENT_URI", "附件重定向到未核验对象存储")
        response = opener.open(urllib.request.Request(location), timeout=40)
    with response:
        length = response.headers.get("Content-Length")
        chunks, count = [], 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            count += len(chunk)
            require(count <= 32 * 1024 * 1024, "UNSUPPORTED_WORKBOOK", "附件超过32MiB")
            chunks.append(chunk)
    payload = b"".join(chunks)
    if length and int(length) != len(payload):
        raise OSError("附件下载不完整")
    require(payload.startswith(b"PK"), "UNSUPPORTED_WORKBOOK", "附件字节不是XLSX容器")
    digest = hashlib.sha256(payload).hexdigest()
    target = Path(directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    path = target / (digest + ".xlsx")
    if path.exists():
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                "SOURCE_CONFLICT", "已有同摘要文件的内容不同")
    else:
        with path.open("xb") as stream:
            stream.write(payload)
    return path


class Workbook:
    def __init__(self, path):
        self.path = Path(path).resolve()
        require(self.path.is_file() and self.path.stat().st_size <= 32 * 1024 * 1024,
                "UNSUPPORTED_WORKBOOK", "附件不存在或超过32MiB")
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.proofs = {}
        self.cache_checks = []
        self.sheets = {}
        try:
            with zipfile.ZipFile(self.path) as archive:
                members = archive.infolist()
                require(len(members) <= 5000 and sum(m.file_size for m in members) <= 64 * 1024 * 1024,
                        "UNSUPPORTED_WORKBOOK", "附件解压结构超出有限支持范围")
                names = archive.namelist()
                require(len(names) == len(set(names)), "UNSUPPORTED_WORKBOOK", "附件含重复ZIP成员")
                require(not any("vbaProject" in n for n in names), "UNSUPPORTED_WORKBOOK", "不支持含宏附件")

                def xml(name):
                    require(name in names and archive.getinfo(name).file_size <= 10 * 1024 * 1024,
                            "UNSUPPORTED_WORKBOOK", "XLSX XML成员缺失或过大")
                    return ET.fromstring(archive.read(name))

                workbook = xml("xl/workbook.xml")
                properties = workbook.find("s:workbookPr", NS)
                self.epoch = datetime(1904, 1, 1) if properties is not None and properties.get("date1904") in {"1", "true"} else datetime(1899, 12, 30)
                shared = []
                if "xl/sharedStrings.xml" in names:
                    shared = ["".join(t.text or "" for t in item.findall(".//s:t", NS))
                              for item in xml("xl/sharedStrings.xml").findall("s:si", NS)]
                relationships = {r.get("Id"): r for r in xml("xl/_rels/workbook.xml.rels")}
                for sheet in workbook.findall("s:sheets/s:sheet", NS):
                    relation = relationships.get(sheet.get("{" + REL + "}id"))
                    require(relation is not None and relation.get("TargetMode") != "External",
                            "UNSUPPORTED_WORKBOOK", "工作表关系缺失或为外部关系")
                    target = relation.get("Target", "")
                    target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
                    require(target.startswith("xl/worksheets/"), "UNSUPPORTED_WORKBOOK", "工作表XML路径无效")
                    cells = {}
                    for node in xml(target).findall(".//s:sheetData/s:row/s:c", NS):
                        address = node.get("r")
                        require(address not in cells, "UNSUPPORTED_WORKBOOK", "工作表单元格重复")
                        value = node.find("s:v", NS)
                        raw = value.text if value is not None else None
                        kind = node.get("t", "n")
                        if kind == "s":
                            require(raw is not None and raw.isdigit() and int(raw) < len(shared),
                                    "UNSUPPORTED_WORKBOOK", "共享字符串索引错误")
                            text = shared[int(raw)]
                        elif kind == "inlineStr":
                            text = "".join(t.text or "" for t in node.findall(".//s:t", NS))
                        else:
                            text = raw
                        formula = node.find("s:f", NS)
                        cells[address] = {"raw": raw, "text": text, "kind": kind,
                                          "formula": None if formula is None else "=" + (formula.text or ""),
                                          "formula_type": None if formula is None else formula.get("t")}
                    require(sheet.get("name") not in self.sheets, "UNSUPPORTED_WORKBOOK", "工作表名称重复")
                    self.sheets[sheet.get("name")] = cells
        except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
            raise AttachmentContractError("UNSUPPORTED_WORKBOOK", "不是受支持的完整XLSX文件") from exc

    def cell(self, sheet, address):
        require(sheet in self.sheets, "MISSING_SHEET", "缺少已核验工作表：" + sheet)
        cell = self.sheets[sheet].get(address)
        require(cell is not None and cell["text"] is not None, "MISSING_INPUT", sheet + "!" + address + "缺值")
        self.proofs[sheet + "!" + address] = {"sheet": sheet, "cell": address, **cell}
        return cell

    def text(self, sheet, address):
        cell = self.cell(sheet, address)
        require(cell["formula"] is None, "UNEXPECTED_FORMULA", "身份或日期文字不可由未核公式生成")
        return cell["text"]

    def number(self, sheet, address):
        cell = self.cell(sheet, address)
        require(cell["formula"] is None and cell["kind"] == "n", "UNEXPECTED_FORMULA", "原始表底必须为直接数字")
        return decimal(cell["raw"])

    def date(self, sheet, address):
        cell = self.cell(sheet, address)
        require(cell["formula"] is None, "UNEXPECTED_FORMULA", "日期不可依赖未核公式")
        if cell["kind"] == "n":
            serial = decimal(cell["raw"])
            require(serial == serial.to_integral_value() and 40000 <= serial <= 80000,
                    "WRONG_PERIOD", "日期不是支持的午夜Excel日期")
            return self.epoch + timedelta(days=int(serial))
        try:
            when = datetime.fromisoformat(cell["text"])
        except (ValueError, TypeError) as exc:
            raise AttachmentContractError("WRONG_PERIOD", "日期格式未核验") from exc
        require(when.hour == when.minute == when.second == when.microsecond == 0 and when.tzinfo is None,
                "WRONG_PERIOD", "日期不是本地午夜")
        return when

    def formula(self, sheet, address, expected, calculated):
        cell = self.cell(sheet, address)
        choices = expected if isinstance(expected, list) else [expected]
        require(cell["formula_type"] not in {"shared", "array", "dataTable"}
                and normalized(cell["formula"]) in [normalized(c) for c in choices],
                "UNEXPECTED_FORMULA", sheet + "!" + address + "公式不在有限契约内")
        cached = decimal(cell["raw"])
        delta = abs(cached - calculated)
        require(delta <= CACHE_TOLERANCE, "CACHE_MISMATCH", sheet + "!" + address + "缓存与原输入重算不一致")
        self.cache_checks.append({"sheet": sheet, "cell": address, "computed": str(calculated),
                                  "cached": str(cached), "delta": str(delta), "tolerance": str(CACHE_TOLERANCE)})

    def multiplier(self, sheet, address):
        cell = self.cell(sheet, address)
        if cell["formula"] is None:
            result = self.number(sheet, address)
        else:
            expression = cell["formula"][1:]
            require(re.fullmatch(r"[\d.\s()+*/-]{1,80}", expression) is not None,
                    "UNEXPECTED_FORMULA", "倍率公式不是有限纯数字表达式")
            try:
                tree = ast.parse(expression, mode="eval")
            except SyntaxError as exc:
                raise AttachmentContractError("UNEXPECTED_FORMULA", "倍率公式语法错误") from exc
            require(len(list(ast.walk(tree))) <= 25, "UNEXPECTED_FORMULA", "倍率公式过长")

            def evaluate(node):
                if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                    return decimal(ast.get_source_segment(expression, node))
                require(isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)),
                        "UNEXPECTED_FORMULA", "倍率公式运算不受支持")
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Add): return left + right
                if isinstance(node.op, ast.Sub): return left - right
                if isinstance(node.op, ast.Mult): return left * right
                require(right != 0, "INVALID_NUMBER", "倍率公式除零")
                return left / right

            result = evaluate(tree.body)
            self.formula(sheet, address, cell["formula"], result)
        require(0 < result <= Decimal("1000000000"), "INVALID_NUMBER", "倍率非正或超出支持范围")
        return result


def _month_from_text(text):
    match = re.search(r"(20\d{2})年(\d{1,2})月", normalized(text))
    require(match is not None, "WRONG_PERIOD", "来源文字缺明确年月")
    period = f"{match[1]}-{int(match[2]):02d}"
    require(period_valid(period), "WRONG_PERIOD", "来源文字月份无效")
    return period


def _auxiliary_rows(book, policy, container_period, station_code):
    config = policy.get("auxiliary_station_use")
    if not config or config["sheet_name"] not in book.sheets:
        return []
    sheet = config["sheet_name"]
    title = book.text(sheet, config["title_cell"])
    require(normalized(config["title_text"]) in normalized(title), "AUXILIARY_SCOPE", "辅助台账标题不符")
    require(normalized(book.text(sheet, config["unit_header_cell"])) == normalized(config["unit_header_text"]),
            "AUXILIARY_SCOPE", "辅助台账电量单位/字段不符")
    cells = book.sheets[sheet]
    require(any(normalized(config["purpose_text"]) == normalized(c["text"]) for c in cells.values()),
            "AUXILIARY_SCOPE", "辅助台账缺光伏运营用电用途依据")
    date_column, value_column = config["date_column"], config["value_column"]
    rows = []
    for address, cell in cells.items():
        match = re.fullmatch(re.escape(date_column) + r"(\d+)", address)
        if (not match or int(match[1]) < config["first_data_row"] or cell["kind"] != "n"
                or cell["formula"] or cell["raw"] is None):
            continue
        # Dates are identified by Excel serial, not by numeric amount cells.
        raw = decimal(cell["raw"])
        if not 40000 <= raw <= 80000:
            continue
        when = book.date(sheet, address)
        period = when.strftime("%Y-%m")
        if period not in config["allowed_business_periods"]:
            continue
        require(period <= container_period, "WRONG_PERIOD", "辅助台账包含未来业务月份")
        value_cell = value_column + match[1]
        value = book.number(sheet, value_cell)
        require(value >= 0, "INVALID_NUMBER", "辅助台账电量为负")
        rows.append({"metric": "station_use_kwh", "value": str(value), "unit": "kWh",
                     "station_code": str(station_code), "business_period": period,
                     "container_period": container_period, "origin": "auxiliary_monthly_ledger",
                     "date_cell": address, "value_cell": value_cell, "sheet": sheet,
                     "source_date": when.date().isoformat(), "source_title": title,
                     "quality": "verified_ledger_source"})
    periods = [r["business_period"] for r in rows]
    require(len(periods) == len(set(periods)), "SOURCE_CONFLICT", "辅助台账同月存在多行，不取首条或相加")
    return rows


def parse_bill_meter_attachment(path, *, station_code, station_aliases, container_period,
                                meter_config, source_metadata=None):
    policy = validate_policy(meter_config, station_code)
    require(period_valid(container_period) and container_period in policy["allowed_container_periods"],
            "WRONG_PERIOD", "附件容器月份未在本站有限政策内")
    require(set(station_aliases) == set(policy["station_aliases"]), "POLICY_ERROR", "名称参数与计量政策不一致")
    book = Workbook(path)
    for sheet in (GENERATION_SHEET, EXPORT_SHEET, NOTICE_SHEET):
        require(sheet in book.sheets, "MISSING_SHEET", "缺少必需计量工作表：" + sheet)
    g, e, n = GENERATION_SHEET, EXPORT_SHEET, NOTICE_SHEET
    for sheet, address in ((g, "A2"), (e, "A2"), (n, "A3"), (n, "A5")):
        text = normalized(book.text(sheet, address))
        require(any(normalized(alias) in text for alias in station_aliases), "WRONG_STATION", "附件页眉或购电方不是本站")
    period = _month_from_text(book.text(n, "I3"))
    require(period == container_period and period in policy["allowed_metric_periods"],
            "WRONG_PERIOD", "附件计量月份与工单容器月份/允许月份不一致")
    year, month = map(int, period.split("-"))
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    for sheet in (g, e):
        label = normalized(book.text(sheet, "A2"))
        match = re.search(r"计量月份[:：]?(\d{1,2})月", label)
        require(match is not None and int(match[1]) == month and "单位:千瓦时" in label,
                "WRONG_PERIOD", "附件计量月份或千瓦时单位不符")
    for address, expected in (("I4", start), ("I5", end)):
        text = normalized(book.text(n, address))
        match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
        require(match is not None and tuple(map(int, match.groups())) == (expected.year, expected.month, expected.day),
                "WRONG_PERIOD", "通知单抄表日期不符")
    for sheet, starts, ends in ((g, ("D6", "D8", "D10", "D12"), ("D5", "D7", "D9", "D11")),
                                (e, ("D6", "D8"), ("D5", "D7"))):
        for address in starts:
            require(book.date(sheet, address) == start, "WRONG_PERIOD", "期初表底不是本月初")
        for address in ends:
            require(book.date(sheet, address) == end, "WRONG_PERIOD", "期末表底不是次月初")
    for sheet, labels in ((g, {"B5": "反向发电侧", "B7": "正向用电侧", "B9": "反向发电侧", "B11": "正向用电侧", "J3": "倍率"}),
                          (e, {"B5": "反向上网侧", "B7": "反向上网侧", "K3": "倍率"}),
                          (n, {"F9": "正向有功", "F10": "反向有功", "F12": "反向有功", "A14": "甲方实际使用乙方光伏电量合计"})):
        for address, label in labels.items():
            require(normalized(book.text(sheet, address)) == normalized(label), "METER_SCOPE_MISMATCH", "计量方向/业务标签不符")
    meter_inputs = []
    for sheet, key, id_cells, ratio_cells in ((g, "generation_meters", ("A5", "A9"), ("J5", "J9")),
                                             (e, "export_meters", ("A5", "A7"), ("K5", "K7"))):
        actual_ids = []
        for expected, id_cell, ratio_cell in zip(policy[key], id_cells, ratio_cells):
            label = normalized(book.text(sheet, id_cell))
            match = re.search(r"NO[:：]?(\d{8,30})", label, re.IGNORECASE)
            require(match is not None and match[1] == expected["meter_id"], "METER_SCOPE_MISMATCH", "实际电表与本站政策不一致")
            ratio = book.multiplier(sheet, ratio_cell)
            require(ratio == decimal(expected["multiplier"]), "MULTIPLIER_MISMATCH", "实际倍率与本站政策不一致")
            actual_ids.append(match[1])
            meter_inputs.append({"sheet": sheet, "meter_id": match[1], "id_cell": id_cell,
                                 "multiplier_cell": ratio_cell, "multiplier": str(ratio)})
        all_ids = {m for c in book.sheets[sheet].values() if c["text"]
                   for m in re.findall(r"NO[:：]?(\d{8,30})", normalized(c["text"]), re.IGNORECASE)}
        require(all_ids == set(actual_ids), "METER_SCOPE_MISMATCH", "附件出现政策未覆盖的计量电表")

    def delta(sheet, end_cell, start_cell, ratio_cell):
        value = (book.number(sheet, end_cell) - book.number(sheet, start_cell)) * book.multiplier(sheet, ratio_cell)
        require(value >= 0, "NEGATIVE_DELTA", "表底差为负，需另行核验换表或回卷")
        return value

    g1, g2 = delta(g, "E5", "E6", "J5"), delta(g, "E9", "E10", "J9")
    u1, u2 = delta(g, "E7", "E8", "J5"), delta(g, "E11", "E12", "J9")
    e1, e2 = delta(e, "E5", "E6", "K5"), delta(e, "E7", "E8", "K7")
    formula_l7 = normalized(book.cell(e, "L7")["formula"])
    if formula_l7 == normalized("=(E7-E8)*K5"):
        require(book.multiplier(e, "K5") == book.multiplier(e, "K7"), "MULTIPLIER_MISMATCH", "第二关口公式借用第一倍率，两个倍率不一致")
    generation, exported, station_use = g1 + g2, e1 + e2, u1 + u2
    consumption = generation - exported
    require(consumption >= 0, "NEGATIVE_DELTA", "上网电量超过同边界发电量")
    for sheet, equations in ((g, {"K5": ("=(E5-E6)*J5", g1), "K7": ("=(E7-E8)*J5", u1),
                                    "K9": ("=(E9-E10)*J9", g2), "K11": ("=(E11-E12)*J9", u2), "F14": ("=K9+K5", generation)}),
                             (e, {"L5": ("=(E5-E6)*K5", e1), "L7": (["=(E7-E8)*K5", "=(E7-E8)*K7"], e2), "F9": ("=L5+L7", exported)}),
                             (n, {"H9": (f"='{g}'!K7+'{g}'!K11", station_use), "H10": (f"='{g}'!F14", generation),
                                    "H12": (f"='{e}'!F9", exported), "H14": ("=H10-H12", consumption)})):
        for address, (formula, value) in equations.items():
            book.formula(sheet, address, formula, value)
    values = dict(zip(METRICS, (generation, consumption, exported, station_use)))
    auxiliary = _auxiliary_rows(book, policy, container_period, station_code)
    return {"method_id": METHOD, "station_code": str(station_code), "container_period": container_period,
            "business_period": period, "measurement_interval": [start.isoformat(), end.isoformat()],
            "file": str(book.path), "sha256": book.sha256, "bytes": book.path.stat().st_size,
            "source_metadata": source_metadata or {}, "metrics": {key: {"value": str(value), "unit": "kWh",
                "business_period": period, "quality": "verified_meter_calculation"} for key, value in values.items()},
            "auxiliary_monthly_rows": auxiliary, "meter_inputs": meter_inputs,
            "inputs": list(book.proofs.values()), "cache_checks": book.cache_checks,
            "excluded_sheets": [EXAMPLE_SHEET] if EXAMPLE_SHEET in book.sheets else [],
            "historical_monthly_report_used": False}
