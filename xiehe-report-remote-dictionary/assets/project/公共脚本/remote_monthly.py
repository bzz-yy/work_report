"""Remote publication -> scoped monthly values -> existing Word presentation.

The old D values are migration lookup keys only. This module never loads a local
public catalog and never calls the legacy resolver or collection validators.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import calendar
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ID = 'NW-MONTHLY-STD-01'
ENERGY_FIELDS = {'F004': 'D006', 'F005': 'D007', 'F006': 'D008', 'F054': 'D006'}


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _local(project, path):
    path = Path(path).resolve()
    if not path.is_relative_to(project) or not path.is_file():
        raise ValueError('报告配置路径无效或越界：' + str(path))
    return path


def _binding(catalog, previous_id, reader=None):
    binding = catalog.get('bindings', {}).get(previous_id)
    if not binding or not binding.get('executable'):
        return None
    sid = binding.get('standard_id')
    if not isinstance(sid, str) or not sid.startswith('STD-') or sid not in catalog.get('fields', {}):
        raise ValueError('可执行绑定没有本次已发布STD字段：' + previous_id)
    if reader is not None and binding.get('reader') != reader:
        raise ValueError('远程字段读取器与已启用月报用途不符：' + sid)
    return binding


def build_plan(request, station_id=None, period=None, template_id=None, *, project=ROOT, catalog=None):
    """Resolve static station/template identity without a public D/SM catalog."""
    from report_route import route_request
    from station_config import load_station_config
    from fixed_fields import template_content

    project = Path(project).resolve()
    routed = route_request(project, request, station_id, period, '运维月报', template_id)
    if routed.get('route') != 'generate':
        raise ValueError('报告请求需先接入或澄清：' + routed.get('reason', '未识别请求'))
    if routed['template_id'] != TEMPLATE_ID:
        raise ValueError('远程月报仅支持已核验的NW-MONTHLY-STD-01模板')
    station_path = _local(project, routed['station_config'])
    station = load_station_config(station_path)
    base_path = _local(project, station['station_base_file'])
    base = _read(base_path)
    if station.get('readiness', {}).get('simulation') or base.get('onboarding', {}).get('simulation'):
        raise ValueError('远程月报不查询合成电站')
    mapping_path = _local(project, station_path.parent / station['template_mapping'])
    mapping = _read(mapping_path)
    template_path = _local(project, mapping_path.parent / mapping['template_file'])
    rule_path = _local(project, mapping_path.parent / mapping['retrieval_rules'])
    common = _read(rule_path)
    if mapping['template_id'] != TEMPLATE_ID or common['template_id'] != TEMPLATE_ID:
        raise ValueError('月报模板或取值规则串版本')
    if _sha(template_path) != mapping['template_sha256']:
        raise ValueError('模板原件摘要与填写位置映射不符')
    expected_fields = {f'F{i:03}' for i in range(1, 91)}
    if set(common['fields']) != expected_fields or {f['id'] for f in mapping['fields']} != expected_fields:
        raise ValueError('远程月报须完整保留90个Word填写位置')
    groups = common['filling']['repeat_groups']
    if set(groups) != {g['key'] for g in mapping['repeat_groups']}:
        raise ValueError('模板循环表规则与位置映射不符')
    policy = template_content(common)
    catalog = catalog or {'bindings': {}}
    fields = []
    for fid, field in common['fields'].items():
        definition = field.get('data_definition') or {}
        old = (definition.get('previous_standard_id') or (field.get('previous_data_definition') or {}).get('standard_id')
               or definition.get('standard_id'))
        binding = catalog.get('bindings', {}).get(old) or {}
        sid = binding.get('standard_id')
        if sid is not None and (not isinstance(sid, str) or not sid.startswith('STD-')):
            raise ValueError('运行计划的有效数据项必须是STD编号')
        if ('previous_standard_id' in definition or field.get('previous_data_definition')) and definition.get('standard_id') != sid:
            raise ValueError('模板有效STD引用与本次发布对应不一致：' + fid)
        fields.append({'field_id': fid, 'label': field['filling_rule']['label'],
                       'unit': field['filling_rule']['unit'],
                       'previous_standard_id': old,
                       'data_definition': {'standard_id': sid, 'parameters': deepcopy(definition.get('parameters', {}))},
                       'remote_binding': deepcopy(binding) if binding else None,
                       'source_mode': field.get('source_mode'),
                       'effective_filling_rule': deepcopy(field['filling_rule'])})
    files = [station_path, base_path, mapping_path, template_path, rule_path]
    binding_path = mapping_path.parent / '远程字段对应.json'
    if binding_path.is_file():
        files.append(binding_path)
    return {'request': request, 'station_id': station['station_id'], 'station_name': station['station_name'],
            'period': routed['period'], 'template_id': TEMPLATE_ID, 'report_type': '运维月报',
            'template_file': str(template_path), 'template_sha256': mapping['template_sha256'],
            'mapping_file': str(mapping_path), 'template_rules_file': str(rule_path),
            'template_rules_sha256': _sha(rule_path), 'station_config': str(station_path),
            'station_base_file': str(base_path), 'station_profile': deepcopy(station.get('profile', {})),
            'platform_identity': deepcopy(station['powerplus_identity_evidence']),
            'bound_parameters': {'station_id': station['station_id'],
                                 'powerplus_station_id': str(station['powerplus_station_id']),
                                 'period': routed['period']},
            'fields': fields, 'common_rules': common, 'template_fixed_policy': policy,
            'repeat_group_rules': deepcopy(groups), 'configuration_files': [str(p) for p in files],
            'configuration_sha256': {str(p): _sha(p) for p in files},
            'formal_report_ready': False, 'historical_report_used': False}


def _cell(plan, fid, value=None, status='missing', reason='', source=None, period=None):
    field = next(f for f in plan['fields'] if f['field_id'] == fid)
    return {'field_id': fid, 'label': field['label'], 'unit': field['unit'],
            'station_id': plan['station_id'], 'period': period or plan['period'],
            'value': value, 'status': status, 'reason': reason, 'source': source or {}}


def _evidence(plan, value):
    supplied = value.get('evidence_file')
    if not isinstance(supplied, (str, Path)) or not str(supplied):
        raise ValueError('本次平台响应证据文件未提供')
    path = Path(supplied).resolve()
    root = Path(plan['evidence_directory']).resolve()
    expected = value.get('evidence_sha256')
    if not path.is_relative_to(root) or not path.is_file() or not isinstance(expected, str) or _sha(path) != expected:
        raise ValueError('本次平台响应证据缺失、越界或摘要不符')
    try:
        captured = datetime.fromisoformat(value['captured_at'])
        if captured.tzinfo is None:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError('平台响应采集时间无效或缺少时区') from None
    return {'system': 'Power+', 'evidence_file': str(path), 'evidence_sha256': expected,
            'captured_at': value['captured_at'], 'station_id': plan['station_id'],
            'powerplus_station_id': plan['bound_parameters']['powerplus_station_id']}


def _number(value):
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        raise ValueError('平台电量不是有效数值')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('平台电量不是有效数值') from None
    if not number.is_finite() or number < 0:
        raise ValueError('平台电量不能为负数或非有限数')
    return number


def _at_path(value, path):
    try:
        for part in path.split('.'):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError('原始响应路径不存在') from None


def _evidence_rows(document, form_key):
    rows, totals, pages = [], set(), []
    for step in document.get('evidence', []):
        request = step.get('request') or {}; body = request.get('body') or {}
        if body.get('formKey') != form_key:
            continue
        if request.get('method') != 'POST' or request.get('path') != '/api/blade-form/form/data/list':
            raise ValueError('结算原始证据接口与已启用读取器不符')
        data = (step.get('response_excerpt') or {}).get('data') or {}
        total = data.get('totalCount')
        if not isinstance(data.get('datas'), list) or isinstance(total, bool) or not str(total).isdigit():
            raise ValueError('结算原始分页证据不完整')
        totals.add(int(total)); rows.extend(data['datas']); pages.append(body.get('query', {}).get('current'))
    ids = [row.get('id') for row in rows if isinstance(row, dict)]
    if (len(totals) != 1 or len(rows) != next(iter(totals)) or len(ids) != len(rows)
            or any(i is None for i in ids) or len(set(ids)) != len(ids)
            or pages != list(range(1, len(pages) + 1))):
        raise ValueError('结算原始分页条数、页码或唯一记录不符')
    return rows


def _settlement_evidence(plan, entry, month):
    """Tie the transport summary back to the saved, original parent/child rows."""
    source = _evidence(plan, entry); document = _read(source['evidence_file'])
    code = plan['bound_parameters']['powerplus_station_id']
    if (str(document.get('station_code')) != str(code) or document.get('period') != month
            or document.get('captured_at') != entry.get('captured_at')):
        raise ValueError('结算原始证据错站、错期或采集时间不符')
    saved = document.get('result') or {}
    if (saved.get('status') != 'ok' or saved.get('values') != entry.get('values')
            or saved.get('order') != entry.get('order')):
        raise ValueError('结算汇总与保存的取数结果不符')
    parents = _evidence_rows(document, 'Electricitybill')
    children = _evidence_rows(document, 'ElectricitybillSettlement')
    if len(parents) != 1 or len(children) != 1:
        raise ValueError('结算原始证据不具有唯一父单和子表')
    row, child = parents[0], children[0]; parent = row.get('variables')
    if isinstance(parent, str):
        parent = json.loads(parent)
    if not isinstance(parent, dict):
        raise ValueError('结算父单原始variables无效')
    order = entry.get('order') or {}
    if (str(parent.get('station')) != str(code) or parent.get('settlement_month') != month
            or parent.get('status') != 'yjd' or row.get('processIsFinished') not in (None, 'finished')
            or not parent.get('r_id') or parent.get('formKey') not in (None, 'Electricitybill')
            or str(parent.get('id', row['id'])) != str(row['id']) or str(order.get('id')) != str(row['id'])):
        raise ValueError('结算原始父单身份、站月或完成状态不符')
    for key in ('r_id', 'station', 'status', 'settlement_month', 'settlement_month_start', 'settlement_month_end', 'tbl_number'):
        if order.get(key) != parent.get(key):
            raise ValueError('结算父单汇总字段与原始响应不符：' + key)
    if parent.get('settlement_month_start') is not None or parent.get('settlement_month_end') is not None:
        year, number = map(int, month.split('-'))
        if (parent.get('settlement_month_start'), parent.get('settlement_month_end')) != (month + '-01', month + f'-{calendar.monthrange(year, number)[1]:02}'):
            raise ValueError('结算原始起止日期不覆盖报告自然月')
    if (child.get('Electricitybill_CORRELATION_ID') != parent['r_id'] or not child.get('r_id')
            or child.get('Electricitybill_CORRELATION_STATUS') not in (None, 'yjd')
            or order.get('child_r_id') != child['r_id'] or str(order.get('child_id')) != str(child.get('id'))):
        raise ValueError('结算子表与父单或汇总的唯一关联不符')
    return source, document, child


def _display(value, display):
    expected = {'kWh': Decimal('1'), '万kWh': Decimal('0.0001')}
    try:
        factor = Decimal(str(display.get('factor')))
    except InvalidOperation:
        raise ValueError('月报电量换算系数无效') from None
    if (display.get('source_unit') != 'kWh' or display.get('to_unit') not in expected
            or factor != expected[display['to_unit']] or display.get('rounding') != 'half_up'):
        raise ValueError('月报电量显示单位或换算规则未被支持')
    places = display.get('decimal_places')
    if isinstance(places, bool) or not isinstance(places, int) or not 0 <= places <= 10:
        raise ValueError('月报电量显示精度无效')
    result = format((value * factor).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), 'f')
    if display.get('trim_trailing_zeros') and '.' in result:
        result = result.rstrip('0').rstrip('.')
    return result


def _energy_cell(plan, catalog, raw, fid, month):
    old = ENERGY_FIELDS[fid]
    binding = _binding(catalog, old, 'electricitybill_settlement.v1')
    if binding is None:
        reason = (catalog.get('bindings', {}).get(old) or {}).get('reason') or '该位置尚无本次可执行的服务器字段对应'
        return _cell(plan, fid, reason=reason, period=month)
    sid = binding['standard_id']
    entry = raw.get('months', {}).get(month) or {'status': 'missing'}
    status = entry.get('status')
    if status not in {'ok', 'missing', 'ambiguous', 'error'}:
        raise ValueError('结算月返回未知状态：' + month)
    if status != 'ok':
        reasons = {'missing': '未取得唯一当期结算记录', 'ambiguous': '当期结算记录存在歧义', 'error': '当期平台查询未成功'}
        return _cell(plan, fid, reason=month + '：' + reasons[status], period=month)
    source, document, child = _settlement_evidence(plan, entry, month)
    value = entry.get('values', {}).get(sid)
    if value is None:
        return _cell(plan, fid, reason=month + '：响应中缺少本次STD字段值', period=month)
    if not isinstance(value, dict) or value.get('quality') not in {'valid', 'zero_requires_confirmation', 'missing'}:
        raise ValueError('远程电量质量状态无效：' + sid)
    original = child.get(binding.get('prop'))
    if value.get('raw_value') != original:
        raise ValueError('电量汇总值与原始子表字段不符：' + sid)
    quality = 'missing' if original is None or original == '' else 'zero_requires_confirmation' if _number(original) == 0 else 'valid'
    if value.get('quality') != quality or value.get('unit') != 'kWh':
        raise ValueError('电量质量或单位与原始字段及契约不符：' + sid)
    expected_path = 'data.datas[unique r_id=' + child['r_id'] + '].' + binding['prop']
    if value.get('value_path') != expected_path:
        raise ValueError('电量返回路径与唯一子表及STD字段不符')
    if value.get('quality') != 'valid':
        reason = '原始0仍需核实，不作为真实零填入' if value['quality'] == 'zero_requires_confirmation' else '字段本期缺数'
        return _cell(plan, fid, reason=month + '：' + reason,
                     source={**source, 'period': month, 'remote_field_id': sid, 'standard_id': sid,
                             'raw_value': value.get('raw_value'), 'source_unit': value.get('unit'),
                             'quality': value['quality']}, period=month)
    if value.get('unit') != 'kWh':
        raise ValueError('远程结算电量单位与已启用契约不符：' + sid)
    number = _number(value.get('raw_value'))
    if number == 0:
        raise ValueError('原始0不能绕过zero_requires_confirmation标记')
    display = plan['common_rules']['fields'][fid]['source_selection']['display']
    source.update(period=month, remote_field_id=sid, standard_id=sid,
                  source_method=binding['reader'], response_field=binding.get('prop'),
                  raw_value=str(number), source_unit='kWh', display=deepcopy(display),
                  quality='valid',
                  label='Power+当期结算记录（服务器STD契约）')
    if value.get('value_path'):
        if not isinstance(value['value_path'], str):
            raise ValueError('远程电量证据路径必须是字符串')
        source['value_path'] = value['value_path']
    return _cell(plan, fid, _display(number, display), 'real', '已核对本期结算记录及服务器字段契约', source, month)


def _apply_template_fixed(plan, collected):
    from fixed_fields import item
    policy = plan['template_fixed_policy']
    definitions = {f['field_id']: f for f in plan['fields']}
    bindings = {}
    for fid, value in policy.get('fields', {}).items():
        decision = {k: deepcopy(policy[k]) for k in ('scope', 'confirmed_by', 'confirmed_at', 'reason', 'evidence')}
        decision.update(status='confirmed_fixed', value=deepcopy(value))
        binding = {'active': True, 'scope_kind': 'template', 'decision': decision,
                   'template_rules_sha256': plan['template_rules_sha256'],
                   'value_kind': 'record_column' if isinstance(value, list) else 'scalar',
                   'reason': '按已批准的本模板位置固定内容填写；有意留空不显示待填。'}
        bindings[fid] = binding
        if not isinstance(value, list):
            collected['fields'][fid] = item(plan, definitions[fid], binding, value)
    for key, group in plan['repeat_group_rules'].items():
        ids = group['field_ids']
        if not any(fid in bindings for fid in ids):
            continue
        if not all(fid in bindings for fid in ids):
            raise ValueError('模板固定循环内容未完整成组')
        lengths = {len(bindings[fid]['decision']['value']) for fid in ids}
        if len(lengths) != 1:
            raise ValueError('模板固定循环内容行数不一致')
        records = [{fid: item(plan, definitions[fid], bindings[fid],
                              bindings[fid]['decision']['value'][i], i) for fid in ids}
                   for i in range(lengths.pop())]
        collected['repeat_groups'][key].update(status='fixed', records=records, reason='已批准模板固定记录')


def build_collected(plan, catalog, raw):
    """Validate only the published remote contracts that this runner can use."""
    if catalog.get('schema_version') != 3:
        raise ValueError('远程月报需要第三版实时执行目录')
    if not isinstance(raw, dict) or not isinstance(raw.get('months', {}), dict) or not isinstance(raw.get('photos', []), list):
        raise ValueError('远程取数结果结构无效')
    code, period = plan['bound_parameters']['powerplus_station_id'], plan['period']
    if str(raw.get('station_code')) != str(code) or raw.get('period') != period:
        raise ValueError('远程取数结果错站或错期')
    if raw.get('historical_fallback_used') is not False or raw.get('publication') != catalog['metadata']:
        raise ValueError('远程取数发布不一致或未证明没有历史回退')
    common = plan['common_rules']
    fields = {}
    for field in plan['fields']:
        binding = field.get('remote_binding') or {}
        reason = binding.get('reason') if binding and not binding.get('executable') else None
        reason = reason or ('计算输入未齐备或本版尚未启用该计算' if common['fields'][field['field_id']].get('calculation_rule')
                            else '本位置尚无已启用的服务器取数对应；保留人工填写')
        fields[field['field_id']] = _cell(plan, field['field_id'], reason=reason)
    collected = {'station_id': plan['station_id'], 'period': period, 'publication': deepcopy(catalog['metadata']),
                 'fields': fields, 'repeat_groups': {key: {'status': 'missing', 'records': [],
                 'reason': '尚未取得本期已核验的完整业务记录'} for key in plan['repeat_group_rules']},
                 'photos': {'status': 'missing', 'items': [], 'rejected': deepcopy(raw.get('rejected_photos', []))},
                 'historical_report_used': False, 'historical_fallback_used': False}
    for fid, value in {'F002': int(period[:4]), 'F003': int(period[5:])}.items():
        fields[fid] = _cell(plan, fid, value, 'request', '来自本次报告年月', {'system': '本次请求', 'request': plan['request']})
    for fid, prop in {'F001': 'cover_name', 'F051': 'overview_short_name', 'F087': 'revenue_name'}.items():
        value = plan['station_profile'].get(prop)
        if isinstance(value, str) and value.strip():
            fields[fid] = _cell(plan, fid, value, 'config', '来自本站对应名称用途配置',
                                {'system': '本站配置', 'file': plan['station_config'], 'pointer': '/profile/' + prop})
    archive = raw.get('archive') or {'status': 'missing'}
    if not isinstance(archive, dict) or archive.get('status') not in {'ok', 'missing', 'error'} or not isinstance(archive.get('values', {}), dict):
        raise ValueError('当前档案响应状态或结构无效')
    snapshot = {'station_id': plan['station_id'], 'period': period, 'powerplus_station_id': code,
                'period_scope': '采集时当前档案，不替代报告月末历史属性', 'items': []}
    if archive.get('status') == 'ok':
        source = _evidence(plan, archive)
        archive_document = _read(source['evidence_file'])
        archive_steps = archive_document.get('evidence') or []
        if (str(archive_document.get('station_code')) != str(code)
                or archive_document.get('captured_at') != archive['captured_at']
                or (archive_document.get('result') or {}).get('values') != archive.get('values')
                or len(archive_steps) != 1):
            raise ValueError('档案汇总与原始响应证据不一致')
        original_archive = (archive_steps[0].get('response_excerpt') or {}).get('data')
        if not isinstance(original_archive, dict) or str(original_archive.get('stationCode')) != str(code):
            raise ValueError('档案原始响应身份不符')
        snapshot['captured_at'] = source['captured_at']
        for old in ('D001', 'D002', 'D003'):
            binding = _binding(catalog, old, 'station_archive.v1')
            if binding is None:
                continue
            sid = binding['standard_id']; value = archive.get('values', {}).get(sid)
            if value != original_archive.get(binding.get('prop')):
                raise ValueError('档案STD值与原始返回字段不符：' + sid)
            if value is None or value == '':
                continue
            if old == 'D001' and str(value) != str(code):
                raise ValueError('当前档案电站编码与本站不符')
            if old == 'D002' and value != plan['platform_identity']['powerplus_station_name']:
                raise ValueError('当前档案电站名称与已核验本站身份不符')
            if isinstance(value, (bool, dict, list)) or (isinstance(value, float) and not Decimal(str(value)).is_finite()):
                raise ValueError('当前档案值不是可核验标量')
            definition = catalog['fields'][sid]
            snapshot['items'].append({'label': definition.get('name_zh') or binding.get('prop') or sid,
                                     'value': value, 'status': 'real', 'unit': definition.get('unit') or '',
                                     'source': {**source, 'period': period, 'remote_field_id': sid, 'standard_id': sid}})
    snapshot['status'] = 'real' if snapshot['items'] else 'missing'
    snapshot['note'] = '当前容量的单位、交流/直流口径及历史生效期未核时，仅列档案附页。'
    collected['platform_snapshot'] = snapshot
    month_keys = [period[:4] + f'-{m:02}' for m in range(1, int(period[5:]) + 1)]
    if set(raw.get('months', {})) - set(month_keys):
        raise ValueError('远程结算返回超出本报告年度期间的月份')
    ledger = []
    for month in month_keys:
        row = {fid: _cell(plan, fid, reason='本月该列未取得已核验输入', period=month)
               for fid in plan['repeat_group_rules']['records.1']['field_ids']}
        row['F052'] = _cell(plan, 'F052', str(int(month[5:])), 'request', '本次年度台账月份', {'system': '本次请求'}, month)
        row['F054'] = _energy_cell(plan, catalog, raw, 'F054', month)
        ledger.append(row)
    for fid in ('F004', 'F005', 'F006'):
        fields[fid] = _energy_cell(plan, catalog, raw, fid, period)
    fields['F054'] = deepcopy(ledger[-1]['F054'])
    fields['F052'] = deepcopy(ledger[-1]['F052'])
    total_row = {fid: _cell(plan, fid, reason='累计输入不完整，不能把部分月份当全年累计')
                 for fid in plan['repeat_group_rules']['records.1']['field_ids']}
    total_row['F052'] = _cell(plan, 'F052', '累计', 'request', '年度累计行', {'system': '本次请求'})
    absent = [month for month, row in zip(month_keys, ledger) if row['F054']['status'] != 'real']
    if not absent:
        total = sum((Decimal(row['F054']['source']['raw_value']) for row in ledger), Decimal('0'))
        source = {'system': '模板计算', 'label': '1月至报告月完整结算电量之和',
                  'inputs': [{'field_id': 'F054', 'period': m, 'source': deepcopy(row['F054']['source'])}
                             for m, row in zip(month_keys, ledger)], 'raw_sum_kwh': str(total)}
        rule = common['fields']['F011']['calculation_rule']
        display = {**rule['display'], 'source_unit': 'kWh', 'to_unit': rule['output_unit'], 'factor': rule['factor']}
        fields['F011'] = _cell(plan, 'F011', _display(total, display), 'derived', '仅完整月份输入计算累计', source)
        total_rule = common['fields']['F054']['ledger_total_rule']
        display = {**total_rule['display'], 'source_unit': 'kWh', 'to_unit': total_rule['output_unit'], 'factor': total_rule['factor']}
        total_row['F054'] = _cell(plan, 'F054', _display(total, display), 'derived', '完整月份累计', source)
    else:
        source = {'system': '模板计算', 'missing_inputs': [{'field_id': 'F054', 'period': m} for m in absent]}
        fields['F011'] = _cell(plan, 'F011', reason='累计发电量缺少1月至报告月的完整有效输入', source=source)
        total_row['F054'] = _cell(plan, 'F054', reason=fields['F011']['reason'], source=source)
    collected['repeat_groups']['records.1'].update(status='partial' if absent else 'real',
        records=ledger + [total_row], reason='逐月独立读取；缺月、异常0及未核容量等保留待填，不用当月值填其他月份。')
    photo_binding = _binding(catalog, 'D019', 'electricitybill_photos.v1')
    seen = set()
    for photo in raw.get('photos', []):
        if photo_binding is None or photo.get('remote_field_id') != photo_binding['standard_id']:
            raise ValueError('照片不属于本次已启用的服务器STD来源')
        if str(photo.get('station_code')) != str(code) or photo.get('period') != period:
            raise ValueError('照片错站或错期')
        original = photo.get('source') or {}
        if (original.get('system') != 'Power+' or str(original.get('powerplus_station_id')) != str(code)
                or original.get('period') != period or original.get('station_id', plan['station_id']) != plan['station_id']):
            raise ValueError('照片来源上下文不完整或串站期')
        source = _evidence(plan, original)
        month_entry = raw.get('months', {}).get(period) or {}
        if (month_entry.get('status') != 'ok' or original.get('evidence_file') != month_entry.get('evidence_file')
                or original.get('evidence_sha256') != month_entry.get('evidence_sha256')):
            raise ValueError('照片未关联本次唯一结算原始响应')
        _, document, child = _settlement_evidence(plan, month_entry, period)
        attachment_path = photo.get('attachment_path')
        if not isinstance(attachment_path, str) or not re.fullmatch(r'evidence\.\d+\.response_excerpt\.data\.datas\.0\.meter_reading_photos\.\d+', attachment_path):
            raise ValueError('照片附件路径不在已核验结算子表')
        attachment = _at_path(document, attachment_path)
        child_path = attachment_path.split('.meter_reading_photos.')[0]
        if _at_path(document, child_path) != child or not isinstance(attachment, dict):
            raise ValueError('照片附件不属于本次唯一结算子表')
        if photo.get('name') != (attachment.get('name') or attachment.get('label') or ''):
            raise ValueError('照片名称与原始附件条目不符')
        url = attachment.get('url') or attachment.get('value')
        url_sha = url.get('sha256') if isinstance(url, dict) else None
        if not isinstance(url_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', url_sha) or photo.get('source_url_sha256') != url_sha:
            raise ValueError('照片下载URL摘要与原始附件不符')
        download = _evidence(plan, {'evidence_file': photo.get('download_evidence_file'),
            'evidence_sha256': photo.get('download_evidence_sha256'), 'captured_at': original.get('captured_at')})
        receipt = _read(download['evidence_file'])
        expected_receipt = {k: photo.get(k) for k in ('attachment_path', 'source_url_sha256', 'name', 'file', 'sha256', 'bytes', 'remote_field_id', 'station_code', 'period')}
        if (any(receipt.get(k) != v for k, v in expected_receipt.items()) or receipt.get('status') != 'downloaded'
                or receipt.get('source_evidence_file') != original['evidence_file']
                or receipt.get('source_evidence_sha256') != original['evidence_sha256']):
            raise ValueError('照片下载凭证与附件或原图记录不符')
        image = Path(photo.get('file', '')).resolve()
        run_root = Path(plan.get('run_directory') or Path(plan['evidence_directory']).parent).resolve()
        if not image.is_relative_to(run_root) or not image.is_file() or _sha(image) != photo.get('sha256'):
            raise ValueError('本次照片文件缺失、越界或摘要不符')
        if photo.get('bytes') != image.stat().st_size:
            raise ValueError('照片下载记录字节数不符')
        if not isinstance(photo.get('attachment_path'), str) or not photo['attachment_path'] or photo['sha256'] in seen:
            raise ValueError('照片附件引用缺失或原图重复')
        seen.add(photo['sha256']); sid = photo_binding['standard_id']
        collected['photos']['items'].append({**deepcopy(photo), 'station_id': plan['station_id'],
            'source': {**source, 'period': period, 'standard_id': sid, 'remote_field_id': sid,
                       'attachment_path': photo['attachment_path']}})
    if collected['photos']['items']:
        collected['photos']['status'] = 'retrieved'
        fields['F090']['reason'] = '已取得本次STD结算抄表照片；其他现场检查来源未发布或未启用，剩余位置待填。'
    else:
        fields['F090']['reason'] = '本次未取得已核验STD抄表照片；不查询未发布来源或使用历史图片。'
    _apply_template_fixed(plan, collected)
    return collected


def apply_photo_review(plan, collected, review):
    """Apply this run's explicit picture decisions; never alter image bytes."""
    if (not isinstance(review, dict) or review.get('station_id') != plan['station_id']
            or review.get('period') != plan['period'] or not isinstance(review.get('items'), list)):
        raise ValueError('图片审核清单站点、期间或结构不符')
    try:
        if datetime.fromisoformat(review['reviewed_at']).tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError('图片审核时间缺失或未带时区') from None
    decisions = {}
    for decision in review['items']:
        if not isinstance(decision, dict):
            raise ValueError('图片审核决定不是对象')
        sha = decision.get('sha256')
        if (not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha) or sha in decisions
                or decision.get('decision') not in {'include', 'exclude'}
                or not isinstance(decision.get('reason'), str) or not decision['reason'].strip()):
            raise ValueError('图片审核摘要、决定或原因无效')
        decisions[sha] = decision
    photos = collected['photos']['items']
    if set(decisions) != {photo['sha256'] for photo in photos}:
        raise ValueError('图片审核清单与本次新取回照片不完全对应，不能使用过期或不完整决定')
    accepted, excluded = [], []
    for photo in photos:
        decision = decisions[photo['sha256']]
        note = {'reviewed_at': review['reviewed_at'], 'decision': decision['decision'], 'reason': decision['reason']}
        if decision['decision'] == 'include':
            accepted.append({**photo, 'visual_review': note})
        else:
            excluded.append({**photo, 'reason': '图片审核排除：' + decision['reason'], 'visual_review': note})
    collected['photos']['items'] = accepted
    collected['photos']['rejected'].extend(excluded)
    collected['photos']['status'] = 'retrieved' if accepted else 'missing'
    collected['fields']['F090']['reason'] = f'本次图片审核保留{len(accepted)}张、排除{len(excluded)}张；排除原因和原图保留于本次证据，剩余照片槽待填。'
    return {'status': 'applied_to_exact_fresh_photo_hashes', 'reviewed_at': review['reviewed_at'],
            'included': len(accepted), 'excluded': len(excluded),
            'included_sha256': [p['sha256'] for p in accepted], 'excluded_sha256': [p['sha256'] for p in excluded]}


