#!/usr/bin/env python3
"""电费结算工单只读适配。使用已安装 Power+ CLI 的自带 Python 运行。

不改 CLI、不操作网页、不办理工单。一次查询保留一个 JSON 结果及限字段原始响应。
本模块输出来源值及统一数据项，保留来源、期间、单位和数据质量。
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import sys
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

CLI_ROOT = Path.home() / "Library/Application Support/xhyw-power-cli"
BASE = "https://power-xhyw.cnecloud.com"
LIST_PATH = "/api/blade-form/form/data/list"
DETAIL_PATH = "/api/blade-workflow/process/detail"
FIELD_UNITS = {
    "totalpower_name": ("总发电量-电量（kWh）", "kWh"),
    "totalowner_name": ("企业自用电量-电量（kWh）", "kWh"),
    "totalownercost_name": ("企业自用电量-电费（元）", "元"),
    "totalonline_name": ("上网电量-电量（kWh）", "kWh"),
    "totalonlinecost_name": ("上网电量--电费（元）", "元"),
}
ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / '数据字典/验证记录/电费结算接口-20260908/接口契约.json'
from catalog_schema import validate_source_catalog
from remote_catalog import load_catalog


def source_catalog():
    data = validate_source_catalog(load_catalog(ROOT))
    contract = data["query_methods"]["power.electricitybill.settlement.v1"]
    require(contract["base_url"] == BASE, "字典来源域名与已核验的只读适配不一致")
    require([(step["method"],step["path"]) for step in contract["steps"]] ==
            [("POST",LIST_PATH),("GET",DETAIL_PATH),("POST",LIST_PATH)],
            "字典接口步骤已变化，须先验证适配器，不静默使用旧请求")
    verified = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    # Parameter provenance and positive-value prose are descriptions, not request
    # or validation instructions. They may be clarified without changing the
    # previously verified API contract; every other contract detail stays strict.
    def request_parameters(value):
        return {key: {k: v for k, v in parameter.items() if k != "source"}
                for key, parameter in value.items()}
    require(contract["steps"] == verified["steps"] and
            request_parameters(contract["parameters"]) == request_parameters(verified["parameters"]) and
            {k: v for k, v in contract["quality_rules"].items() if k != "positive"} ==
            {k: v for k, v in verified["quality_rules"].items() if k != "positive"},
            "字典请求或校验规则与已验证契约不同，须先扩展并核验适配器")
    return data
VARIABLE_KEYS = (
    "id", "r_id", "formKey", "station", "cne_station", "tbl_number", "status",
    "settlement_month", "settlement_month_start", "settlement_month_end", "created_at",
    "completed_at", "tbl_description", *FIELD_UNITS,
)


class ContractError(ValueError):
    """来源身份、期间或返回结构错误，不能作为缺项降级后填值。"""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def variables(row):
    value = row.get("variables")
    if isinstance(value, str):
        value = json.loads(value)
    require(isinstance(value, dict), "主工单 variables 必须为对象或合法 JSON 对象字符串")
    return value


def subset(value, keys):
    return {key: value[key] for key in keys if key in value}


def clean_list_row(row, form_key):
    if form_key == "Electricitybill":
        out = subset(row, ("id", "processInstanceId", "taskId", "processIsFinished", "status"))
        out["variables"] = json.dumps(subset(variables(row), VARIABLE_KEYS), ensure_ascii=False)
        return out
    return subset(row, ("id", "r_id", "Electricitybill_CORRELATION_ID", "Electricitybill_CORRELATION_STATUS", "meter_reading_photos", *FIELD_UNITS))


def bind_parameters(value, parameters):
    """Bind the dictionary's request template while preserving parameter types."""
    if isinstance(value, dict):return {key:bind_parameters(child,parameters) for key,child in value.items()}
    if isinstance(value, list):return [bind_parameters(child,parameters) for child in value]
    if not isinstance(value,str):return value
    exact=re.fullmatch(r'\{\{([^{}]+)\}\}',value)
    if exact:
        require(exact[1] in parameters,'字典请求参数未提供：'+exact[1])
        return parameters[exact[1]]
    def replace(match):
        require(match[1] in parameters,'字典请求参数未提供：'+match[1])
        # Embedded values are JSON literals, not executable code or shell text.
        return json.dumps(parameters[match[1]],ensure_ascii=False,separators=(',',':'))
    return re.sub(r'\{\{([^{}]+)\}\}',replace,value)


