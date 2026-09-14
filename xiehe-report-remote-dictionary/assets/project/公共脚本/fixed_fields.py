"""Historical equality is evidence; only a scoped business decision can fill a constant."""
from copy import deepcopy
from pathlib import Path
import datetime as dt
import hashlib
import json
import re

STATES = {'pending_confirmation', 'confirmed_fixed', 'confirmed_dynamic'}
SOURCE_KEYS = ('query_method', 'source_catalog_candidates', 'source_selection',
               'record_selection', 'photo_selection', 'calculation_rule',
               'ledger_total_rule', 'attachment_selection', 'plan_selection')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_decision(decision, common, fid, station_id=None):
    if not isinstance(decision, dict) or decision.get('status') not in STATES:
        raise ValueError('固定性确认状态无效：' + fid)
    if decision['status'] == 'pending_confirmation':
        if decision.get('value') is not None or decision.get('scope') is not None:
            raise ValueError('待二次确认不能配置生效范围或执行固定值：' + fid)
        return
    for key in ('confirmed_by', 'confirmed_at', 'reason', 'evidence'):
        if not isinstance(decision.get(key), str) or not decision[key].strip():
            raise ValueError('固定性结论缺少确认人、时间、依据或原因：' + fid)
    try:
        if dt.datetime.fromisoformat(decision['confirmed_at']).tzinfo is None:
            raise ValueError('确认时间缺少时区')
    except ValueError as exc:
        raise ValueError('固定性确认时间无效：' + fid) from exc
    scope = decision.get('scope') or {}
    ids = scope.get('station_ids')
    if (scope.get('template_id') != common['template_id']
            or scope.get('rules_version') != common['rules_version']
            or not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
            or any(not isinstance(s, str) or not re.fullmatch(r'[A-Z]+\d+', s) for s in ids)
            or (station_id and ids != [station_id])):
        raise ValueError('固定性确认必须绑定模板版本及明确电站，禁止通配继承：' + fid)
    for key in ('valid_from', 'valid_to'):
        if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', str(scope.get(key, ''))):
            raise ValueError('固定性确认须有明确起止月份：' + fid)
    if scope['valid_from'] > scope['valid_to']:
        raise ValueError('固定性生效期间倒置：' + fid)
    value = decision.get('value')
    if decision['status'] == 'confirmed_dynamic':
        if value is not None:
            raise ValueError('动态字段不能携带执行固定值：' + fid)
        return
    column = common['fields'][fid]['fixedness']['value_kind'] == 'record_column'
    values = value if column and isinstance(value, list) else [value] if not column else []
    if not values or any(not isinstance(v, str) for v in values):
        raise ValueError('固定值须为原文字符串，循环列须为非空字符串列表：' + fid)


def template_content(common):
    """Validate explicit template-position defaults, independently of public D semantics."""
    policy = common.get('template_fixed_content')
    if policy is None:
        return {}
    if not isinstance(policy, dict) or policy.get('schema_version') != 1:
        raise ValueError('模板固定内容结构无效')
    scope = policy.get('scope', {})
    if scope != {'kind': 'template', 'template_id': common['template_id'],
                 'rules_version': common['rules_version'],
                 'validity': 'until_template_rule_revision'}:
        raise ValueError('模板固定内容必须绑定本模板规则版本，持续至该规则修订')
    for key in ('confirmed_by', 'confirmed_at', 'reason', 'evidence'):
        if not isinstance(policy.get(key), str) or not policy[key].strip():
            raise ValueError('模板固定内容缺少确认人、时间、原因或依据')
    try:
        if dt.datetime.fromisoformat(policy['confirmed_at']).tzinfo is None:
            raise ValueError('确认时间缺少时区')
    except (TypeError, ValueError) as exc:
        raise ValueError('模板固定内容确认时间无效') from exc
    fields = policy.get('fields')
    if not isinstance(fields, dict) or not fields or not set(fields) <= set(common['fields']):
        raise ValueError('模板固定内容包含未知填写位置')
    for fid, value in fields.items():
        column = common['fields'][fid]['filling_rule'].get('group') is not None
        if isinstance(value, list) != column:
            raise ValueError('模板固定内容的单值或循环列类型错误：' + fid)
        values = value if column else [value]
        if not values or any(not isinstance(v, str) for v in values):
            raise ValueError('模板固定值须为字符串；循环列须为非空字符串列表：' + fid)
    for gid, group in common.get('filling', {}).get('repeat_groups', {}).items():
        fids = group['field_ids']
        selected = set(fids) & set(fields)
        if selected and (selected != set(fids) or len({len(fields[f]) for f in fids}) != 1):
            raise ValueError('模板固定循环内容须完整成组且列长度一致：' + gid)
    return deepcopy(policy)


