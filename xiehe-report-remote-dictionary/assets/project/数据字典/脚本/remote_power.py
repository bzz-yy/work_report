#!/usr/bin/env python3
"""Execute reviewed Power+ readers against this invocation's server contracts.

No historical dictionary or business-value fallback is accepted. The host passes
an already validated public execution specification over stdin to the installed
Power CLI Python; this module validates it again before any business request.
"""
from __future__ import annotations

import calendar
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urljoin, urlsplit

CLI_ROOT = Path.home() / "Library/Application Support/xhyw-power-cli"
CLI_PYTHON = CLI_ROOT / "runtime/python/bin/python3"
BASE = "https://power-xhyw.cnecloud.com"
LIST_PATH = "/api/blade-form/form/data/list"
ARCHIVE_PATH = "/api/v4/base/station/stationDetail/"
TZ = timezone(timedelta(hours=8))
READERS = {
    "station_archive.v1": {"stationCode", "stationName", "stationCapacity"},
    "electricitybill_settlement.v1": {"totalpower_name", "totalowner_name", "totalonline_name"},
    "electricitybill_parent.v1": {"tbl_number", "settlement_month", "settlement_month_start", "settlement_month_end", "r_id"},
    "electricitybill_photos.v1": {"meter_reading_photos"},
}
PARENT_KEYS = ("id", "r_id", "station", "status", "settlement_month", "settlement_month_start", "settlement_month_end", "tbl_number", "formKey")
CHILD_KEYS = ("id", "r_id", "Electricitybill_CORRELATION_ID", "Electricitybill_CORRELATION_STATUS", "totalpower_name", "totalowner_name", "totalonline_name", "meter_reading_photos")


class ContractError(ValueError):
    """Invalid identity, period, relation, or schema must stop report generation."""


class _UnavailableClient:
    """Represent session initialization failure without pretending values are empty."""
    base = BASE

    def __init__(self, error):
        self.error = error

    def request(self, method, path, **kwargs):
        raise self.error


def require(condition, message):
    if not condition:
        raise ContractError(message)


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _json_bytes(value)
    with path.open("xb") as stream:
        stream.write(content)
    return {"evidence_file": str(path.resolve()), "evidence_sha256": _digest(content)}


def _safe_error(exc):
    # CLI errors can contain URLs, tokens and headers: keep a typed code only.
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        code = type(exc).__name__
    result = {"code": code, "error_type": type(exc).__name__}
    if isinstance(exc, ContractError):
        result["message"] = str(exc)
    return result