def check_config(project=ROOT):
    """Check current remote contracts and four existing monthly configurations."""
    import csv
    import remote_catalog

    project = Path(project).resolve()
    catalog = remote_catalog.load_catalog(project)
    if catalog.get('schema_version') != 3:
        raise ValueError('远程月报需要第三版实时执行目录')
    index = project / '报告模板/计划/运维月报/模板电站索引.csv'
    with index.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    station_ids = {row['电站编码'] for row in rows}
    if len(rows) != 4 or station_ids != {'SZ065', 'XNY080', 'XNY086', 'XNY108'}:
        raise ValueError('本版仅启用四个既有电站月报，新站须另行完成接入适配')
    checked, hashes = [], {}
    for row in rows:
        plan = build_plan('生成' + row['电站名称'] + '2026年8月运维月报',
                          row['电站编码'], '2026-08', row['模板编号'], project=project, catalog=catalog)
        count = len(plan['template_fixed_policy'].get('fields', {}))
        if count != 14:
            raise ValueError('已核验月报模板的14个固定位置范围变化')
        checked.append({'station_id': plan['station_id'], 'template_id': plan['template_id'],
                        'field_count': len(plan['fields']), 'template_fixed_field_count': count,
                        'matched_remote_field_count': sum(f['data_definition']['standard_id'] is not None for f in plan['fields']),
                        'executable_remote_field_count': sum(bool((f.get('remote_binding') or {}).get('executable')) for f in plan['fields'])})
        hashes.update(plan['configuration_sha256'])
    return {'status': 'configuration_checked', 'remote_dictionary': deepcopy(catalog['metadata']),
            'profiles': checked, 'configuration_sha256': hashes, 'powerplus_queries_executed': False,
            'report_generated': False, 'historical_fallback_used': False, 'business_approved': False}