def bind(common, profile, station_id, period, rule_path, catalog, output_template_id=None):
    candidates = {fid: f['fixedness'] for fid, f in common['fields'].items() if 'fixedness' in f}
    overrides = profile.get('fixedness_decisions', {})
    if not isinstance(overrides, dict) or not set(overrides) <= set(candidates):
        raise ValueError('本站固定性确认引用了未登记的候选字段')
    evidence_cache = {}
    result = {}
    for fid, candidate in candidates.items():
        evidence = candidate['historical_evidence']
        path = (Path(rule_path).parent / evidence['file']).resolve()
        if not path.is_relative_to(Path(rule_path).parent.resolve()) or not path.is_file():
            raise ValueError('固定性历史核验引用无效：' + fid)
        if path not in evidence_cache:
            evidence_cache[path] = (hashlib.sha256(path.read_bytes()).hexdigest(), read(path))
        if evidence_cache[path][0] != evidence['sha256']:
            raise ValueError('固定性历史证据摘要变化')
        verified = evidence_cache[path][1]['fields'][fid]
        expected_kind = 'record_column' if isinstance(verified['observed_value'], list) else 'scalar'
        if candidate.get('value_kind') != expected_kind:
            raise ValueError('固定候选的单值/循环列类型与证据不一致：' + fid)
        standard = catalog['standard_fields'][common['fields'][fid]['data_definition']['standard_id']]
        reference = standard.get('historical_source', {})
        contexts=reference.get('context_keys')
        if contexts is not None:
            binding=common['fields'][fid]['data_definition']
            context=binding.get('historical_context')
            if context not in contexts or binding.get('parameters',{}).get('collection')!=context:
                raise ValueError('历史同值证据不适用于本位置的集合：' + fid)
        if (not verified['exact_same'] or verified['report_count'] != 24
                or candidate['observed_value'] != verified['observed_value']
                or reference.get('observed_value') != candidate['observed_value']
                or reference.get('evidence_sha256') != evidence['sha256']
                or reference.get('status') != 'historical_equality_verified_fixedness_pending'):
            raise ValueError('字典、模板候选与24份原文证据不一致：' + fid)
        validate_decision(candidate['decision'], common, fid)
        decision = deepcopy(overrides.get(fid, candidate['decision']))
        if fid in overrides:
            validate_decision(decision, common, fid, station_id)
        scope = decision.get('scope') or {}
        applicable = (decision['status'] != 'pending_confirmation'
                      and (output_template_id is None or output_template_id == common['template_id'])
                      and station_id in scope.get('station_ids', [])
                      and scope['valid_from'] <= period <= scope['valid_to'])
        # Explicit source overrides must not be bypassed by a constant.
        override = profile.get('field_overrides', {}).get(fid, {})
        if period in override.get('effective_periods', []):
            applicable = False
        fixed = applicable and decision['status'] == 'confirmed_fixed'
        if fixed and fid == 'F002' and decision['value'] != period[:4]:
            raise ValueError('固定年份与本次请求冲突：F002')
        result[fid] = {'status': decision['status'] if applicable else 'pending_confirmation',
                       'configured_status': decision['status'], 'active': fixed,
                       'value_kind': candidate['value_kind'], 'decision': decision,
                       'question': candidate['question'], 'observed_value': candidate['observed_value'],
                       'reason': '已确认固定，按适用站点和期间填写。' if fixed else
                       '已确认动态，按原取数规则执行。' if applicable else
                       '固定性待二次确认或不在已确认范围内；不使用历史同值自动填写。'}
    policy = template_content(common)
    # A semantic source adapter is not permission to copy its template's static content.
    if policy and (output_template_id is None or output_template_id == common['template_id']):
        for fid, value in policy['fields'].items():
            override = profile.get('field_overrides', {}).get(fid, {})
            if period in override.get('effective_periods', []):
                raise ValueError('本站覆盖与模板固定内容冲突，请修订模板规则：' + fid)
            station_decision = overrides.get(fid, {})
            if station_decision.get('status') in {'confirmed_dynamic', 'confirmed_fixed'}:
                raise ValueError('本站固定性决定与模板固定内容重复或冲突，请删除本站决定：' + fid)
            decision = {k: deepcopy(policy[k]) for k in
                        ('scope', 'confirmed_by', 'confirmed_at', 'reason', 'evidence')}
            decision.update(status='confirmed_fixed', value=deepcopy(value))
            result[fid] = {'status': 'confirmed_fixed', 'configured_status': 'confirmed_fixed',
                'active': True, 'value_kind': 'record_column' if isinstance(value, list) else 'scalar',
                'decision': decision, 'scope_kind': 'template',
                'template_rules_sha256': hashlib.sha256(Path(rule_path).read_bytes()).hexdigest(),
                'question': None, 'observed_value': deepcopy(value),
                'reason': '按用户确认的模板固定内容填写；空字符串为有意留白，不查询该位置的当期来源。'}
    return result


