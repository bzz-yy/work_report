"""Finite, reviewed monthly-report calculations over the current collection only.

The caller must validate every real/config/request input against its original
source before ``validate_derivations``. This module never reads research data or
historical reports and never treats a stored derived value as an input authority.

Rules: rule_id, operation, target, inputs, factor, output_unit, display; optional
group_key (only records.1), fractional_precision (ticket_rate only). Input specs
contain field_id, scope (scalar/ledger_month), unit, optional basis (value or
source_raw). Display contains decimal_places, rounding=half_up and a boolean
trim_trailing_zeros. Adopted report periods are explicitly listed per field and
rule slot in profile/plan.calculation_adoptions. All validation_ref paths are
relative to the project root, where report.py lives.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
from pathlib import Path
import re


SLOTS = ("calculation_rule", "ledger_total_rule")
OPS = {"current_month_ledger", "sum_year_months", "ratio", "ticket_rate"}
SYSTEM = "模板计算"
MONTH = re.compile(r"20\d{2}-(?:0[1-9]|1[0-2])\Z")


def canonical_rule_sha256(rule):
    return hashlib.sha256(json.dumps(rule, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _number(value, label):
    if value is None or isinstance(value, bool):
        raise ValueError(label + "：缺失/布尔值不是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(label + "：不是合法数值") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(label + "：负数、NaN或无穷值不可计算")
    return result


def _fields(value):
    fields = value.get("fields", {})
    if isinstance(fields, list):
        result = {f["field_id"]: f for f in fields}
        if len(result) != len(fields):
            raise ValueError("计算字段计划存在重复编号")
        return result
    if not isinstance(fields, dict):
        raise ValueError("计算字段定义必须为对象或列表")
    return fields


def _unit(field):
    return field.get("unit", field.get("filling_rule", {}).get("unit"))


def _validate_rules(definitions):
    nodes, ids = {}, {}
    for fid, field in definitions.items():
        for slot in SLOTS:
            rule = field.get(slot)
            if rule is None:
                continue
            label = fid + "." + slot
            required = {"rule_id", "operation", "target", "inputs", "factor", "output_unit", "display"}
            optional = {"group_key", "fractional_precision"}
            if not isinstance(rule, dict) or not required <= set(rule) or set(rule) - required - optional:
                raise ValueError(label + "：规则缺项或含未实现参数")
            rid = rule["rule_id"]
            if not isinstance(rid, str) or not rid.strip():
                raise ValueError(label + "：rule_id缺失")
            digest = canonical_rule_sha256(rule)
            if rid in ids and ids[rid] != digest:
                raise ValueError(label + "：相同rule_id对应不同规则")
            ids[rid] = digest
            op, target = rule["operation"], rule["target"]
            if op not in OPS or target not in {"scalar", "ledger_month", "ledger_total"}:
                raise ValueError(label + "：不支持的运算或目标")
            if (slot == "ledger_total_rule") != (target == "ledger_total"):
                raise ValueError(label + "：累计规则目标不一致")
            if rule.get("group_key", "records.1") != "records.1":
                raise ValueError(label + "：尚不支持此台账组")
            if rule["output_unit"] != _unit(field):
                raise ValueError(label + "：输出单位与字段定义不一致")
            factor = _number(rule["factor"], label)
            if factor <= 0:
                raise ValueError(label + "：换算因子必须大于0")
            names = ({"value"} if op in {"current_month_ledger", "sum_year_months"}
                     else {"numerator", "denominator"} if op == "ratio"
                     else {"received", "qualified", "unqualified"})
            if not isinstance(rule["inputs"], dict) or set(rule["inputs"]) != names:
                raise ValueError(label + "：输入名称与运算不一致")
            for spec in rule["inputs"].values():
                if (not isinstance(spec, dict) or not {"field_id", "scope", "unit"} <= set(spec)
                        or set(spec) - {"field_id", "scope", "unit", "basis"}):
                    raise ValueError(label + "：输入定义缺项或含未实现参数")
                if spec["field_id"] not in definitions or spec["unit"] != _unit(definitions[spec["field_id"]]):
                    raise ValueError(label + "：输入字段不存在或单位不符")
                if spec["scope"] not in {"scalar", "ledger_month"} or spec.get("basis", "value") not in {"value", "source_raw"}:
                    raise ValueError(label + "：输入范围/数值依据未实现")
            scopes = {s["scope"] for s in rule["inputs"].values()}
            if op == "current_month_ledger" and (target != "scalar" or scopes != {"ledger_month"}):
                raise ValueError(label + "：当月台账引用只用于标量")
            if op == "sum_year_months" and (target not in {"scalar", "ledger_total"} or scopes != {"ledger_month"}):
                raise ValueError(label + "：年累计须引用逐月值")
            if op in {"ratio", "ticket_rate"} and scopes != ({"ledger_month"} if target == "ledger_month" else {"scalar"}):
                raise ValueError(label + "：比率输入须同一业务月范围")
            if op in {"ratio", "ticket_rate"} and target == "ledger_total":
                raise ValueError(label + "：累计比率须使用独立标量依赖")
            if op == "ticket_rate":
                if target != "scalar" or factor != 100 or rule.get("fractional_precision") not in {"pending", "configured"}:
                    raise ValueError(label + "：票率只支持标量百分比及明确精度策略")
            elif "fractional_precision" in rule:
                raise ValueError(label + "：非票率规则不能使用票率精度策略")
            display = rule["display"]
            if not isinstance(display, dict) or set(display) != {"decimal_places", "rounding", "trim_trailing_zeros"}:
                raise ValueError(label + "：显示规则不完整")
            places = display["decimal_places"]
            if (isinstance(places, bool) or not isinstance(places, int) or not 0 <= places <= 10
                    or display["rounding"] != "half_up" or not isinstance(display["trim_trailing_zeros"], bool)):
                raise ValueError(label + "：非法显示精度/舍入方式")
            nodes[(fid, target)] = rule

    def dependency(spec):
        key = (spec["field_id"], "ledger_month" if spec["scope"] == "ledger_month" else "scalar")
        if key not in nodes and spec["scope"] == "scalar":
            key = (spec["field_id"], "ledger_month")  # Scalar current-month alias.
        return key if key in nodes else None

    visited, active = set(), set()
    def visit(key):
        if key in active:
            raise ValueError(key[0] + "：计算规则存在循环依赖")
        if key in visited:
            return
        active.add(key)
        for spec in nodes[key]["inputs"].values():
            child = dependency(spec)
            if child:
                visit(child)
        active.remove(key)
        visited.add(key)
    for node in nodes:
        visit(node)
    return nodes


def validate_calculation_rules(common, profile, root):
    """Validate finite rules and cryptographically bind every explicit adoption."""
    root = Path(root).resolve()
    definitions = _fields(common)
    nodes = _validate_rules(definitions)
    adoptions = profile.get("calculation_adoptions", {})
    if not isinstance(adoptions, dict):
        raise ValueError("calculation_adoptions必须是字段对象")
    for fid, slots in adoptions.items():
        if fid not in definitions or not isinstance(slots, dict) or set(slots) - set(SLOTS):
            raise ValueError(fid + "：计算采用字段/槽位不存在")
        for slot, adoption in slots.items():
            if not isinstance(adoption, dict) or set(adoption) != {"rule_id", "periods", "validation_ref", "validation_sha256"}:
                raise ValueError(fid + "：计算采用登记字段不完整")
            rule = definitions[fid].get(slot)
            if not rule or rule["rule_id"] != adoption["rule_id"]:
                raise ValueError(fid + "：采用的规则编号不匹配")
            periods = adoption["periods"]
            if (not isinstance(periods, list) or not periods or len(periods) != len(set(periods))
                    or any(not isinstance(x, str) or not MONTH.fullmatch(x) for x in periods)):
                raise ValueError(fid + "：采用期间必须明确且不重复")
            ref = adoption["validation_ref"]
            if not isinstance(ref, str) or Path(ref).is_absolute():
                raise ValueError(fid + "：核验引用必须相对项目根")
            path = (root / ref).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(fid + "：核验引用越界或不存在")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != adoption["validation_sha256"]:
                raise ValueError(fid + "：核验文件摘要不一致")
            registry = json.loads(raw)
            if registry.get("template_id") != common.get("template_id") or fid not in registry.get("reviewed_fields", []):
                raise ValueError(fid + "：核验文件模板或字段不匹配")
            if registry.get("calculation_rules", {}).get(rule["rule_id"]) != canonical_rule_sha256(rule):
                raise ValueError(fid + "：公式内容不符合独立核验摘要")
            station = profile.get("station_id")
            allowed = registry.get("calculation_scopes", {}).get(rule["rule_id"], {}).get(station)
            if (not station or not isinstance(allowed, list) or not allowed
                    or any(not isinstance(x, str) or not MONTH.fullmatch(x) for x in allowed)
                    or not set(periods) <= set(allowed)):
                raise ValueError(fid + "：计算采用电站或期间超出独立核验范围")
    return {"rule_count": len(nodes), "adopted_field_count": len(adoptions)}


def _format(value, display):
    with localcontext() as ctx:
        ctx.prec = 60
        try:
            rounded = value.quantize(Decimal(1).scaleb(-display["decimal_places"]), rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise ValueError("计算值超出支持的显示精度范围") from exc
    text = format(rounded, "f")
    return text.rstrip("0").rstrip(".") if display["trim_trailing_zeros"] and "." in text else text


class _Evaluator:
    def __init__(self, plan, collected):
        self.plan, self.data = plan, collected
        self.definitions = _fields(plan)
        self.rules = _validate_rules(self.definitions)
        validate_calculation_rules(plan, plan, plan.get("project_root", Path(__file__).resolve().parents[1]))
        self.period = plan["period"]
        if not MONTH.fullmatch(self.period):
            raise ValueError("计算计划期间无效")
        self.station = plan["station_id"]
        self.months = [self.period[:4] + f"-{m:02d}" for m in range(1, int(self.period[-2:]) + 1)]
        self.cache, self.active = {}, set()
        self.rows, self.total_row = {}, None
        group = collected.get("repeat_groups", {}).get("records.1", {})
        for row in group.get("records", []):
            label = row.get("F052", {})
            label = label.get("value") if isinstance(label, dict) else label
            if str(label).strip() in {"累计", "合计"}:
                if self.total_row is not None:
                    raise ValueError("F052：台账累计行重复")
                self.total_row = row
                continue
            match = re.fullmatch(r"(?:(20\d{2})[-年])?(\d{1,2})月?", str(label).strip())
            if not match or (match[1] and match[1] != self.period[:4]):
                raise ValueError("F052：台账月份标签无效")
            period = self.period[:4] + f"-{int(match[2]):02d}"
            if period not in self.months or period in self.rows:
                raise ValueError("F052：台账月份重复或超出报告范围")
            self.rows[period] = row

    def _adopted(self, fid, target):
        slot = "ledger_total_rule" if target == "ledger_total" else "calculation_rule"
        adoption = self.plan.get("calculation_adoptions", {}).get(fid, {}).get(slot)
        rule = self.rules[(fid, target)]
        return bool(adoption and adoption.get("rule_id") == rule["rule_id"] and self.period in adoption.get("periods", []))

    def _base(self, fid, scope, period):
        row = self.rows.get(period, {}) if scope == "ledger_month" else self.data.get("fields", {})
        value = row.get(fid)
        if not isinstance(value, dict):
            return None
        if value.get("station_id") != self.station or value.get("period") != period:
            raise ValueError(fid + "：计算输入串站或串期间")
        if value.get("unit") != _unit(self.definitions[fid]):
            raise ValueError(fid + "：计算输入单位不匹配")
        if value.get("status") == "derived" or value.get("source", {}).get("system") == SYSTEM:
            raise ValueError(fid + "：没有受支持规则的派生输入")
        if value.get("status") not in {"real", "config", "request"}:
            return value
        source = value.get("source")
        if not isinstance(source, dict) or not source.get("system"):
            raise ValueError(fid + "：计算输入缺来源")
        for key, expected in (("station_id", self.station), ("period", period)):
            if key in source and source[key] != expected:
                raise ValueError(fid + "：输入来源范围不一致")
        code = self.plan.get("bound_parameters", {}).get("powerplus_station_id")
        for obj in (value, source):
            if code is not None and obj.get("powerplus_station_id") is not None and str(obj["powerplus_station_id"]) != str(code):
                raise ValueError(fid + "：输入来源平台编码串站")
        return value

    def _read(self, spec, period):
        fid, scope = spec["field_id"], spec["scope"]
        key = (fid, "ledger_month" if scope == "ledger_month" else "scalar")
        actual_period = period if scope == "ledger_month" else self.period
        if key not in self.rules and scope == "scalar" and (fid, "ledger_month") in self.rules:
            key = (fid, "ledger_month")
        item = (self.evaluate(fid, key[1], actual_period) if key in self.rules
                else self._base(fid, scope, actual_period))
        if item is None or item.get("status") not in {"real", "config", "request", "derived"} or item.get("value") is None:
            reason = item.get("reason", "未取得输入") if item else "未取得输入"
            return None, {"field_id": fid, "period": actual_period, "reason": reason,
                          "reason_code": "input_missing" if item is None else "input_not_trusted_or_empty"}
        basis = spec.get("basis", "value")
        source = item.get("source", {})
        value = item["value"]
        if basis == "source_raw":
            if item["status"] != "real" or source.get("source_unit") != spec["unit"] or source.get("raw_value") is None:
                return None, {"field_id": fid, "period": actual_period,
                              "reason": "缺已验证且单位一致的来源原值，不能退回显示值",
                              "reason_code": "verified_source_raw_missing"}
            value = source["raw_value"]
        number = _number(value, fid)
        trace = {"field_id": fid, "period": actual_period, "unit": spec["unit"], "basis": basis,
                 "value": str(number), "report_value": deepcopy(item["value"]), "source": deepcopy(source)}
        return (number, trace), None

    def evaluate(self, fid, target, period):
        key = (fid, target, period)
        if key in self.cache:
            return self.cache[key]
        if key in self.active:
            raise ValueError(fid + "：运行时循环依赖")
        self.active.add(key)
        rule = self.rules[(fid, target)]
        context = {"station_id": self.station, "period": period,
                   "powerplus_station_id": self.plan.get("bound_parameters", {}).get("powerplus_station_id")}
        source = {"system": SYSTEM, **context, "rule_id": rule["rule_id"],
                  "rule_sha256": canonical_rule_sha256(rule), "target": target, "inputs": []}
        result = {"field_id": fid, "label": self.definitions[fid].get("label", fid),
                  "unit": rule["output_unit"], **context, "status": "missing", "value": None,
                  "source": source, "reason": ""}
        if not self._adopted(fid, target):
            result["reason"] = fid + "：本站本报告期尚未采用此计算规则"
        else:
            missing, numbers, traces = [], {}, {}
            if rule["operation"] == "sum_year_months":
                values = []
                for month in self.months:
                    pair, error = self._read(rule["inputs"]["value"], month)
                    if error:
                        missing.append(error)
                    else:
                        values.append(pair[0]); source["inputs"].append(pair[1])
                numbers["value"] = sum(values, Decimal(0))
            else:
                for name, spec in sorted(rule["inputs"].items()):
                    pair, error = self._read(spec, period)
                    if error:
                        missing.append(error)
                    else:
                        numbers[name], traces[name] = pair
                        source["inputs"].append(pair[1])
            if missing:
                # Guidance may enrich a missing base field's human-readable
                # reason after collection. Keep that prose in result.reason,
                # while the provenance binds stable field/period/cause keys.
                # Numeric input values and all actual source evidence remain
                # fully included and compared without this normalization.
                source["missing_inputs"] = [{k: x[k] for k in ("field_id", "period", "reason_code")} for x in missing]
                result["reason"] = fid + "：缺计算输入：" + "；".join(x["field_id"] + "@" + x["period"] + "（" + x["reason"] + "）" for x in missing)
            else:
                self._calculate(result, rule, numbers, traces)
        self.active.remove(key)
        self.cache[key] = result
        return result

    def _calculate(self, result, rule, values, traces):
        fid, op = result["field_id"], rule["operation"]
        with localcontext() as ctx:
            ctx.prec = 60
            if op in {"current_month_ledger", "sum_year_months"}:
                value = values["value"] * _number(rule["factor"], fid)
            elif op == "ratio":
                if values["denominator"] == 0:
                    result["reason"] = fid + "：分母为0，不能计算比率"
                    return
                value = values["numerator"] / values["denominator"] * _number(rule["factor"], fid)
            else:
                proof = [t["source"].get("ticket_population") for t in traces.values()]
                if any(not isinstance(x, dict) or not x.get("id") for x in proof):
                    result["reason"] = fid + "：缺同一收票集合及完整评价证据"
                    return
                if len({x["id"] for x in proof}) != 1 or any(x.get("station_id") != self.station or x.get("period") != self.period for x in proof):
                    raise ValueError(fid + "：票率输入不属于同一站期收票集合")
                if any(x.get("complete") is not True or x.get("assessment_complete") is not True for x in proof):
                    result["reason"] = fid + "：收票范围或评价覆盖不完整"
                    return
                if any(x != x.to_integral_value() for x in values.values()):
                    raise ValueError(fid + "：票数必须是非负整数")
                if values["qualified"] + values["unqualified"] != values["received"]:
                    raise ValueError(fid + "：合格与不合格之和不等于收票数")
                if values["received"] == 0:
                    result.update(status="derived", value="/", reason="完整来源确认0收票，合格率不适用。")
                    result["source"]["not_applicable"] = True
                    return
                value = values["qualified"] / values["received"] * 100
                if rule["fractional_precision"] == "pending" and value != value.to_integral_value():
                    result["reason"] = fid + "：非整数合格率显示精度尚未核验"
                    return
            result.update(status="derived", value=_format(value, rule["display"]), reason="按已核验模板规则和完整当前输入计算。")
            result["source"]["unrounded_value"] = str(value)
            if op == "ticket_rate":
                result["value"] += "%"


def _month_label(plan, period, total=False):
    return {"field_id": "F052", "value": "累计" if total else str(int(period[-2:])),
            "unit": _unit(_fields(plan).get("F052", {})) or "月", "status": "request", "station_id": plan["station_id"], "period": period,
            "source": {"system": "本次请求", "request": plan["request"]}}


def _outputs(evaluator):
    for (fid, target), _rule in evaluator.rules.items():
        periods = evaluator.months if target == "ledger_month" else [evaluator.period]
        for period in periods:
            yield fid, target, period, evaluator.evaluate(fid, target, period)


def apply_derivations(plan, collected):
    """Apply reviewed finite rules in place, retaining precise missing inputs."""
    evaluator = _Evaluator(plan, collected)
    outputs = list(_outputs(evaluator))  # Evaluate all inputs before mutating rows.
    for fid, target, period, item in outputs:
        if target == "scalar":
            collected.setdefault("fields", {})[fid] = deepcopy(item)
        elif target == "ledger_month":
            row = evaluator.rows.setdefault(period, {"F052": _month_label(plan, period)})
            row[fid] = deepcopy(item)
            if period == plan["period"]:
                collected.setdefault("fields", {})[fid] = deepcopy(item)
        else:
            if evaluator.total_row is None:
                evaluator.total_row = {"F052": _month_label(plan, plan["period"], total=True)}
            evaluator.total_row[fid] = deepcopy(item)
    if any(target != "scalar" for _, target, _, _ in outputs):
        group = collected.setdefault("repeat_groups", {}).setdefault("records.1", {
            "status": "manual", "station_id": plan["station_id"], "period": plan["period"],
            "source": {"system": "待配置"}})
        group["records"] = [evaluator.rows[k] for k in sorted(evaluator.rows)]
        if evaluator.total_row is not None:
            group["records"].append(evaluator.total_row)
    return collected


def validate_derivations(plan, collected):
    """Recompute from validated base inputs; reject changed results or provenance."""
    evaluator = _Evaluator(plan, collected)
    count = 0
    core_keys = ("field_id", "unit", "station_id", "period", "status", "value", "source")
    for fid, target, period, expected in _outputs(evaluator):
        if target == "scalar":
            actual = collected.get("fields", {}).get(fid)
        elif target == "ledger_month":
            actual = evaluator.rows.get(period, {}).get(fid)
        else:
            actual = (evaluator.total_row or {}).get(fid)
        if not isinstance(actual, dict):
            raise ValueError(fid + "：派生结果未记录")
        for key in core_keys:
            if actual.get(key) != expected.get(key):
                raise ValueError(fid + "：派生结果或输入溯源被修改（" + key + "）")
        if target == "ledger_month" and period == plan["period"]:
            alias = collected.get("fields", {}).get(fid)
            if not isinstance(alias, dict) or any(alias.get(key) != actual.get(key) for key in core_keys):
                raise ValueError(fid + "：当月台账和标量结果不一致")
        count += 1
    return {"validated_derivation_count": count}