def _validate_inputs(station_code, period):
    require(isinstance(station_code, str) and re.fullmatch(r"[0-9]{7}", station_code), "Power+ 电站编码必须为七位数字字符串")
    require(isinstance(period, str) and re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", period), "业务月份必须为 YYYY-MM")


def _output_directory(directory):
    directory = Path(directory).resolve()
    require(not directory.is_relative_to(Path(__file__).resolve().parents[4]), "Power+ 业务响应和图片必须保存到 Skill 外")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _validate_spec(spec):
    from remote_catalog import validate_execution_spec
    validate_execution_spec(spec)
    require(isinstance(spec.get("bindings"), dict), "执行规格缺少字段绑定")
    for binding in spec["bindings"].values():
        require(isinstance(binding, dict), "字段绑定必须为对象")
        if not binding.get("executable"):
            continue
        reader, prop = binding.get("reader"), binding.get("prop")
        require(reader in READERS and prop in READERS[reader], "执行规格包含未经核验的 Power+ reader 或返回字段")
        require(binding.get("standard_id") in spec.get("fields", {}), "执行规格引用未发布字段")


def _active(spec, reader):
    return [b for b in spec["bindings"].values() if b.get("executable") and b.get("reader") == reader]


def _variables(row):
    require(isinstance(row, dict), "父单列表行必须为对象")
    value = row.get("variables")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ContractError("父单 variables 不是合法 JSON 对象") from None
    require(isinstance(value, dict), "父单 variables 必须为对象或 JSON 对象字符串")
    return value


def _safe_attachment(asset):
    if not isinstance(asset, dict):
        return {"invalid_type": type(asset).__name__}
    retained = {k: asset[k] for k in ("name", "label", "type", "size") if k in asset}
    for key in ("url", "value"):
        value = asset.get(key)
        if isinstance(value, str) and value:
            # Signed URLs are never written. Preserve an exact hash for provenance.
            parsed = urlsplit(value)
            retained[key] = {"sha256": _digest(value.encode()), "scheme": parsed.scheme, "host": parsed.hostname, "path": parsed.path, "query_redacted": bool(parsed.query)}
        elif key in asset:
            retained[key] = None
    return retained


def _project_row(row, form_key):
    if not isinstance(row, dict):
        return {"invalid_type": type(row).__name__}
    if form_key == "Electricitybill":
        output = {k: row[k] for k in ("id", "processIsFinished", "status") if k in row}
        try:
            value = _variables(row)
        except ContractError:
            output["variables_invalid_type"] = type(row.get("variables")).__name__
            return output
        output["variables"] = {k: value[k] for k in PARENT_KEYS if k in value}
        return output
    output = {k: row[k] for k in CHILD_KEYS if k in row and k != "meter_reading_photos"}
    photos = row.get("meter_reading_photos")
    output["meter_reading_photos"] = ([_safe_attachment(a) for a in photos] if isinstance(photos, list)
                                      else {"invalid_type": type(photos).__name__} if photos is not None else None)
    return output


def all_pages(client, form_key, search, evidence, page_size=50):
    """Read a fixed form endpoint, retaining every page and checking completeness."""
    require(form_key in {"Electricitybill", "ElectricitybillSettlement"}, "不允许执行未经核验的表单接口")
    require(isinstance(page_size, int) and not isinstance(page_size, bool) and 1 <= page_size <= 100, "分页大小须为 1 至 100")
    rows, seen, expected = [], set(), None
    for page in range(1, 1001):
        body = {"formKey": form_key, "search": json.dumps(search, ensure_ascii=False, separators=(",", ":")),
                "query": {"current": page, "size": page_size}, "sort": {}, "customCriteria": {"excludeDraft": 1}}
        entry = {"request": {"method": "POST", "path": LIST_PATH, "body": body}, "captured_at": _now()}
        evidence.append(entry)
        try:
            response = client.request("POST", LIST_PATH, body=body)
        except Exception as exc:
            entry["error"] = _safe_error(exc)
            raise
        data = response.get("data") if isinstance(response, dict) else None
        batch = data.get("datas") if isinstance(data, dict) else None
        entry["response_excerpt"] = {"code": response.get("code") if isinstance(response, dict) else None,
            "data": {**({k: data[k] for k in ("totalCount", "totalPage", "pageNo", "pageSize", "hasNextPage") if k in data} if isinstance(data, dict) else {}),
                     "datas": [_project_row(row, form_key) for row in batch] if isinstance(batch, list) else None}}
        require(isinstance(data, dict) and isinstance(batch, list), "列表响应缺少 data.datas 数组")
        total = data.get("totalCount")
        require(not isinstance(total, bool) and str(total).isdigit(), "列表 totalCount 不是非负整数")
        total = int(total)
        require(expected is None or total == expected, "分页期间总数变化，需重新采集")
        expected = total
        for row in batch:
            require(isinstance(row, dict) and isinstance(row.get("id"), (str, int)) and not isinstance(row.get("id"), bool) and str(row["id"]), "列表行缺少有效 id")
            identifier = str(row["id"])
            require(identifier not in seen, "分页出现重复 id，不能确认响应完整")
            seen.add(identifier)
        rows.extend(batch)
        require(len(rows) <= expected, "分页条数超过 totalCount")
        if len(rows) == expected:
            require(data.get("hasNextPage") is not True, "分页总数与 hasNextPage 不一致")
            return rows
        require(bool(batch), "分页未达到总数即返回空页")
    raise ContractError("分页超过 1000 页，需缩小查询范围")


def _validate_parent(row, station_code, period):
    value = _variables(row)
    require(str(value.get("station")) == station_code, "电费父单返回错站，后端电站筛选未生效")
    require(value.get("settlement_month") == period, "电费父单返回错期，后端结算月份筛选未生效")
    require(isinstance(value.get("r_id"), str) and value["r_id"], "电费父单缺少关联 r_id")
    require(isinstance(value.get("status"), str) and value["status"], "电费父单缺少状态")
    if value.get("formKey") is not None:
        require(value["formKey"] == "Electricitybill", "父单返回其他表单类型")
    if value.get("id") is not None:
        require(str(value["id"]) == str(row["id"]), "父单列表与 variables.id 不一致")
    year, month = map(int, period.split("-"))
    expected = (period + "-01", f"{period}-{calendar.monthrange(year, month)[1]:02d}")
    # The documented business period remains authoritative. If the source includes
    # explicit endpoints, both must match the natural month; never use created_at.
    if value.get("settlement_month_start") is not None or value.get("settlement_month_end") is not None:
        require((value.get("settlement_month_start"), value.get("settlement_month_end")) == expected,
                "电费结算起止日期与业务自然月不一致")
    return value


def _number(raw):
    if raw is None or raw == "":
        return {"raw_value": raw, "unit": "kWh", "quality": "missing"}
    require(not isinstance(raw, bool) and isinstance(raw, (int, float, str)), "结算电量不是合法数值类型")
    try:
        number = Decimal(str(raw))
    except InvalidOperation:
        raise ContractError("结算电量不是可解析数值") from None
    require(number.is_finite() and number >= 0, "结算电量不是有限非负数")
    return {"raw_value": raw, "unit": "kWh", "quality": "zero_requires_confirmation" if number == 0 else "valid"}


def read_month(client, spec, station_code, period, evidence, page_size=50):
    result = {"status": "missing", "values": {}, "order": {}, "captured_at": _now()}
    rows = all_pages(client, "Electricitybill", {"station": int(station_code), "settlement_month": period}, evidence, page_size)
    parents = [_validate_parent(row, station_code, period) for row in rows]
    result["matching_parent_count"] = len(rows)
    if not rows:
        result["reason"] = "no_matching_parent"
        return result, []
    if len(rows) != 1:
        result.update(status="ambiguous", reason="multiple_matching_parents", candidates=[_project_row(r, "Electricitybill") for r in rows])
        return result, []
    row, parent = rows[0], parents[0]
    result["order"] = {"id": row["id"], **{k: parent[k] for k in PARENT_KEYS if k in parent},
                       "processIsFinished": row.get("processIsFinished"),
                       "values": {b["standard_id"]: parent.get(b["prop"]) for b in _active(spec, "electricitybill_parent.v1")}}
    if parent["status"] != "yjd" or (row.get("processIsFinished") is not None and row["processIsFinished"] != "finished"):
        result["reason"] = "parent_not_finished"
        return result, []
    child_rows = all_pages(client, "ElectricitybillSettlement", {"Electricitybill_CORRELATION_ID": parent["r_id"]}, evidence, page_size)
    for child in child_rows:
        require(child.get("Electricitybill_CORRELATION_ID") == parent["r_id"], "结算子表关联了其他父单")
        require(isinstance(child.get("r_id"), str) and child["r_id"], "结算子表缺少 r_id")
        if child.get("Electricitybill_CORRELATION_STATUS") is not None:
            require(child["Electricitybill_CORRELATION_STATUS"] == "yjd", "结算子表关联状态与已结束父单不一致")
    result["matching_child_count"] = len(child_rows)
    if len(child_rows) != 1:
        result.update(status="ambiguous" if child_rows else "missing", reason="multiple_matching_children" if child_rows else "no_matching_child")
        if child_rows:
            result["child_candidates"] = [{k: c.get(k) for k in ("id", "r_id", "Electricitybill_CORRELATION_ID")} for c in child_rows]
        return result, []
    child = child_rows[0]
    result["order"]["child_id"] = child["id"]
    result["order"]["child_r_id"] = child["r_id"]
    for binding in _active(spec, "electricitybill_settlement.v1"):
        result["values"][binding["standard_id"]] = {**_number(child.get(binding["prop"])), "value_path": "data.datas[unique r_id=" + child["r_id"] + "]." + binding["prop"]}
    result["status"] = "ok"
    photos = child.get("meter_reading_photos")
    require(photos is None or isinstance(photos, list), "抄表照片字段不是数组")
    assets = []
    for index, asset in enumerate(photos or []):
        require(isinstance(asset, dict), "抄表照片条目不是对象")
        assets.append({"asset": asset, "attachment_path": f"evidence.{len(evidence)-1}.response_excerpt.data.datas.0.meter_reading_photos.{index}"})
    result["photo_entry_count"] = len(assets)
    return result, assets


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_picture_once(client, url, target):
    parsed = urlsplit(url)
    require(parsed.scheme == "https" and parsed.hostname == "powersaber-xhyw.cnecloud.com"
            and parsed.port in (None, 443) and not parsed.username and not parsed.password
            and parsed.path == "/api/blade-resource/minio/endpoint/getFile"
            and set(parse_qs(parsed.query)) == {"fileName"}, "照片下载地址不符合已核验的只读接口")
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers=client._headers())
    try:
        response = opener.open(request, timeout=40)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = urljoin(url, exc.headers.get("Location", ""))
        exc.close()
        storage = urlsplit(location)
        require(storage.scheme == "https" and storage.hostname == "powers3-xhyw.cnecloud.com"
                and storage.port in (None, 443) and not storage.username and not storage.password,
                "照片返回未经核验的对象存储重定向")
        # Storage URLs have their own scoped signature. Never forward Power auth.
        response = opener.open(urllib.request.Request(location), timeout=40)
    with response:
        length, chunks, size = response.headers.get("Content-Length"), [], 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            require(size <= 32 * 1024 * 1024, "图片超过 32 MiB 支持范围")
            chunks.append(chunk)
    payload = b"".join(chunks)
    if length and int(length) != len(payload):
        raise OSError("图片下载长度不完整")
    jpeg, png = payload[:3] == b"\xff\xd8\xff", payload.startswith(b"\x89PNG\r\n\x1a\n")
    require(jpeg or png, "抄表附件不是 JPEG 或 PNG 图片")
    if (jpeg and b"\xff\xd9" not in payload[-32:]) or (png and b"IEND" not in payload[-20:]):
        raise OSError("图片结束标记不完整")
    digest = _digest(payload)
    path = Path(target) / (digest + (".jpg" if jpeg else ".png"))
    if path.exists():
        require(_digest(path.read_bytes()) == digest, "已存在同名图片的内容摘要不一致")
    else:
        with path.open("xb") as stream:
            stream.write(payload)
    return path.resolve(), digest, len(payload)