def fresh_bindings(plan):
    """Re-read persisted decisions when validating output; collected status is not authority."""
    path = Path(plan['template_rules_file'])
    if plan.get('template_rules_sha256') and hashlib.sha256(path.read_bytes()).hexdigest() != plan['template_rules_sha256']:
        raise ValueError('模板规则摘要变化，请重新生成并验收')
    common = read(path)
    profile = read(plan['field_config'])['profiles'][plan['template_id']]
    output = plan.get('output_template')
    output_id = plan['template_id']
    if output:
        config_path = Path(output['station_config'])
        config = read(config_path)
        if (config.get('template_id') != output['template_id']
                or config.get('station_id') != plan['station_id']
                or (config_path.parent / config.get('source_adapter_config', '')).resolve()
                    != Path(plan['station_config']).resolve()):
            raise ValueError('输出模板的取数适配关联不一致')
        output_id = output['template_id']
    return bind(common, profile, plan['station_id'], plan['period'],
                plan['template_rules_file'], read(plan['source_catalog_file']), output_id)


def without_fixed_sources(plan, bindings):
    """Keep a dynamic query when another position needs it; drop only fixed bindings."""
    result = deepcopy(plan)
    for field in result['fields']:
        if bindings.get(field['field_id'], {}).get('active'):
            field['source_mode'] = 'manual'
            for key in SOURCE_KEYS:
                field.pop(key, None)
    if bindings.get('F090', {}).get('active'):
        result['photo_selection'] = None
    return result


def item(plan, field, binding, value, row_index=None):
    result = {'field_id': field['field_id'], 'label': field['label'], 'value': value,
            'unit': field['unit'], 'status': 'fixed', 'station_id': plan['station_id'],
            'period': plan['period'], 'reason': binding['reason'],
            'source': {'system': '已确认模板固定值', 'template_id': plan['template_id'],
                       'decision_sha256': digest(binding['decision']), 'row_index': row_index,
                       'confirmed_by': binding['decision']['confirmed_by'],
                       'evidence': binding['decision']['evidence']}}
    if binding.get('scope_kind') == 'template':
        result['source'].update(system='模板固定内容（用户确认）', scope_kind='template',
            rules_version=binding['decision']['scope']['rules_version'],
            confirmed_at=binding['decision']['confirmed_at'],
            template_rules_sha256=binding['template_rules_sha256'])
    return result