def all_pages(client, form_key, search, evidence, page_size=50, step=None):
    rows, expected, seen, page = [], None, set(), 1
    while True:
        body = {
            "formKey": form_key, "search": search,
            "query": {"current": page, "size": page_size},
            "sort": {} if form_key == "Electricitybill" else {"createTime": "ASC"},
            "customCriteria": {"excludeDraft": 1 if form_key == "Electricitybill" else "1"},
        }
        if step:
            body=bind_parameters({**deepcopy(step['body']),'search':search},{'page':page,'page_size':page_size})
            require(body['formKey']==form_key,'字典表单类型与适配不一致')
            body['search']=search
        resp = client.request(step['method'] if step else "POST", step['path'] if step else LIST_PATH, body=body)
        data = resp.get("data")
        require(isinstance(data, dict) and isinstance(data.get("datas"), list), "列表响应缺 data.datas")
        total = data.get("totalCount")
        require(not isinstance(total, bool) and str(total).isdigit(), "totalCount 不是非负整数")
        total = int(total)
        require(expected is None or total == expected, "分页期间总数变化，请重新采集")
        expected = total
        batch = data["datas"]
        evidence.append({
            "request": {"method": "POST", "path": LIST_PATH, "body": body},
            "response_excerpt": {"code": resp.get("code"), "data": {
                **subset(data, ("totalCount", "totalPage", "pageNo", "pageSize", "hasNextPage")),
                "datas": [clean_list_row(row, form_key) for row in batch],
            }},
        })
        for row in batch:
            require(isinstance(row, dict) and row.get("id"), "列表行缺 id")
            require(row["id"] not in seen, "分页重复 id，不能声称完整")
            seen.add(row["id"])
        rows.extend(batch)
        require(len(rows) <= expected, "行数超过 totalCount")
        if len(rows) == expected:
            require(data.get("hasNextPage") is not True, "总数与 hasNextPage 不一致")
            return rows
        require(bool(batch), "尚未达到总数却收到空页")
        page += 1
        require(page <= 1000, "分页超过 1000 页，请缩小范围")


