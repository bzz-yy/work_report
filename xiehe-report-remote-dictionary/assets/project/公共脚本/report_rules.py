"""Load template-owned rules, then bind station parameters and scoped exceptions."""
from copy import deepcopy
from pathlib import Path
import json
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '数据字典/脚本'))
from catalog_schema import validate_source_catalog, validate_conversion
from remote_catalog import load_catalog as load_source_catalog, catalog_path
from station_config import load_station_config
from report_catalog import project_root
from value_rules import validate_selection
from source_guides import load_guide
from field_models import validate_field_models,digest as model_digest
import hashlib


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def linked(root, base, relative):
    path = (Path(base) / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('报告规则引用无效：' + str(path))
    return path


def apply_override(field, override, period):
    if not override:
        return field
    required = {'values', 'effective_periods', 'confirmed_by', 'reason'}
    if set(override) != required or not override['confirmed_by'] or not override['reason']:
        raise ValueError('本站取数覆盖须包含取值、适用期间、确认人和原因')
    periods = override['effective_periods']
    if not isinstance(periods, list) or not periods or not all(isinstance(p,str) for p in periods):
        raise ValueError('本站取数覆盖适用期间必须是明确期间列表')
    values = override['values']
    if not isinstance(values, dict) or not values or not set(values) <= {'source_mode', 'dictionary_ref'}:
        raise ValueError('本站取数覆盖仅支持来源方式和字典引用，其他差异须扩展执行器')
    if values.get('source_mode', 'local_history_replay') not in {'request','station_profile','local_history_replay','manual','catalog','record_collection','pending_business_rule','independent_photos','attachment','plan_records','derived'}:
        raise ValueError('不支持的本站来源方式')
    if period in periods:
        field.update(deepcopy(values))
        if values.get('source_mode') in {'manual','pending_business_rule','local_history_replay'}:
            for key in ['calculation_rule','ledger_total_rule','attachment_selection','plan_selection']:
                field.pop(key,None)
        if 'dictionary_ref' in values:
            # A changed source does not inherit the previous source's live query.
            field.pop('query_method',None)
            field.pop('source_catalog_candidates',None)
            field.pop('report_source_assessments',None)
            field.pop('source_selection',None)
            field.pop('record_selection',None)
            field.pop('photo_selection',None)
            for key in ['calculation_rule','ledger_total_rule','attachment_selection','plan_selection']:
                field.pop(key,None)
    return field


def validate_report_assessments(rule, catalog):
    """Report applicability stays on the template and is never source approval."""
    assessments = rule.get('report_source_assessments', [])
    if not isinstance(assessments, list):
        raise ValueError('报告来源适用性判断必须为列表')
    for assessment in assessments:
        standard_id = assessment.get('standard_id')
        if standard_id not in catalog['standard_fields']:
            raise ValueError('报告适用性判断引用了不存在的统一数据项')
        mapping_ids = assessment.get('mapping_ids')
        if not isinstance(mapping_ids, list) or not mapping_ids:
            raise ValueError('报告适用性判断必须明确来源映射')
        for mapping_id in mapping_ids:
            mapping = catalog['source_mappings'].get(mapping_id)
            if mapping is None or mapping['standard_id'] != standard_id:
                raise ValueError('报告适用性判断的来源映射与统一数据项不一致')
        use = assessment.get('use')
        if use not in {'value_candidate', 'identity_check_only'}:
            raise ValueError('报告适用性判断使用方式未被支持')
        if use == 'value_candidate' and not set(mapping_ids) <= set(rule.get('source_mapping_candidates', [])):
            raise ValueError('报告取值判断未在本字段来源候选中登记')
        if assessment.get('status') not in {'pending_business_confirmation','validated_against_history'}:
            raise ValueError('报告来源适用状态未支持；采用须经历史对账验证')
        if assessment.get('period_binding') not in {'$request.period', '$record.month'}:
            raise ValueError('报告适用性判断的期间绑定未被支持')
        display = assessment.get('display', {})
        if display.get('source_unit') != catalog['standard_fields'][standard_id].get('unit'):
            raise ValueError('报告展示换算的输入单位与统一数据项不一致')
        if display.get('to_unit') != rule['filling_rule'].get('unit'):
            raise ValueError('报告展示换算的目标单位与模板字段不一致')
        if display.get('operation') not in {'identity', 'multiply', 'pending'}:
            raise ValueError('报告展示换算方式未被支持')
        if display.get('status') != 'conditional_on_source_approval':
            raise ValueError('报告展示换算须保留来源口径确认条件')
        factor = display.get('factor')
        if display['operation'] == 'multiply' and (
                isinstance(factor, bool) or not isinstance(factor, (int, float)) or
                not math.isfinite(factor) or factor <= 0):
            raise ValueError('报告展示换算系数必须为正数')
        if display['operation'] == 'identity' and (
                display['source_unit'] != display['to_unit'] or isinstance(factor, bool) or factor != 1):
            raise ValueError('原值展示要求输入输出单位一致且系数为1')
        if display['operation'] == 'pending' and factor is not None:
            raise ValueError('待确认的报告换算不能写入系数')
        validate_conversion(display, display['source_unit'], display['to_unit'], rule['field_id'])
        expected_period = '$record.month' if rule['filling_rule'].get('group') == 'records.1' else '$request.period'
        if assessment['period_binding'] != expected_period:
            raise ValueError('报告字段的期间绑定与单值或逐月台账不一致')
    candidates = rule.get('source_mapping_candidates', [])
    assessed = [mid for a in assessments if a['use'] == 'value_candidate' for mid in a['mapping_ids']]
    if len(candidates) != len(set(candidates)) or len(assessed) != len(set(assessed)) or set(assessed) != set(candidates):
        raise ValueError('报告来源候选须逐条且唯一登记适用判断')
    return assessments


def load_bundle(root, config_path, mapping_path, template_id, period, output_template_id=None):
    root, config_path, mapping_path = map(Path, (root, config_path, mapping_path))
    project = project_root(root)
    cfg = load_station_config(config_path, read)
    fcpath = linked(root, config_path.parent, cfg['field_configuration'])
    fc = read(fcpath)
    station_rule_path = linked(root, config_path.parent, cfg['maintenance_rules'])
    notes = read(station_rule_path)
    if fc.get('schema_version') != 2 or notes.get('schema_version') != 2:
        raise ValueError('请使用模板通用规则的第二版配置，旧配置只作历史证据')
    if fc['station_id'] != cfg['station_id'] or notes['station_id'] != cfg['station_id']:
        raise ValueError('本站规则参数串站')
    profile = fc['profiles'][template_id]
    rule_path = linked(root, fcpath.parent, profile['template_rules'])
    mapping = read(mapping_path)
    linked_paths = [linked(root, mapping_path.parent, mapping['retrieval_rules']),
                    linked(root, config_path.parent, cfg['template_rules'][template_id]),
                    linked(root, station_rule_path.parent, notes['template_rules'][template_id])]
    if any(p != rule_path for p in linked_paths):
        raise ValueError('模板、本站和维护说明引用了不同的通用规则')
    common = read(rule_path)
    if common.get('source_catalog') != 'remote:dictionary_readonly':
        raise ValueError('公共数据字典必须引用 remote:dictionary_readonly，禁止使用本地字典或旧值回退')
    catalog_data = validate_source_catalog(load_source_catalog(project))
    catalog_binding = {'path': str(catalog_path(project)), 'data': catalog_data}
    from catalog_schema import validate_definition_binding
    for fid, field in common['fields'].items():
        validate_definition_binding(catalog_data, field.get('data_definition', {}), template_id + '/' + fid)
    if common['template_id'] != template_id or common['dictionary_id'] != fc['dictionary_id']:
        raise ValueError('通用规则串模板或字典编号不一致')
    model_validation_file=validate_field_models(common,catalog_binding['data'] if catalog_binding else {},project,rule_path)
    if any(f.get('calculation_rule') or f.get('ledger_total_rule') for f in common['fields'].values()):
        from derived_fields import validate_calculation_rules
        validate_calculation_rules(common,{**profile,'station_id':cfg['station_id']},project)
    if linked(root, rule_path.parent, common['template_mapping']) != mapping_path:
        raise ValueError('通用规则没有关联所选模板映射')
    ids = [f['id'] for f in mapping['fields']] if 'fields' in mapping else mapping['field_ids']
    if set(ids) != set(common['fields']):
        raise ValueError('模板通用规则字段覆盖不完整')
    inspection = cfg.get('report_type') == '定检报告'
    station_profile = cfg['profiles'][template_id] if inspection else None
    active = [fid for fid in ids if not inspection or station_profile['include_tickets'] or fid not in ['J040','J041','J042']]
    if not set(profile['field_notes']) <= set(active) or not set(profile['field_overrides']) <= set(active):
        raise ValueError('本站备注或覆盖包含未启用字段')
    expected_params = {'station_id': '$station.station_id', 'powerplus_station_id': '$station.powerplus_station_id', 'period': '$request.period'}
    if fc['parameters'] != expected_params or common['parameter_bindings'] != expected_params:
        raise ValueError('参数绑定不支持或包含写死的电站及期间')
    expanded = []
    from fixed_fields import bind as bind_fixedness
    fixedness = bind_fixedness(common, profile, cfg['station_id'], period, rule_path,
                              catalog_binding['data'], output_template_id) if catalog_binding else {}
    for fid in active:
        rule = common['fields'][fid]
        if rule.get('field_id') != fid:
            raise ValueError('模板字段编号与规则键不一致：' + fid)
        if not set(notes['fields'].get(fid, {})) <= {'observation', 'evidence', 'business_confirmation'}:
            raise ValueError('本站观察不能覆盖模板字段、单位或填写规则：' + fid)
        field = {k: deepcopy(v) for k,v in rule.items() if k in ['field_id','dictionary_ref','source_mode','query_method','source_selection','record_selection','photo_selection','field_model','calculation_rule','ledger_total_rule','attachment_selection','plan_selection']}
        if rule.get('report_source_assessments') and not catalog_binding:
            raise ValueError('报告来源适用性判断缺少公共数据字典引用')
        if catalog_binding:
            candidates=rule.get('source_mapping_candidates',[])
            if not set(candidates)<=set(catalog_binding['data']['source_mappings']):
                raise ValueError('模板引用了新字典中不存在的来源映射')
            if candidates:
                field['source_catalog_candidates']={
                    'catalog_file':catalog_binding['path'],
                    'mapping_ids':deepcopy(candidates),
                    'standard_ids':list(dict.fromkeys(catalog_binding['data']['source_mappings'][mid]['standard_id'] for mid in candidates)),
                    'status':'candidate_not_automatic_source_selection'}
            assessments = validate_report_assessments(rule, catalog_binding['data'])
            if assessments:
                field['report_source_assessments'] = deepcopy(assessments)
            if rule.get('source_selection'):
                selection=rule['source_selection']
                validate_selection(selection,rule,catalog_binding['data'])
                validation_path=linked(root,rule_path.parent,selection['validation_ref'])
                if hashlib.sha256(validation_path.read_bytes()).hexdigest()!=selection['validation_sha256']:
                    raise ValueError('报告来源采用核验摘要变化')
                validation=read(validation_path)
                if validation['template_id']!=template_id or validation['selected_fields'].get(fid)!=selection['mapping_id']:
                    raise ValueError('核验记录与当前字段选源不一致')
                field['source_selection']['validation_file']=str(validation_path)
            if rule.get('source_mode')=='catalog' and not rule.get('source_selection'):
                raise ValueError('字典取数字段未选定可执行来源')
            if rule.get('attachment_selection'):
                from value_rules import transform
                selection=rule['attachment_selection']
                if set(selection)!={'mapping_id','metric','display'} or fid not in {'F004','F005','F006','F007','F054','F058'}:
                    raise ValueError('结算附件字段选择未支持')
                mid=selection['mapping_id'];source_mapping=catalog_binding['data']['source_mappings'].get(mid,{})
                if (mid not in rule.get('source_mapping_candidates',[])
                        or source_mapping.get('query_method_id')!='power.electricitybill.attachment_meter.v1'
                        or source_mapping.get('response_field')!=selection['metric']
                        or source_mapping.get('technical_status')!='verified_source_values'):
                    raise ValueError('结算附件选源与已核验字典不一致')
                standard=catalog_binding['data']['standard_fields'][source_mapping['standard_id']]
                if selection['display'].get('source_unit')!='kWh' or selection['display'].get('to_unit')!=rule['filling_rule']['unit']:
                    raise ValueError('结算附件显示单位与报告字段不一致')
                transform('1',source_mapping,standard,selection['display'])
            if rule.get('source_mode')=='attachment' and not rule.get('attachment_selection'):
                raise ValueError('附件字段未配置有效取数方法')
            if rule.get('plan_selection'):
                policy=rule['plan_selection']
                if (fid!='F060' or rule['source_mode']!='record_collection'
                        or set(policy)!={'method_id','mapping_id','formatter','categories','plan_types','sequence_field'}
                        or policy['method_id']!='power.plan.triggers.v1' or policy['mapping_id']!='SM025'
                        or policy['formatter']!='issued_plan_text' or policy['sequence_field']!='F059'
                        or policy['categories']!=['安全管理'] or policy['plan_types']!=['其他计划']):
                    raise ValueError('当月计划字段采用范围未支持')
            if rule.get('photo_selection'):
                policy=rule['photo_selection']
                if (fid!='F090' or set(policy)!={'mapping_ids','site_categories','period_policy','deduplicate'}
                        or not policy['mapping_ids'] or not set(policy['mapping_ids'])<=set(catalog_binding['data']['source_mappings'])
                        or not set(policy['mapping_ids'])<={'SM016','SM020'}
                        or policy['deduplicate']!='sha256'):
                    raise ValueError('图片来源选择不支持或未登记')
                for mid in policy['mapping_ids']:
                    mapping=catalog_binding['data']['source_mappings'][mid]
                    if catalog_binding['data']['standard_fields'][mapping['standard_id']]['role']!='asset_collection':
                        raise ValueError('图片来源必须关联图片集合数据项')
            if rule.get('record_selection'):
                policy=rule['record_selection']
                if (fid not in {'F065','F067'} or policy.get('method_id')!='power.other_work.executions.v1'
                        or policy.get('formatter')!=('category_only' if fid=='F065' else 'category_text_time') or policy.get('sequence_field')!='F064'
                        or set(policy.get('mapping_ids',{}))!={'text','time','category'}):
                    raise ValueError('执行记录填写规则未支持')
                expected={'text':'process_table_tbl_instructions','time':'process_table_st_time','category':'tbl_category'}
                for key,mid in policy['mapping_ids'].items():
                    mapping=catalog_binding['data']['source_mappings'].get(mid,{})
                    if mapping.get('query_method_id')!=policy['method_id'] or mapping.get('response_field')!=expected[key]:
                        raise ValueError('执行记录变量与字典来源不一致')
        # Dictionary paths are relative to the template, then normalized for legacy consumers.
        rel, pointer = field['dictionary_ref'].split('#',1)
        dictionary_path = linked(root,rule_path.parent,rel)
        definition = read(dictionary_path)['fields'].get(fid)
        if pointer != '/fields/' + fid or not definition:
            raise ValueError('报告填写定义的字段引用错位：' + fid)
        if rule['filling_rule'].get('unit') != definition.get('unit') or rule['filling_rule'].get('label') != definition.get('label'):
            raise ValueError('模板填写单位或名称与本报告字段定义不一致：' + fid)
        import os
        field['dictionary_ref'] = os.path.relpath(dictionary_path,fcpath.parent) + '#' + pointer
        field['station_id'] = cfg['station_id']
        field['period'] = '$request.period'
        field['maintenance_rule_ref'] = cfg['maintenance_rules'] + '#/fields/' + fid
        field_note=profile['field_notes'].get(fid,{})
        if not set(field_note)<={'business_confirmation','observation_2026_02_to_07','question'}:
            raise ValueError('字段备注不能覆盖站点、期间或通用绑定；请使用明确的取数覆盖项')
        field.update(deepcopy(field_note))
        if fid in fixedness:
            field['fixedness'] = deepcopy(fixedness[fid])
        if inspection:
            field['module_scope'] = deepcopy(station_profile['equipment_modules']) if rule['module_scope_parameter'] else None
            field['source_override'] = None
        else:
            field['local_source_index'] = fc['local_source_index']
            field['station_override'] = None
        expanded.append(apply_override(field,profile['field_overrides'].get(fid),period))
    effective = deepcopy(notes)
    effective['fields'] = {
        fid: {**deepcopy(common['fields'][fid]['filling_rule']), **deepcopy(notes['fields'].get(fid,{}))}
        for fid in ids}
    # Retain inactive station evidence; it does not activate template fields.
    for fid,value in notes['fields'].items():
        if fid in effective['fields']:
            continue
        other_rules = []
        for other_id,ref in notes['template_rules'].items():
            other = read(linked(root,station_rule_path.parent,ref))
            if other['template_id'] != other_id:
                raise ValueError('本站其他版本的维护规则关联错误')
            if fid in other['fields']:
                other_rules.append(other['fields'][fid]['filling_rule'])
        if other_rules and not all(x == other_rules[0] for x in other_rules):
            raise ValueError('未启用字段的多版本规则冲突')
        effective['fields'][fid] = {**deepcopy(other_rules[0] if other_rules else {}),**deepcopy(value)}
    for key,value in common['filling'].items():
        if key == 'repeat_groups':
            effective[key] = {k:{**deepcopy(v),**deepcopy(notes.get(key,{}).get(k,{}))} for k,v in value.items()}
        elif isinstance(value,dict):
            effective[key] = {**deepcopy(value), **deepcopy(notes.get(key,{}))}
        else:
            effective[key] = deepcopy(notes.get(key,value))
    result = deepcopy(fc)
    result['fields'] = expanded
    result['profiles'][template_id]['fields'] = expanded
    query_methods = {}
    for alias, binding in common.get('query_methods', {}).items():
        if not catalog_binding or set(binding) != {'catalog_method_id', 'use', 'empty_policy'}:
            raise ValueError('模板只引用字典取数方法，不能重复维护接口命令')
        mid = binding['catalog_method_id']
        if mid not in catalog_binding['data']['query_methods'] or binding['use'] != 'probe_only' or binding['empty_policy'] != 'missing_not_zero':
            raise ValueError('模板取数方法绑定未被支持')
        method = deepcopy(catalog_binding['data']['query_methods'][mid])
        replacements = {'${profile}': '$session.profile', '${station_code}': '$station.powerplus_station_id',
                        '${period}': '$request.period', '${period_start}': '$request.period_start', '${period_end}': '$request.period_end'}
        method['command'] = [replacements.get(v, v) for v in method['command']]
        query_methods[alias] = {**method, **binding}
    for field in expanded:
        if field.get('query_method') and field['query_method'] not in query_methods:
            raise ValueError('字段绑定了不存在的取数方法：' + field['field_id'])
    controls=profile.get('source_controls',{})
    for mid,control in controls.items():
        if not catalog_binding or mid not in catalog_binding['data']['source_mappings'] or set(control)!={'blocked_periods','confirmed_zeros'}:
            raise ValueError('本站来源控制项不支持')
        for item in control['blocked_periods']+control['confirmed_zeros']:
            import re
            if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',item.get('period','')):
                raise ValueError('本站来源控制须限定明确年月')
            if not item.get('reason') and not item.get('source_record_id'):
                raise ValueError('本站来源控制缺少原因或来源记录标识')
    source_guide=None
    if profile.get('source_guide'):
        source_guide=load_guide(linked(root,fcpath.parent,profile['source_guide']),cfg['station_id'],cfg['powerplus_station_id'],period,active,
            template_id=template_id,catalog_guides=catalog_binding['data'].get('lookup_guides',{}) if catalog_binding else {})
    if model_validation_file:
        source_guide=source_guide or {'path':str(rule_path),'reviews':{},'online_files':{},'catalog_guides':deepcopy(catalog_binding['data'].get('lookup_guides',{}))}
        source_guide['general_models']={fid:deepcopy(common['fields'][fid]['field_model']) for fid in active}
    meter_policy_file=None
    if profile.get('meter_policy_ref'):
        meter_policy_file=str(linked(root,fcpath.parent,profile['meter_policy_ref']))
        from bill_meter_attachment import load_meter_policy
        meter_policy=load_meter_policy(meter_policy_file,cfg['powerplus_station_id'])
        if not model_validation_file or read(model_validation_file).get('source_policy_hashes',{}).get(cfg['station_id'],{}).get('meter_policy')!=hashlib.sha256(Path(meter_policy_file).read_bytes()).hexdigest():
            raise ValueError('电站计量附件参数超出独立核验版本')
    plan_source_policy=deepcopy(profile.get('plan_source_policy',{}))
    if plan_source_policy:
        if (set(plan_source_policy)!={'enabled','periods','method_id','next_month'} or not isinstance(plan_source_policy['enabled'],bool)
                or plan_source_policy['method_id']!='power.plan.triggers.v1' or plan_source_policy['next_month']!='candidate_only'
                or not model_validation_file
                or read(model_validation_file).get('source_policy_hashes',{}).get(cfg['station_id'],{}).get('plan_source_policy')!=model_digest(plan_source_policy)):
            raise ValueError('电站计划采用政策超出独立核验范围')
    calculation_adoptions={fid:{slot:deepcopy(value) for slot,value in slots.items()
        if next(f for f in expanded if f['field_id']==fid).get(slot)}
        for fid,slots in profile.get('calculation_adoptions',{}).items()}
    calculation_adoptions={fid:slots for fid,slots in calculation_adoptions.items() if slots}
    return {'field_config': result, 'maintenance': effective, 'template_rules': common,
            'field_model_validation_file':model_validation_file,'meter_policy_file':meter_policy_file,
            'plan_source_policy':plan_source_policy,'calculation_adoptions':calculation_adoptions,
            'source_guide':source_guide,
            'source_controls': deepcopy(controls), 'station_base_file': cfg['station_base_file'],
            'query_methods': query_methods, 'source_catalog_file': catalog_binding['path'] if catalog_binding else None,
            'template_rules_file': str(rule_path), 'station_notes_file': str(station_rule_path),
            'bound_parameters': {'station_id':cfg['station_id'], 'powerplus_station_id':cfg.get('powerplus_station_id'), 'period':period}}