def generate(request, out=None, profile='me', render=True, station_id=None, period=None, template_id=None, photo_review=None):
    """Fetch one live catalog, collect current evidence, fill, render, and persist."""
    import remote_catalog
    import remote_power
    from fixed_fields import validate_run_fixed_content

    monthly_scripts = ROOT / '报告模板/计划/运维月报/脚本'
    sys.path.insert(0, str(monthly_scripts))
    from fill_report import fill, sync_toc_pages
    from generate_report import render_report

    skill = ROOT.parents[1]
    destination = Path(out).expanduser().resolve() if out is not None else Path.home() / 'Documents/报告输出' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    if destination.is_relative_to(skill):
        raise ValueError('报告输出必须位于Skill目录外')
    destination.mkdir(parents=True, exist_ok=False)
    record = {'request': request, 'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'initializing',
              'formal_report_ready': False, 'historical_report_used': False, 'historical_fallback_used': False,
              'human_completion_required': True, 'executor': 'remote_monthly_std_v1'}
    record_path = destination / '运行记录.json'
    _write(record_path, record)
    try:
        catalog = remote_catalog.load_catalog(ROOT)
        plan = build_plan(request, station_id, period, template_id, catalog=catalog)
        plan['run_directory'] = str(destination)
        evidence = destination / '取数证据'; evidence.mkdir()
        plan['evidence_directory'] = str(evidence)
        record.update({k: plan[k] for k in ('station_id', 'station_name', 'period', 'template_id')})
        record.update(status='collecting', remote_dictionary=deepcopy(catalog['metadata']))
        _write(destination / '服务器字典快照.json', remote_catalog.fetch_snapshot())
        _write(destination / '执行目录证据.json', catalog)
        _write(destination / '报告执行计划.json', {k: v for k, v in plan.items() if k not in {'common_rules'}})
        raw = remote_power.collect(catalog, plan['bound_parameters']['powerplus_station_id'], plan['period'], evidence, profile=profile)
        _write(destination / '远程取数汇总.json', raw)
        collected = build_collected(plan, catalog, raw)
        review_path = None
        if photo_review is not None:
            supplied = Path(photo_review).expanduser().resolve()
            if supplied.is_relative_to(skill) or not supplied.is_file():
                raise ValueError('图片审核清单必须是Skill外的本次JSON文件')
            content = supplied.read_bytes()
            record['photo_review'] = apply_photo_review(plan, collected, json.loads(content))
            review_path = destination / '图片审核证据.json'; review_path.write_bytes(content)
            record['photo_review'].update(evidence_file=str(review_path), evidence_sha256=_sha(review_path))
        else:
            record['photo_review'] = {'status': 'pending_visual_review', 'candidate_count': len(collected['photos']['items'])}
        _write(destination / '取数结果.json', collected)
        if any(_sha(p) != sha for p, sha in plan['configuration_sha256'].items()):
            raise ValueError('本次查询期间模板或本站配置变化，不能混用配置生成报告')
        record['configuration_sha256'] = deepcopy(plan['configuration_sha256'])
        if review_path is not None:
            record['configuration_sha256'][str(review_path)] = _sha(review_path)
        record['template_fixed_fields'] = validate_run_fixed_content(record, collected)
        record['template_fixed_field_count'] = len(record['template_fixed_fields'])
        docx = destination / f"{plan['station_name']}_{plan['period']}_运维月报_待填版.docx"
        filled = fill(plan, collected, docx)
        record.update(status='generated', docx=str(docx), docx_sha256=_sha(docx), fill_result=filled,
                      field_status_counts=dict(Counter(v['status'] for v in collected['fields'].values())),
                      platform_snapshot_items=len(collected['platform_snapshot']['items']),
                      real_business_fields=filled['real_value_fields'], embedded_photos=filled['photos']['embedded'],
                      work_record_rows=0, dictionary_evidence_file=str(destination / '服务器字典快照.json'))
        lines = ['# 当次月报缺项说明', '', f"电站：{plan['station_name']}；期间：{plan['period']}。", '',
                 '已填值均来自本次请求、本站配置、已批准模板固定内容或当前STD契约；待填不表示零值或无事项。', '']
        for fid in filled['pending_fields']:
            cell = collected['fields'][fid]
            lines.append('- ' + cell['label'] + '：' + cell['reason'])
        pending_path = destination / '缺项说明.md'; pending_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        record['pending_source_guide'] = str(pending_path)
        if render:
            record['render'] = render_report(docx, destination / '预览')
            record['toc_page_update'] = sync_toc_pages(docx, Path(record['render']['pdf']))
            if record['toc_page_update'].get('updated'):
                shutil.rmtree(destination / '预览')
                record['render'] = render_report(docx, destination / '预览')
                if sync_toc_pages(docx, Path(record['render']['pdf'])).get('updated'):
                    raise ValueError('目录页码调整后分页仍变化，须继续版式检查')
            record['docx_sha256'] = _sha(docx)
            record['render']['page_sha256'] = {p: _sha(p) for p in record['render']['pages']}
            record['status'] = 'rendered_pending_visual_review'
        else:
            record['render'] = {'visual_review': 'not_rendered'}
        validate_run_fixed_content(record, collected)
        _write(record_path, record)
        return record
    except Exception as exc:
        record.update(status='failed', error=str(exc), error_type=type(exc).__name__)
        _write(record_path, record)
        raise