def download_picture(client, url, target):
    for attempt in range(3):
        try:
            return _download_picture_once(client, url, target)
        except (OSError, TimeoutError):
            if attempt == 2:
                raise


def verify_download_evidence(photo):
    """Verify the standalone receipt and original image before returning a photo.

    The report layer additionally resolves attachment_path in the source response
    and compares its redacted URL hash. This check does not infer that relation.
    """
    proof_path = Path(photo["download_evidence_file"]).resolve()
    source_path = Path(photo["source"]["evidence_file"]).resolve()
    picture_path = Path(photo["file"]).resolve()
    require(proof_path.is_relative_to(source_path.parent)
            and picture_path.is_relative_to(source_path.parent), "照片或下载凭证不在本次取数证据目录")
    proof_bytes = proof_path.read_bytes()
    require(_digest(proof_bytes) == photo["download_evidence_sha256"], "照片下载凭证摘要不一致")
    try:
        proof = json.loads(proof_bytes)
    except ValueError:
        raise ContractError("照片下载凭证不是合法 JSON") from None
    require(isinstance(proof, dict) and proof.get("status") == "downloaded", "照片下载凭证状态无效")
    for key in ("attachment_path", "source_url_sha256", "name", "file", "sha256", "bytes", "remote_field_id", "station_code", "period"):
        require(proof.get(key) == photo.get(key), "照片下载凭证与照片元数据不一致：" + key)
    require(proof.get("source_evidence_file") == photo["source"]["evidence_file"]
            and proof.get("source_evidence_sha256") == photo["source"]["evidence_sha256"]
            and proof.get("captured_at") == photo["source"]["captured_at"], "照片下载凭证与来源响应不一致")
    require(_digest(source_path.read_bytes()) == proof["source_evidence_sha256"], "照片来源响应的内容摘要不一致")
    payload = picture_path.read_bytes()
    require(len(payload) == proof["bytes"] and _digest(payload) == proof["sha256"], "原始照片与下载凭证的内容摘要或长度不一致")
    return proof


