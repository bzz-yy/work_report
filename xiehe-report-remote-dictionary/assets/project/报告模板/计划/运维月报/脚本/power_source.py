"""Read Power+ for one resolved monthly report; never replay historical reports.

Only request/config values and verified report values enter ``fields``. Current
platform attributes remain in ``platform_snapshot`` so a draft can show genuine
online evidence without presenting today's attributes as historical facts.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

PROJECT = next(p for p in Path(__file__).resolve().parents if (p/'报告索引.csv').is_file())
sys.path.insert(0, str(PROJECT / '数据字典/脚本'))
from query_power_bill import source_catalog
from station_config import load_station_config
from source_values import bill_command
from selected_source import choose_value
from work_evidence import bind_work_records,bind_photos


TZ = dt.timezone(dt.timedelta(hours=8))


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _data(response):
    value = response.get("data") if response.get("ok") else None
    return value if isinstance(value, dict) else {}


def _run(command):
    """CLI owns authentication. Do not inspect profiles, tokens, or stderr."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300 if '--photos-out' in command or '--attachments-out' in command else 40)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "TIMEOUT"}}
    except OSError:
        return {"ok": False, "error": {"code": "CLI_UNAVAILABLE"}}
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"code": "NON_JSON_RESPONSE"}}
    if not isinstance(response, dict):
        return {"ok": False, "error": {"code": "UNEXPECTED_RESPONSE"}}
    if not response.get("ok") or result.returncode:
        # A backend error may contain debug headers; retain the error code only.
        error = response.get("error", {})
        code = error.get("code", "QUERY_FAILED") if isinstance(error, dict) else "QUERY_FAILED"
        if code == 'CONTRACT_ERROR':
            raise ValueError('数据来源身份、期间或接口契约校验失败，不能作为缺项继续')
        return {"ok": False, "error": {"code": code}}
    return response


def _window(period):
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", period):
        raise ValueError("取数需要明确的年月")
    year, month = map(int, period.split("-"))
    start = dt.datetime(year, month, 1, tzinfo=TZ)
    end = dt.datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59, tzinfo=TZ)
    utc = lambda value: value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return year, month, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), utc(start), utc(end)


def assess_monthly_response(response):
    if not response.get("ok"):
        return "missing", "Power+报表查询未成功；保留待人工填写。"
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return "missing", "Power+返回结构不符合已核验的报表列表结构。"
    if data["records"] == [] and data.get("total") == 0:
        return "missing", "本站当月报表返回空记录，不能转换为0。"
    return "unverified", "报表返回记录，但非空结果的字段路径和报告口径尚未核验；保留待人工填写。"