def collect(client, station_code, month, page_size=50, photos_directory=None, *,
            meter_policy_file=None, attachments_directory=None):
    catalog = source_catalog()
    steps=catalog['query_methods']['power.electricitybill.settlement.v1']['steps']
    require(client.base.rstrip("/") == BASE, "客户端不是已核验的生产域")
    require(re.fullmatch(r"[0-9]{7}", station_code) is not None, "电站编码需为已核验的七位 Power+ 编码")
    require(re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", month) is not None, "月份必须为 YYYY-MM")
    require(1 <= page_size <= 100, "page_size 应在 1 到 100")
    require(bool(meter_policy_file) == bool(attachments_directory),
            "计量政策与附件输出目录必须同时提供")
    meter_policy = None
    if meter_policy_file:
        from bill_meter_attachment import load_meter_policy
        meter_policy = load_meter_policy(meter_policy_file, station_code)
        require(not Path(attachments_directory).exists() or Path(attachments_directory).is_dir(),
                "附件输出目录不能指向已有文件")
    evidence = []
    result = {
        "source": "Power+ 电费结算工单填报", "base_url": BASE,
        "retrieved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "station_code": station_code, "settlement_month": month,
        "status": "no_matching_order", "values": {}, "evidence": evidence,
        "evidence_scope": "业务响应限字段摘录；未保存凭据、人员明细、附件地址、流程XML。",
    }
    if meter_policy is not None:
        policy_path = Path(meter_policy_file).resolve()
        result["attachment_policy"] = {"file": str(policy_path),
            "sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest()}
        result["attachment_sources"] = []
        result["attachment_rejections"] = []
        result["attachment_status"] = ("not_enabled_for_period"
            if month not in meter_policy["allowed_container_periods"] else "no_eligible_attachment")
    search=bind_parameters(steps[0]['body']['search'],{'station_code_as_integer':int(station_code)})
    rows = all_pages(client, "Electricitybill", search, evidence, page_size, steps[0])
    for row in rows:
        require(str(variables(row).get("station")) == station_code, "后端电站过滤未生效或返回错站工单")
    matches = [row for row in rows if variables(row).get("settlement_month") == month]
    result["station_order_count"] = len(rows)
    result["matching_order_count"] = len(matches)
    if not matches:
        return result
    if len(matches) != 1:
        result["status"] = "ambiguous_orders"
        result["candidate_orders"] = [clean_list_row(row, "Electricitybill") for row in matches]
        return result
    row = matches[0]
    require(row.get("processInstanceId") and row.get("taskId"), "已选工单缺流程实例或任务ID")
    query = bind_parameters(steps[1]['query'],{'selected_row.processInstanceId':str(row['processInstanceId']),
                                             'selected_row.taskId':str(row['taskId'])})
    resp = client.request(steps[1]['method'], steps[1]['path'], query=query)
    data = resp.get("data", {})
    process = data.get("process", {}) if isinstance(data, dict) else {}
    require(isinstance(process, dict) and isinstance(process.get("variables"), dict), "详情缺 process.variables")
    var = process["variables"]
    require(str(process.get("processInstanceId")) == query["processInsId"], "详情流程实例不符")
    require(process.get("formKey") == "Electricitybill", "详情表单类型不符")
    require(str(var.get("station")) == station_code and var.get("settlement_month") == month, "详情错站或错结算月份")
    require(var.get("id") == row["id"] and var.get("r_id") == variables(row).get("r_id"), "详情主表id或子表关联id不符")
    require(bool(var.get("r_id")), "主工单缺r_id")
    evidence.append({"request": {"method": "GET", "path": DETAIL_PATH, "query": query},
                     "response_excerpt": {"code": resp.get("code"), "data": {"process": {
                         **subset(process, ("processInstanceId", "taskId", "processDefinitionKey", "formKey", "processIsFinished")),
                         "variables": subset(var, VARIABLE_KEYS),
                     }}}})
    result["order"] = {**subset(row, ("id", "processInstanceId", "taskId")), **subset(var, VARIABLE_KEYS)}
    if var.get("status") != "yjd" or process.get("processIsFinished") != "finished":
        result["status"] = "order_not_finished"
        return result
    year, mon = map(int, month.split("-"))
    expected_period = (month + "-01", f"{month}-{calendar.monthrange(year, mon)[1]:02}")
    require((var.get("settlement_month_start"), var.get("settlement_month_end")) == expected_period,
            "结算起止日期与所查询的自然月不一致，此适配器不支持该结算期间")
    search=bind_parameters(steps[2]['body']['search'],{'detail.data.process.variables.r_id':var['r_id']})
    records = all_pages(client, "ElectricitybillSettlement", search, evidence, page_size, steps[2])
    for record in records:
        require(record.get("Electricitybill_CORRELATION_ID") == var["r_id"], "子表返回其他主工单的记录")
    result["settlement_record_count"] = len(records)
    if len(records) != 1:
        result["status"] = "no_settlement_record" if not records else "ambiguous_settlement_records"
        return result
    record = records[0]
    result["settlement_record_id"] = record["id"]
    if meter_policy is not None and month in meter_policy["allowed_container_periods"]:
        _collect_meter_attachments(client, record, result, meter_policy, attachments_directory)
    result['photos']=[];result['rejected_photos']=[]
    assets=record.get('meter_reading_photos') or []
    require(isinstance(assets,list),'结算抄表图片不是数组')
    result['photo_entry_count']=len(assets)
    if photos_directory:
        from query_other_work import download_picture
        destination=Path(photos_directory).resolve();destination.mkdir(parents=True,exist_ok=True)
        seen=set()
        for index,asset in enumerate(assets):
            require(isinstance(asset,dict),'抄表图片条目不是对象')
            name=asset.get('name') or asset.get('label') or ''
            meta={'name':name,'photo_index':index,'order_id':row['id'],'order_number':var.get('tbl_number'),
                  'source_record_id':record['id'],'station_code':station_code,'period':month,
                  'attachment_path':f'data.evidence.{len(evidence)-1}.response_excerpt.data.datas.0.meter_reading_photos.{index}'}
            url=asset.get('url') or asset.get('value')
            if not url:
                result['rejected_photos'].append({**meta,'reason':'empty_url'});continue
            dated=re.search(r'(20\d{2})[_-]?(\d{2})[_-]?(\d{2})',name)
            if dated and f'{dated[1]}-{dated[2]}'!=month:
                result['rejected_photos'].append({**meta,'reason':'filename_month_differs_from_settlement'});continue
            try:path,digest,size=download_picture(client,url,destination)
            except ContractError:raise
            except Exception as exc:
                result['rejected_photos'].append({**meta,'reason':'download_failed','error_type':type(exc).__name__});continue
            if digest in seen:
                result['rejected_photos'].append({**meta,'reason':'duplicate_bytes','sha256':digest});continue
            seen.add(digest)
            result['photos'].append({**meta,'file':str(path),'sha256':digest,'bytes':size,'download_status':'downloaded',
                'period_basis':'settlement_month_with_filename_check','caption':'现场抄表照片（结算月份 '+month+'）'})
    result["status"] = "source_values_retrieved"
    for field, (label, unit) in FIELD_UNITS.items():
        raw = record.get(field)
        if raw is None or raw == "":
            quality = "missing"
        else:
            require(not isinstance(raw, bool), f"{field} 不是数值")
            try:
                number = Decimal(str(raw))
            except InvalidOperation as exc:
                raise ContractError(f"{field} 不是数值") from exc
            require(number.is_finite() and number >= 0, f"{field} 数值非法")
            quality = "zero_requires_confirmation" if number == 0 else "positive_source_value"
        result["values"][field] = {"label": label, "value": raw, "unit": unit, "quality": quality,
                                    "value_path": f"data.datas[unique id={record['id']}].{field}"}
    result["standard_values"] = {}
    for mapping_id, mapping in catalog["source_mappings"].items():
        if mapping.get("query_method_id") != "power.electricitybill.settlement.v1":
            continue
        key = mapping.get("response_field")
        if key not in result["values"]:
            require(catalog["standard_fields"][mapping["standard_id"]]["role"] != "metric",
                    "字典指标的返回字段未被已核验的接口契约支持")
            continue
        observed = result["values"][key]
        standard_id = mapping["standard_id"]
        standard = catalog["standard_fields"][standard_id]
        require(mapping["original_unit"] == observed["unit"], "字典单位与来源字段定义不一致")
        require(standard["unit"] == observed["unit"],
                "统一数据项单位与来源单位不同，须实现并验证换算，不能只修改字典后填值")
        result["standard_values"][standard_id] = {
            **observed, "standard_name": standard["name"],
            "source_mapping_id": mapping_id, "source_record_id": record["id"],
            "station_code": station_code, "business_period": month,
            "source_semantic_status": mapping["semantic_status"],
        }
    result["dictionary_id"] = catalog["dictionary_id"]
    result["dictionary_version"] = catalog["version"]
    available = sum(item["quality"] != "missing" for item in result["standard_values"].values())
    result["standard_value_count"] = available
    result["status"] = ("source_values_missing" if available == 0 else
                        "source_values_partial" if available < len(result["standard_values"]) else
                        "source_values_retrieved")
    return result


def _collect_meter_attachments(client, record, result, policy, directory):
    """Reuse the already retrieved unique child record; never query orders twice."""
    from bill_meter_attachment import download_bill_attachment, parse_bill_meter_attachment
    assets = record.get("at_messages_photos") or []
    require(isinstance(assets, list), "结算文件附件不是数组")
    retained = []
    for asset in assets:
        require(isinstance(asset, dict), "结算文件附件条目不是对象")
        retained.append({k: asset[k] for k in ("name", "label", "type", "value", "url") if k in asset})
    # Keep the source link beside the original scalar values for later verification.
    result["evidence"][-1]["response_excerpt"]["data"]["datas"][0]["at_messages_photos"] = retained
    seen = set()
    for index, asset in enumerate(retained):
        name = asset.get("name") or asset.get("label") or ""
        if not name.lower().endswith(".xlsx"):
            result["attachment_rejections"].append({"index": index, "name": name,
                "reason": "unsupported_attachment_format"})
            continue
        url = asset.get("url") or asset.get("value")
        if not url:
            result["attachment_rejections"].append({"index": index, "name": name, "reason": "empty_url"})
            continue
        metadata = {"name": name, "attachment_index": index, "attachment_field": "at_messages_photos",
            "source_record_id": record["id"], "order_id": result["order"]["id"],
            "attachment_path": f'data.evidence.{len(result["evidence"])-1}.response_excerpt.data.datas.0.at_messages_photos.{index}'}
        try:
            path = download_bill_attachment(client, url, directory)
        except OSError as exc:
            result["attachment_rejections"].append({**metadata, "reason": "download_failed",
                                                   "error_type": type(exc).__name__})
            continue
        # Contract/config/identity/formula errors deliberately propagate as ValueError.
        parsed = parse_bill_meter_attachment(path, station_code=result["station_code"],
            station_aliases=policy["station_aliases"], container_period=result["settlement_month"],
            meter_config=policy, source_metadata=metadata)
        if parsed["sha256"] in seen:
            result["attachment_rejections"].append({**metadata, "reason": "duplicate_bytes", "sha256": parsed["sha256"]})
            continue
        seen.add(parsed["sha256"])
        result["attachment_sources"].append(parsed)
    if result["attachment_sources"]:
        result["attachment_status"] = "retrieved"
    elif any(r["reason"] == "download_failed" for r in result["attachment_rejections"]):
        result["attachment_status"] = "download_failed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-code", required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--profile", default="me")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument('--photos-out',type=Path)
    parser.add_argument('--meter-policy-file', type=Path)
    parser.add_argument('--attachments-out', type=Path)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument("--stdout", action="store_true", help="供生成器接收完整只读结果")
    args = parser.parse_args()
    if bool(args.meter_policy_file) != bool(args.attachments_out):
        parser.error("--meter-policy-file 与 --attachments-out 必须同时提供")
    if args.output and args.output.exists():
        parser.error("输出已存在，请使用新的文件名保留取数证据")
    sys.path.insert(0, str(CLI_ROOT))
    from power_ui.paths import load_dotenv_local, apply_profile_session_env, base_url
    from power_ui.session import client_for
    from power_ui.errors import CliError
    try:
        source_catalog()
        load_dotenv_local()
        apply_profile_session_env(args.profile)
        require(base_url().rstrip("/") == BASE, "会话不是已核验的生产域")
        result = collect(client_for(args.profile), args.station_code, args.month, args.page_size,args.photos_out,
                         meter_policy_file=args.meter_policy_file, attachments_directory=args.attachments_out)
    except CliError as exc:
        print(json.dumps({'ok': False, 'error': {'code': exc.code}}))
        raise SystemExit(1)
    except (ValueError, KeyError, TypeError) as exc:
        # A malformed contract or wrong station/period must never be downgraded.
        print(json.dumps({'ok': False, 'error': {'code': 'CONTRACT_ERROR'}}))
        raise SystemExit(3) from exc
    if args.stdout:
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in ("evidence", "order")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