def read_photos(client, spec, station_code, period, assets, target, source):
    bindings = _active(spec, "electricitybill_photos.v1")
    if not bindings:
        return [], []
    require(len(bindings) == 1, "抄表照片执行绑定不唯一")
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    accepted, rejected, seen = [], [], set()
    for index, entry in enumerate(assets):
        asset = entry["asset"]
        name = asset.get("name") or asset.get("label") or ""
        require(isinstance(name, str), "抄表照片名称不是字符串")
        metadata = {"name": name, "station_code": station_code, "period": period,
                    "remote_field_id": bindings[0]["standard_id"], "attachment_path": entry["attachment_path"], "photo_index": index,
                    "source": {"system": "Power+", **source, "period": period, "powerplus_station_id": station_code}}
        url = asset.get("url") or asset.get("value")
        if not url:
            rejected.append({**metadata, "reason": "empty_url"})
            continue
        require(isinstance(url, str), "抄表照片下载地址不是字符串")
        dates = re.findall(r"(20\d{2})[_-]?(\d{2})[_-]?(\d{2})", name)
        try:
            for year, month, day in dates:
                datetime(int(year), int(month), int(day))
        except ValueError:
            rejected.append({**metadata, "reason": "invalid_filename_date"})
            continue
        if dates and any(f"{year}-{month}" != period for year, month, _ in dates):
            rejected.append({**metadata, "reason": "filename_month_differs_from_settlement"})
            continue
        try:
            path, digest, size = download_picture(client, url, target)
        except ContractError:
            raise
        except Exception as exc:
            rejected.append({**metadata, "reason": "download_failed", "error": _safe_error(exc)})
            continue
        if digest in seen:
            rejected.append({**metadata, "reason": "duplicate_bytes", "sha256": digest})
            continue
        seen.add(digest)
        item = {**metadata, "file": str(path), "sha256": digest, "bytes": size,
                "source_url_sha256": _digest(url.encode()),
                "caption": "现场抄表照片（结算月份 " + period + "）",
                "period_basis": "settlement_month_with_filename_check" if dates else "settlement_month_requires_watermark_review",
                "watermark_review_required": not bool(dates), "download_status": "downloaded"}
        receipt = {key: item[key] for key in ("attachment_path", "source_url_sha256", "name", "file", "sha256", "bytes", "remote_field_id", "station_code", "period")}
        receipt.update(source_evidence_file=source["evidence_file"],
                       source_evidence_sha256=source["evidence_sha256"],
                       captured_at=source["captured_at"], downloaded_at=_now(), status="downloaded")
        receipt_file = _save(target / f"{index:03d}-{digest[:24]}.download.json", receipt)
        item.update(download_evidence_file=receipt_file["evidence_file"],
                    download_evidence_sha256=receipt_file["evidence_sha256"])
        verify_download_evidence(item)
        # Missing filename dates are explicitly exposed for the required visual
        # review; the settlement relation is not claimed to verify the watermark.
        accepted.append(item)
    return accepted, rejected