def apply_fixed(plan, collected):
    bindings = fresh_bindings(plan)
    definitions = {f['field_id']: f for f in plan['fields']}
    for fid, binding in bindings.items():
        field = collected['fields'][fid]
        field['fixedness_review'] = {k: deepcopy(binding[k]) for k in
                                    ['status', 'configured_status', 'active', 'reason', 'question']}
        if binding['active'] and binding['value_kind'] == 'scalar':
            collected['fields'][fid] = item(plan, definitions[fid], binding, binding['decision']['value'])
    for gid, group in plan['repeat_group_rules'].items():
        fids = group['field_ids']
        active = [fid for fid in fids if bindings.get(fid, {}).get('active')]
        if not active:
            continue
        if set(active) != set(fids):
            # A partially approved column must not fabricate a business record.
            for fid in active:
                collected['fields'][fid]['reason'] = '本列已确认固定；同组其他列未确认，尚不创建固定记录。'
            continue
        lengths = {len(bindings[fid]['decision']['value']) for fid in fids}
        if len(lengths) != 1:
            raise ValueError('固定循环列行数不一致：' + gid)
        payload = collected['repeat_groups'][gid]
        if payload['records']:
            raise ValueError('固定记录与本次业务记录冲突，不能覆盖或合并：' + gid)
        records = [{fid: item(plan, definitions[fid], bindings[fid],
                              bindings[fid]['decision']['value'][i], i) for fid in fids}
                   for i in range(lengths.pop())]
        payload.update(status='fixed', records=records, reason='同组字段均已确认固定；按模板条目展示。')


def validate_item(plan, field_id, value, binding, location, row_index):
    if not binding or not binding['active']:
        raise ValueError('未确认固定或超出适用范围，禁止标记已填：' + field_id)
    decision = binding['decision']
    expected = decision['value']
    if binding['value_kind'] == 'record_column':
        if location != 'record' or row_index is None or not 0 <= row_index < len(expected):
            raise ValueError('固定循环值位置错误：' + field_id)
        expected = expected[row_index]
    elif location != 'scalar':
        raise ValueError('固定单值位置错误：' + field_id)
    field = next(f for f in plan['fields'] if f['field_id'] == field_id)
    required = item(plan, field, binding, expected, row_index)
    if any(value.get(k) != required[k] for k in ['value', 'status', 'station_id', 'period', 'unit', 'source']):
        raise ValueError('固定填写值与已确认规则不一致：' + field_id)


def validate_run_fixed_content(record, collected):
    """Audit/review persisted rules and filled cells, never a caller's fixed flag alone."""
    policies = []
    for name, sha in record.get('configuration_sha256', {}).items():
        path = Path(name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('运行配置摘要变化，请重新生成并验收：' + name)
        if path.name == '通用取值规则.json':
            rules = read(path)
            if rules.get('template_id') == record.get('template_id') and rules.get('template_fixed_content'):
                policies.append((path, rules))
    template_marked = [fid for fid, cell in collected.get('fields', {}).items()
                       if cell.get('source', {}).get('scope_kind') == 'template']
    for group in collected.get('repeat_groups', {}).values():
        template_marked.extend(fid for row in group.get('records', []) for fid, cell in row.items()
                               if isinstance(cell, dict) and cell.get('source', {}).get('scope_kind') == 'template')
    if not policies:
        if template_marked:
            raise ValueError('模板固定标记缺少本模板持久规则及运行字节依据')
        return []
    if len(policies) != 1:
        raise ValueError('运行引用了多份本模板固定规则')
    path, common = policies[0]
    policy = template_content(common)
    if set(template_marked) - set(policy['fields']):
        raise ValueError('模板固定标记出现在未批准的位置')
    plan = {'template_id': common['template_id'], 'station_id': record['station_id'],
            'period': record['period'], 'fields': [{'field_id':fid, 'label':f['filling_rule']['label'],
            'unit':f['filling_rule']['unit']} for fid,f in common['fields'].items()]}
    for fid, value in policy['fields'].items():
        decision = {k: deepcopy(policy[k]) for k in ('scope','confirmed_by','confirmed_at','reason','evidence')}
        decision.update(status='confirmed_fixed', value=value)
        binding = {'active':True, 'scope_kind':'template', 'decision':decision,
                   'template_rules_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                   'value_kind':'record_column' if isinstance(value,list) else 'scalar', 'reason':''}
        if not isinstance(value,list):
            validate_item(plan,fid,collected.get('fields',{}).get(fid,{}),binding,'scalar',None)
        else:
            key = common['fields'][fid]['filling_rule']['group']
            group = collected.get('repeat_groups',{}).get(key,{})
            records = group.get('records',[])
            fids = set(common['filling']['repeat_groups'][key]['field_ids'])
            if group.get('status') != 'fixed' or len(records) != len(value) or any(set(row) != fids for row in records):
                raise ValueError('运行中的模板固定循环表不完整：'+key)
            for i,row in enumerate(records):
                validate_item(plan,fid,row[fid],binding,'record',i)
    return sorted(policy['fields'])
