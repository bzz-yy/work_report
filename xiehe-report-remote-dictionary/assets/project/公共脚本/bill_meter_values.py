"""Bind bill-attachment metrics, then independently recompute before filling."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "数据字典/脚本"))
from bill_meter_attachment import METHOD, METRICS, load_meter_policy, parse_bill_meter_attachment
from source_values import at_path
from value_rules import transform


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _code(plan):
    return str(plan["bound_parameters"]["powerplus_station_id"])


def _selection(definition, catalog):
    selection = definition.get("attachment_selection")
    _require(definition.get("source_mode") in {"catalog", "attachment"} and isinstance(selection, dict),
             "附件字段未按有效计划启用")
    metric = selection.get("metric")
    _require(metric in METRICS, "附件选源指标不受支持")
    mapping = catalog["source_mappings"][selection["mapping_id"]]
    binding = mapping.get("value_binding") or {}
    _require(mapping.get("query_method_id") == METHOD and binding.get("scope") == "bill_attachment"
             and binding.get("metric") == metric and binding.get("field") == metric
             and mapping.get("response_field") == metric and mapping.get("original_unit") == "kWh",
             "附件选源与数据字典指标绑定不一致")
    standard = catalog["standard_fields"][mapping["standard_id"]]
    _require(standard["unit"] == "kWh" and selection["display"]["source_unit"] == "kWh"
             and selection["display"]["to_unit"] == definition["unit"], "附件来源或显示单位不一致")
    return selection, mapping, standard


def _recompute(response, plan):
    if not response.get("ok"):
        return []
    data = response.get("data") or {}
    sources = data.get("attachment_sources") or []
    if not sources:
        return []
    _require(str(data.get("station_code")) == _code(plan), "附件结算响应串站")
    policy_file = Path(plan.get("meter_policy_file", "")).resolve()
    _require(policy_file.is_file(), "附件字段缺已审核计量政策文件")
    saved_policy = data.get("attachment_policy") or {}
    digest = hashlib.sha256(policy_file.read_bytes()).hexdigest()
    _require(Path(saved_policy.get("file", "")).resolve() == policy_file and saved_policy.get("sha256") == digest,
             "当前计量政策与取数时政策不一致")
    policy = load_meter_policy(policy_file, _code(plan))
    container = data.get("settlement_month")
    _require(container in policy["allowed_container_periods"], "附件容器期未获本站政策采用")
    _require(data.get("matching_order_count") == 1 and data.get("settlement_record_count") == 1,
             "附件缺唯一主工单和唯一关联子表")
    order = data.get("order") or {}
    _require(str(order.get("station")) == _code(plan) and order.get("settlement_month") == container,
             "附件主工单站月不一致")
    entries = []
    for index, source in enumerate(sources):
        metadata = source.get("source_metadata") or {}
        _require(metadata.get("order_id") == order.get("id")
                 and metadata.get("source_record_id") == data.get("settlement_record_id")
                 and metadata.get("attachment_field") == "at_messages_photos", "附件工单关联元数据不一致")
        path = metadata.get("attachment_path")
        _require(isinstance(path, str) and path.startswith("data.evidence.")
                 and path.endswith(".at_messages_photos." + str(metadata.get("attachment_index"))),
                 "附件原始引用路径错误")
        asset = at_path(response, path)
        child_path = path.split(".at_messages_photos.")[0]
        child = at_path(response, child_path)
        _require(child.get("id") == metadata["source_record_id"]
                 and child.get("Electricitybill_CORRELATION_ID") == order.get("r_id"), "附件子表r_id关联不一致")
        _require(metadata.get("name") == (asset.get("name") or asset.get("label")), "附件文件名与原始引用不一致")
        local = Path(source.get("file", ""))
        _require(local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == source.get("sha256"),
                 "附件文件缺失或字节内容变化")
        parsed = parse_bill_meter_attachment(local, station_code=_code(plan), station_aliases=policy["station_aliases"],
            container_period=container, meter_config=policy, source_metadata=metadata)
        _require(parsed == source, "附件解析记录与原始XLSX重新计算不一致")
        # A positive independent scalar is evidence to reconcile, not overwrite.
        for metric, scalar in (("generation_kwh", "totalpower_name"), ("consumption_kwh", "totalowner_name"),
                               ("grid_kwh", "totalonline_name")):
            observation = (data.get("values") or {}).get(scalar) or {}
            if observation.get("quality") == "positive_source_value":
                _require(Decimal(str(observation["value"])) == Decimal(parsed["metrics"][metric]["value"]),
                         "结算可信正值与独立计量附件冲突：" + metric)
        entries.append((index, parsed, digest))
    return entries


def _candidates(response, plan, metric, period):
    candidates = []
    for index, source, policy_sha in _recompute(response, plan):
        common = {"attachment_source_index": index, "attachment_sha256": source["sha256"],
                  "attachment_file": source["file"], "container_period": source["container_period"],
                  "meter_policy_sha256": policy_sha, "source_record_id": source["source_metadata"]["source_record_id"]}
        if source["business_period"] == period:
            value = source["metrics"][metric]
            candidates.append({**common, "raw_value": value["value"], "source_quality": value["quality"],
                "origin": "meter_calculation", "auxiliary_row_index": None,
                "value_path": f"data.attachment_sources.{index}.metrics.{metric}.value"})
        if metric == "station_use_kwh":
            for row_index, row in enumerate(source["auxiliary_monthly_rows"]):
                if row["business_period"] == period:
                    candidates.append({**common, "raw_value": row["value"], "source_quality": row["quality"],
                        "origin": "auxiliary_monthly_ledger", "auxiliary_row_index": row_index,
                        "value_path": f"data.attachment_sources.{index}.auxiliary_monthly_rows.{row_index}.value"})
    return candidates


def _make_item(definition, selection, mapping, standard, provenance, candidate, period, plan):
    source = {**deepcopy(provenance), **deepcopy(candidate), "period": period, "business_period": period,
              "system": "Power+", "source_method": METHOD, "metric": selection["metric"],
              "mapping_id": selection["mapping_id"], "standard_id": mapping["standard_id"], "source_unit": "kWh",
              "transformation": deepcopy(selection["display"])}
    return {"field_id": definition["field_id"], "label": definition["label"], "unit": definition["unit"],
            "station_id": plan["station_id"], "powerplus_station_id": int(_code(plan)), "period": period,
            "status": "real", "value": transform(candidate["raw_value"], mapping, standard, selection["display"]),
            "source": source, "reason": "来自本站Power+结算附件；业务月与附件容器月分别核验，原始XLSX已重算。"}


def bind_meter_values(plan, collected, responses, catalog):
    """Mutate/return collected; responses are {container_period: (response, source)}."""
    definitions = [f for f in plan["fields"] if f.get("attachment_selection")
                   and f.get("source_mode") in {"catalog", "attachment"}]
    for definition in definitions:
        selection, mapping, standard = _selection(definition, catalog)
        group = (definition.get("effective_filling_rule") or {}).get("group")
        periods = ([plan["period"][:4] + f"-{month:02d}" for month in range(1, int(plan["period"][-2:]) + 1)]
                   if group == "records.1" else [plan["period"]])
        for period in periods:
            candidates = []
            for container, (response, source) in responses.items():
                data = response.get("data") or {}
                if data.get("attachment_sources"):
                    _require(container == data.get("settlement_month") == source.get("period"), "附件响应与查询容器期不一致")
                candidates.extend((candidate, source) for candidate in _candidates(response, plan, selection["metric"], period))
            if not candidates:
                continue
            values = {Decimal(candidate["raw_value"]) for candidate, _ in candidates}
            _require(len(values) == 1, "同站同月附件有不同版本的业务值，不自动选首个或相加")
            # Equivalent copies corroborate one business fact; keep their evidence.
            candidate, provenance = candidates[0]
            item = _make_item(definition, selection, mapping, standard, provenance, candidate, period, plan)
            item["source"]["corroborating_attachments"] = [
                {"sha256": c["attachment_sha256"], "container_period": c["container_period"],
                 "evidence_file": p.get("evidence_file")} for c, p in candidates]
            if group == "records.1":
                rows = collected["repeat_groups"][group]["records"]
                row = next((r for r in rows if r.get("F052", {}).get("period") == period), None)
                if row is None:
                    row = {"F052": {"field_id": "F052", "value": str(int(period[-2:])), "status": "request",
                        "unit": "月", "station_id": plan["station_id"], "period": period,
                        "source": {"system": "本次请求", "request": plan["request"]}}}
                    rows.append(row)
                old = row.get(definition["field_id"]) or {}
                if old.get("status") == "real":
                    _require(old.get("value") == item["value"], "附件与已选真实台账值冲突")
                else:
                    row[definition["field_id"]] = item
                rows.sort(key=lambda r: r["F052"]["period"])
            if group != "records.1" or period == plan["period"]:
                old = collected["fields"].get(definition["field_id"]) or {}
                if old.get("status") == "real":
                    _require(old.get("value") == item["value"], "附件与已选真实字段值冲突")
                else:
                    collected["fields"][definition["field_id"]] = item
    return collected


def validate_meter_field(item, definition, raw, catalog, plan):
    """Reject edited values/provenance even when the displayed rounded value matches."""
    selection, mapping, standard = _selection(definition, catalog)
    source = item.get("source") or {}
    _require(item.get("station_id") == plan["station_id"]
             and str(item.get("powerplus_station_id")) == _code(plan), "附件报告字段串站")
    _require(source.get("source_method") == METHOD and source.get("metric") == selection["metric"]
             and source.get("mapping_id") == selection["mapping_id"]
             and source.get("standard_id") == mapping["standard_id"]
             and source.get("source_unit") == "kWh"
             and source.get("transformation") == selection["display"], "附件字段来源或转换配置不一致")
    period = item.get("period")
    _require(source.get("period") == source.get("business_period") == period, "附件字段业务期不一致")
    candidates = _candidates(raw, plan, selection["metric"], period)
    match = [c for c in candidates if c["attachment_source_index"] == source.get("attachment_source_index")
             and c["auxiliary_row_index"] == source.get("auxiliary_row_index")]
    _require(len(match) == 1, "附件字段原始业务行不能唯一定位")
    candidate = match[0]
    for key, value in candidate.items():
        _require(source.get(key) == value, "附件字段原始值或证据被修改：" + key)
    expected = transform(candidate["raw_value"], mapping, standard, selection["display"])
    _require(item.get("status") == "real" and item.get("value") == expected and item.get("unit") == definition["unit"],
             "附件字段显示值、单位或状态与原始重算不一致")
    return True