def collect_with_client(client, spec, station_code, period, evidence_directory, *, page_size=50):
    """Testable core. Caller may inject a read-only client; contracts remain required."""
    _validate_spec(spec)
    _validate_inputs(station_code, period)
    require(getattr(client, "base", "").rstrip("/") == BASE, "Power+ 会话不属于已核验的生产域")
    directory = _output_directory(evidence_directory)
    result = {"station_code": station_code, "period": period, "publication": spec["metadata"],
              "archive": {}, "months": {}, "photos": [], "rejected_photos": [], "historical_fallback_used": False}
    evidence, archive = [], {"status": "missing", "values": {}, "captured_at": _now(), "scope": "current_archive_not_historical_period"}
    try:
        bindings = _active(spec, "station_archive.v1")
        if bindings:
            request = {"method": "GET", "path": ARCHIVE_PATH + station_code}
            entry = {"request": request, "captured_at": archive["captured_at"]}
            evidence.append(entry)
            response = client.request(request["method"], request["path"])
            record = response.get("data") if isinstance(response, dict) else None
            props = {b["prop"] for b in bindings} | {"stationCode"}
            entry["response_excerpt"] = {"data": {k: record.get(k) for k in props} if isinstance(record, dict) else None}
            require(isinstance(record, dict), "档案响应缺少 data 对象")
            require(str(record.get("stationCode")) == station_code, "电站档案返回错站编码")
            archive.update(status="ok", values={b["standard_id"]: record.get(b["prop"]) for b in bindings})
        else:
            archive["reason"] = "no_executable_archive_bindings"
    except ContractError as exc:
        archive.update(status="error", error=_safe_error(exc))
        raise
    except Exception as exc:
        archive.update(status="error", error=_safe_error(exc))
    finally:
        archive.update(_save(directory / "archive.json", {"station_code": station_code, "captured_at": archive["captured_at"], "result": archive, "evidence": evidence}))
        result["archive"] = archive
    year, last_month = map(int, period.split("-"))
    for month in range(1, last_month + 1):
        current_period = f"{year}-{month:02d}"
        evidence, assets = [], []
        observed = {"status": "error", "values": {}, "order": {}, "captured_at": _now()}
        try:
            if _active(spec, "electricitybill_settlement.v1") or _active(spec, "electricitybill_photos.v1") or _active(spec, "electricitybill_parent.v1"):
                observed, assets = read_month(client, spec, station_code, current_period, evidence, page_size)
            else:
                observed.update(status="missing", reason="no_executable_settlement_bindings")
        except ContractError as exc:
            observed.update(status="error", error=_safe_error(exc))
            raise
        except Exception as exc:
            observed.update(status="error", error=_safe_error(exc))
        finally:
            observed.update(_save(directory / (current_period + ".json"), {"station_code": station_code, "period": current_period,
                "captured_at": observed["captured_at"], "result": observed, "evidence": evidence}))
            result["months"][current_period] = observed
        if current_period == period and observed["status"] == "ok":
            source = {k: observed[k] for k in ("evidence_file", "evidence_sha256", "captured_at")}
            result["photos"], result["rejected_photos"] = read_photos(client, spec, station_code, period, assets, directory / "photos", source)
    _save(directory / "collection.json", result)
    return result