def collect(plan: dict, out_dir: Path, profile="me") -> dict:
    """Collect one station/month. ``out_dir`` may exist; evidence files may not.

    All executable field bindings come from the effective resolver plan, so a
    station override cannot be bypassed by re-reading a template's old binding.
    The current monthly-energy binding is a verified query, not yet a verified
    nonempty-value contract. It therefore reports missing/unverified explicitly.
    """
    if plan.get("data_mode") != "powerplus":
        raise ValueError("线上取数器只接受powerplus计划，禁止历史回放混入")
    from fixed_fields import fresh_bindings, without_fixed_sources
    plan = without_fixed_sources(plan, fresh_bindings(plan))
    catalog = source_catalog()
    station = load_station_config(plan["station_config"], _read)
    if station["station_id"] != plan["station_id"]:
        raise ValueError("取数计划与电站配置串站")
    code = station.get("powerplus_station_id")
    identity = station.get("powerplus_identity_evidence", {})
    if (not code or identity.get("status") != "verified_unique_name_match"
            or str(identity.get("powerplus_station_id")) != str(code)):
        raise ValueError("本站Power+身份尚未核验")
    for field in plan["fields"]:
        if field["station_id"] != plan["station_id"] or field["period"] != plan["period"]:
            raise ValueError("字段计划串站或串期间")
    year, month, start, end, utc_start, utc_end = _window(plan["period"])
    destination = Path(out_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    context = {"station_id": plan["station_id"], "powerplus_station_id": code, "period": plan["period"]}
    evidence = []

    def query(name, command, scope):
        path = destination / (name + ".json")
        if path.exists():
            raise FileExistsError("已有同名取数证据，请使用新的输出目录：" + str(path))
        response = _run(command)
        captured = dt.datetime.now(TZ).isoformat(timespec="seconds")
        _write(path, response)
        meta = response.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        source = {"system": "Power+", **context, "captured_at": captured,
                  "command": command, "endpoint": meta.get("endpoint"),
                  "evidence_file": str(path), "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "period_scope": scope}
        evidence.append({"query": name, "ok": bool(response.get("ok")), "source": source})
        return response, source

    fields = {}
    for field in plan["fields"]:
        fid, mode = field["field_id"], field["source_mode"]
        source = {"system": "配置", **context, "dictionary_file": field["dictionary_file"],
                  "dictionary_pointer": field["dictionary_pointer"], "source_mode": mode}
        value = None
        status, reason = "manual", "尚无经确认的当期取数方法，请人工填写。"
        position = field.get("source_position") or {}
        if mode == "request":
            if position.get("kind") != "request" or position.get("param") not in {"year", "month"}:
                raise ValueError("未支持的请求参数绑定：" + fid)
            value = {"year": year, "month": month}[position["param"]]
            status, reason = "request", "来自本次明确的报告年月。"
            source = {"system": "本次请求", **context, "request": plan["request"]}
        elif mode == "station_profile":
            if position.get("kind") != "station_profile" or not position.get("property"):
                raise ValueError("未支持的电站档案绑定：" + fid)
            value = station.get("profile", {}).get(position["property"])
            if value is not None:
                status, reason = "config", "来自本站报告命名配置，正式命名及生效范围仍待确认。"
                source = {"system": "本站配置", **context, "file": plan["station_config"],
                          "pointer": "/profile/" + position["property"], "confirmation": station.get("profile_evidence")}
        elif mode == "pending_business_rule":
            reason = "业务填写规则尚未确认，请人工填写。"
        elif mode == "independent_photos":
            reason = "尚未配置本站当期独立照片来源，请人工插入；未复用历史照片。"
        elif mode not in {"local_history_replay", "manual", "catalog", "record_collection", "attachment", "derived"}:
            raise ValueError("未支持的有效来源方式：" + mode)
        fields[fid] = {"field_id": fid, "label": field["label"], "value": value,
                       "status": status, "unit": field["unit"], **context, "source": source, "reason": reason}

    substitutions = {'${profile}': profile, '${station_code}': str(code)}
    archive_command = [substitutions.get(part, part) for part in catalog['query_methods']['power.station.archive']['command']]
    archive, archive_source = query("本站当前档案", archive_command, "采集时的当前档案，未证明报告月末历史属性")
    record = _data(archive).get("record")
    record = record if isinstance(record, dict) else {}
    if record and (str(record.get("stationCode")) != str(code)
                   or record.get("stationName") != identity.get("powerplus_station_name")):
        raise ValueError("Power+当前返回身份与已核验电站不一致，请重新核查对应关系")
    snapshot = {"status": "real" if record else "missing", **context,
                "report_period": plan["period"], "captured_at": archive_source["captured_at"],
                "period_scope": archive_source["period_scope"], "items": [],
                "reason": "当前档案只作在线核验，未写入历史月度容量或经营指标。"}
    attributes = {"stationName": ("Power+电站名称", ""), "stationCode": ("Power+电站编码", ""),
                  "provinceName": ("省份", ""), "cityName": ("城市", ""), "countyName": ("区县", ""),
                  "stationCapacity": ("当前平台容量", "单位及历史口径待确认"),
                  "coverType": ("屋顶类型", ""), "gridVoltageLevel": ("并网电压等级", ""),
                  "consumptionType": ("消纳方式", "")}
    for key, (label, unit) in attributes.items():
        if record.get(key) is not None and record[key] != "":
            snapshot["items"].append({"key": key, "label": label, "value": record[key], "status": "real",
                                      "unit": unit, **context, "source": {**archive_source, "value_path": "data.record." + key}})

    observations = []
    groups = {key: {"status": "manual", "records": [], **context,
                    "source": {"system": "待配置", **context},
                    "reason": "未取得本站当期完整记录集合，保留缺项。"}
              for key in plan["repeat_group_rules"]}
    responses = {}
    photo_policy=plan.get('photo_selection') or {}
    photo_directory=destination.parent/'图片'
    meter_policy=_read(plan['meter_policy_file']) if plan.get('meter_policy_file') else None
    active_attachments=[f for f in plan['fields'] if f.get('attachment_selection') and f['source_mode'] in {'catalog','attachment'}]
    def fetch(period):
        if period not in responses:
            command=bill_command(catalog,code,period,profile)
            if period==plan['period'] and 'SM020' in photo_policy.get('mapping_ids',[]):
                command+=['--photos-out',str(photo_directory)]
            if meter_policy and active_attachments and period in meter_policy.get('allowed_container_periods',[]):
                command+=['--meter-policy-file',plan['meter_policy_file'],'--attachments-out',str(destination.parent/'结算附件'/period)]
            response, source = query('结算_' + period,
                command, '结算自然月；核对详情及关联子表')
            source['period'] = period
            responses[period] = response, source
        return responses[period]
    selected = [field for field in plan['fields']
                if field['source_mode'] == 'catalog' and field.get('source_selection')]
    ledger = {}
    for field in selected:
        selection = field['source_selection']
        mapping = catalog['source_mappings'][selection['mapping_id']]
        if mapping['query_method_id'] != 'power.electricitybill.settlement.v1':
            raise ValueError('报告选源的取数适配尚未实现')
        if selection['operation'] != 'value':
            raise ValueError('该报告尚未启用此计算方式')
        group = field['effective_filling_rule'].get('group')
        periods = ([plan['period'][:4] + f'-{m:02}' for m in range(1, month + 1)]
                   if group == 'records.1' else [plan['period']])
        for period in periods:
            response, source = fetch(period)
            value = choose_value(field, selection, catalog, response, source, plan.get('source_controls', {}))
            if group == 'records.1':
                row = ledger.setdefault(period, {'F052': {'field_id': 'F052', 'value': str(int(period[-2:])),
                    'unit': '月', 'status': 'request', 'station_id': plan['station_id'], 'period': period,
                    'source': {'system': '本次请求', 'request': plan['request']}}})
                row[field['field_id']] = value
                if period == plan['period']: fields[field['field_id']] = value
            else:
                fields[field['field_id']] = value
    if ledger:
        groups['records.1']['records'] = [ledger[p] for p in sorted(ledger)]
        groups['records.1']['reason'] = '逐月填写已验证的来源；各列累计仅在本列月份完整时计算，仍有缺项的列保留待填。'
    if meter_policy and active_attachments:
        attachment_periods=set()
        for field in active_attachments:
            attachment_periods.update([plan['period'][:4]+f'-{m:02}' for m in range(1,month+1)]
                if field['effective_filling_rule'].get('group')=='records.1' else [plan['period']])
        containers=meter_policy.get('allowed_container_periods',[])
        for period in sorted(attachment_periods & set(meter_policy.get('allowed_metric_periods',[])) & set(containers)):
            fetch(period)
        auxiliary=meter_policy.get('auxiliary_station_use',{}).get('allowed_business_periods',[])
        needed=[p for p in auxiliary if p in attachment_periods]
        if needed and containers and not any(p in responses for p in containers):
            # A verified earlier business-month row can live in a later workbook.
            # The binder retains both periods and excludes future metric rows.
            fetch(sorted(containers)[0])
    photos=[];photo_rejections=[]
    if 'SM020' in photo_policy.get('mapping_ids',[]):
        response,source=fetch(plan['period'])
        photos.extend(bind_photos(plan,response,source,'SM020'))
        photo_rejections.extend(_data(response).get('rejected_photos',[]))
    if ('SM016' in photo_policy.get('mapping_ids',[]) or any(f.get('record_selection') for f in plan['fields'])):
        method=catalog['query_methods']['power.other_work.executions.v1']
        adapter=(PROJECT/'数据字典'/method['adapter_script']).resolve()
        interpreter=Path.home()/'Library/Application Support/xhyw-power-cli/runtime/python/bin/python3'
        command=[str(interpreter),'-B',str(adapter),'--station-code',str(code),'--period',plan['period'],'--profile',profile]
        if 'SM016' in photo_policy.get('mapping_ids',[]):command+=['--photos-out',str(photo_directory)]
        response,source=query('当期其他工单执行记录',command,'按每条实际处理时间选取报告月份，不用创建时间替代')
        rows=bind_work_records(plan,response,source)
        groups['records.3']['records']=rows
        groups['records.3']['reason']=f'取得{len(rows)}条已接入的安全管理处理记录；其他工单类型和未明确分类仍需补齐。'
        photos.extend(bind_photos(plan,response,source,'SM016'))
        photo_rejections.extend(_data(response).get('rejected_photos',[]))
    plan_candidates=[]
    plan_policy=plan.get('plan_source_policy') or {}
    if (plan_policy.get('enabled') is True and plan['period'] in plan_policy.get('periods',[])
            and any(f.get('plan_selection') and f['source_mode']=='record_collection' for f in plan['fields'])):
        from plan_values import bind_plan_records
        method=catalog['query_methods']['power.plan.triggers.v1']
        adapter=PROJECT/'数据字典'/method['adapter_script']
        interpreter=Path.home()/'Library/Application Support/xhyw-power-cli/runtime/python/bin/python3'
        response,source=query('当月已下发现场计划',[str(interpreter),'-B',str(adapter),'--station-code',str(code),'--period',plan['period'],'--profile',profile],
            '按应下发与实际下发日期核对当月计划，并验证关联同站工单；不代表工作完成')
        groups['records.2']['records']=bind_plan_records(plan,response,source,catalog)
        groups['records.2']['reason']='仅填写已核验的当月下发现场计划；风险列和其他计划仍待补。'
        plan_candidates=_data(response).get('next_month_candidates',[])
        if plan_candidates:
            fields['F069']['reason']=f'本次找到{len(plan_candidates)}条下一月计划候选，但缺报告期末的内容版本，保留待填。'
    unique_photos=[];seen_photo_hashes=set()
    for photo in photos:
        if photo['sha256'] in seen_photo_hashes:
            photo_rejections.append({'reason':'duplicate_across_sources','sha256':photo['sha256']});continue
        seen_photo_hashes.add(photo['sha256']);unique_photos.append(photo)
    if 'F090' in fields:
        fields['F090']['reason']=f'已取得{len(unique_photos)}张经来源关联的照片；其余空项、下载失败或日期不符项保留记录。'
    if 'F015' in fields:
        has_capacity = any(item['key']=='stationCapacity' for item in snapshot['items'])
        fields['F015']['reason'] = ('当前平台容量已单列；历史容量的单位与生效范围未确认。' if has_capacity
                                    else '本次未取得当前平台容量；历史容量的单位与生效范围也未确认。')
    if 'F011' in fields:
        missing_months=[str(int(p[-2:]))+'月' for p,row in ledger.items() if row.get('F054',{}).get('status')!='real']
        fields['F011']['reason'] = ('年度累计需要1月至报告月完整来源；当前未取得'+ '、'.join(missing_months) + '结算，不用部分月份相加代替。'
            if missing_months else '逐月数据已取得，但本模板的累计字段计算尚未配置；不自动填写。')
    collected={"schema_version": 2, **context, "fields": fields, "repeat_groups": groups,
            "platform_snapshot": snapshot, "observations": observations, "evidence": evidence,
            'photos':{'items':unique_photos,'rejected':photo_rejections,'status':'retrieved' if unique_photos else 'missing'},
            "source_queries": {period: {'status': _data(response).get('status', 'query_failed'),
                'source': source} for period, (response, source) in responses.items()},
            'plan_candidates':plan_candidates,
            "history_replay_used": False, "formal_report_ready": False}
    if meter_policy and active_attachments:
        from bill_meter_values import bind_meter_values
        bind_meter_values(plan,collected,responses,catalog)
    if any(f.get('calculation_rule') or f.get('ledger_total_rule') for f in plan['fields']):
        from derived_fields import apply_derivations
        from source_guides import apply_guidance
        apply_guidance(plan,collected)
        apply_derivations(plan,collected)
    from fixed_fields import apply_fixed
    apply_fixed(plan,collected)
    return collected