def collect(catalog, station_code: str, period: str, evidence_directory: Path, profile="me"):
    """Host entry: launch only the installed Power CLI runtime with public stdin."""
    spec = catalog["execution_spec"]
    _validate_spec(spec)
    _validate_inputs(station_code, period)
    require(isinstance(profile, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile), "Power+ profile 名称无效")
    require(CLI_PYTHON.is_file(), "未找到 Power+ CLI 自带 Python")
    directory = _output_directory(evidence_directory)
    request = {"execution_spec": spec, "station_code": station_code, "period": period, "evidence_directory": str(directory), "profile": profile}
    command = [str(CLI_PYTHON), "-B", str(Path(__file__).resolve()), "--worker"]
    try:
        run = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), capture_output=True, text=True, timeout=1500)
    except subprocess.TimeoutExpired:
        _save(directory / "worker-process.json", {"command": command, "exit_code": None, "error": "WORKER_TIMEOUT", "captured_at": _now()})
        raise RuntimeError("Power+ 采集超时；已写入的当次证据保留") from None
    except OSError:
        raise RuntimeError("Power+ CLI Python 启动失败") from None
    _save(directory / "worker-process.json", {"command": command, "exit_code": run.returncode, "captured_at": _now(), "stderr_saved": False})
    try:
        payload = json.loads(run.stdout)
    except ValueError:
        raise RuntimeError("Power+ 采集器没有返回有效 JSON；未保存可能含会话信息的输出") from None
    if run.returncode or not payload.get("ok"):
        error = payload.get("error") or {}
        if run.returncode == 3:
            raise ContractError(error.get("message", "Power+ 返回身份、期间、关联或契约校验失败"))
        raise RuntimeError("Power+ 采集器执行失败：" + str(error.get("code", "UNKNOWN_ERROR")))
    return payload["data"]


def _worker():
    request = json.load(sys.stdin)
    directory = Path(request["evidence_directory"]).resolve()
    try:
        _validate_spec(request["execution_spec"])
        _validate_inputs(request["station_code"], request["period"])
        require(isinstance(request.get("profile", "me"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request.get("profile", "me")), "Power+ profile 名称无效")
        sys.path.insert(0, str(CLI_ROOT))
        from power_ui.paths import load_dotenv_local, apply_profile_session_env, base_url
        from power_ui.session import client_for
        # Library chatter may carry auth context. The worker emits one JSON result.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            load_dotenv_local()
            apply_profile_session_env(request.get("profile", "me"))
            require(base_url().rstrip("/") == BASE, "Power+ profile 配置的服务域未经核验")
            try:
                client = client_for(request.get("profile", "me"))
            except (ValueError, TypeError, KeyError):
                raise
            except Exception as exc:
                client = _UnavailableClient(exc)
            result = collect_with_client(client, request["execution_spec"], request["station_code"], request["period"], directory)
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    except Exception as exc:
        error = _safe_error(exc)
        code = 3 if isinstance(exc, ValueError) else 1
        _save(directory / "worker-error.json", {"error": error, "exit_code": code, "captured_at": _now()})
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        raise SystemExit(code) from None


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("Use report.py generate; remote_power has no standalone snapshot input.")
    _worker()
