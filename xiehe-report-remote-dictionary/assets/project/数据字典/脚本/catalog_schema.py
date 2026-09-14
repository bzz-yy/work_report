"""数据定义、来源、接口契约和单位校验；不读取报告模板或运行数值。"""
import math
import json
import re


def validate_conversion(conversion, source_unit, target_unit, label):
    if not isinstance(conversion, dict):
        raise ValueError(f'缺少结构化单位换算：{label}')
    op, factor = conversion.get('operation'), conversion.get('factor')
    if source_unit is None or target_unit is None:
        if op != 'pending' or factor is not None:
            raise ValueError(f'单位未确认时不得换算：{label}')
        return
    if op == 'pending':
        if factor is not None:
            raise ValueError(f'待确认换算不能配置系数：{label}')
        return
    ratios = {('kWh', '万kWh'): 0.0001, ('万kWh', 'kWh'): 10000,
              ('kW', 'MW'): 0.001, ('MW', 'kW'): 1000}
    expected = 1 if source_unit == target_unit else ratios.get((source_unit, target_unit))
    if (expected is None or isinstance(factor, bool) or not isinstance(factor, (int, float))
            or not math.isfinite(factor) or factor != expected
            or op != ('identity' if source_unit == target_unit else 'multiply')):
        raise ValueError(f'单位换算方向或系数错误，或口径不支持：{label} {source_unit} → {target_unit}')


def validate_method_contracts(catalog):
    """Check the executable adapter's supported contract before any query.

    These are adapter capabilities, not a second dictionary. A changed API needs
    an adapter change plus new evidence; editing JSON alone cannot enable it.
    """
    methods = catalog['query_methods']
    expected = {
        'power.station.archive': ('powerplus_archive', 'GET',
            '/api/v4/base/station/stationDetail/{station_code}',
            ['power', 'station', 'get', '--profile', '${profile}', '--station-code', '${station_code}']),
        'power.pv.report': ('powerplus_monitor', 'POST', '/api/v4/report/station/sun/list',
            ['power', 'report', 'pv', 'list', '--profile', '${profile}', '--station-code', '${station_code}',
             '--date-type', '2', '--start-time', '${period}', '--end-time', '${period}', '--size', '100']),
    }
    for method_id, method in methods.items():
        if method.get('source_id') not in catalog['sources']:
            raise ValueError('取数方法引用了不存在的数据来源：' + method_id)
        if method.get('base_url') != 'https://power-xhyw.cnecloud.com':
            raise ValueError('取数方法来源域名与已支持接口契约不一致：' + method_id)
        if method_id in expected:
            source, verb, path, command = expected[method_id]
            if (method.get('source_id'), method.get('method'), method.get('path'), method.get('command')) != (source, verb, path, command):
                raise ValueError('取数方法命令或接口与已支持只读契约不一致：' + method_id)
        elif method_id == 'power.electricitybill.settlement.v1':
            if method['source_id'] != 'powerplus_bill':
                raise ValueError('结算方法的来源绑定错误')
        elif method_id == 'power.other_work.executions.v1':
            if (method['source_id']!='powerplus_other_work' or method.get('adapter_script')!='脚本/query_other_work.py'
                    or [(s['method'],s['path']) for s in method['steps']] != [('POST','/api/blade-form/form/data/list'),('GET','/api/blade-workflow/process/detail')]
                    or method['steps'][0]['body']['formKey']!='OtherWorkOrder'):
                raise ValueError('其他工单只读契约不一致')
        elif method_id == 'power.electricitybill.attachment_meter.v1':
            if (method.get('source_id')!='powerplus_bill_attachments'
                    or method.get('base_query_method_id')!='power.electricitybill.settlement.v1'
                    or method.get('adapter_script')!='脚本/query_power_bill.py'
                    or method.get('attachment_field')!='at_messages_photos'
                    or method.get('decoder')!='bill_meter_attachment.parse_bill_meter_attachment'):
                raise ValueError('结算附件复算契约变化，须重新核验')
        elif method_id == 'power.plan.triggers.v1':
            import sys
            from pathlib import Path
            sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'公共脚本'))
            from plan_values import validate_method
            if method.get('source_id')!='powerplus_plan_triggers' or method.get('adapter_script')!='脚本/query_plan_triggers.py':
                raise ValueError('计划下发来源或适配器绑定错误')
            validate_method(method)
        else:
            raise ValueError('未实现的取数方法；先实现并验证适配器：' + method_id)
    if methods['power.pv.report']['parameters'] != {
        'stationCodes': ['${station_code}'], 'dateType': '2',
        'startTime': '${period}', 'endTime': '${period}', 'timeZone': 8}:
        raise ValueError('光伏报表参数必须绑定本站、当月和北京时间')
    if methods['power.station.archive']['response_root'] != 'data.record' or methods['power.pv.report']['response_root'] != 'data.records':
        raise ValueError('CLI返回结构与已支持契约不一致')

def validate_definition_binding(catalog, binding, label):
    """A shared definition must keep each template slot's semantic dimensions."""
    standard=catalog['standard_fields'].get(binding.get('standard_id'))
    if standard is None:
        raise ValueError('模板字段未关联公共定义：' + label)
    specification=standard.get('definition_parameters',{})
    parameters=binding.get('parameters',{})
    if not isinstance(parameters,dict) or set(parameters)!=set(specification):
        raise ValueError('共用定义缺少或混入维度参数：' + label)
    for key, rule in specification.items():
        value=parameters.get(key)
        if not isinstance(value,str) or not value or (rule.get('allowed_values') and value not in rule['allowed_values']):
            raise ValueError('共用定义维度参数无效：' + label + '/' + key)
    context=binding.get('historical_context')
    if context is not None:
        contexts=standard.get('historical_source',{}).get('context_keys',[])
        if context not in contexts or parameters.get('collection')!=context:
            raise ValueError('历史同值证据与本位置集合不一致：' + label)
    if parameters.get('collection') in {'before_data','after_data'}:
        if parameters.get('phase') not in {'before','after'} or parameters['phase']+'_data'!=parameters['collection']:
            raise ValueError('清洗前后阶段与记录集合不一致：' + label)


def validate_source_catalog(catalog):
    """Validate source definitions independently of any report or template."""
    if catalog.get('schema_version') != 2:
        raise ValueError('公共数据字典须为职责分离后的第二版结构')
    report_keys = {
        'report_field_candidates', 'report_field_id', 'report_targets',
        'report_mapping', 'report_source_assessments', 'report_field_coverage',
        'template_candidate', 'template_id', 'template_mapping', 'business_status',
    }

    def check_boundary(value, location):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in report_keys:
                    raise ValueError(f'公共数据字典不能保存报告绑定或混用业务状态：{location}/{key}')
                if key.startswith(('legacy_', 'colleague_')):
                    raise ValueError(f'项目数据字典不能重新依赖原同事字典元数据：{location}/{key}')
                check_boundary(child, f'{location}/{key}')
        elif isinstance(value, list):
            for index, child in enumerate(value):
                check_boundary(child, f'{location}/{index}')
        elif isinstance(value, str) and '同事字典快照-' in value:
            raise ValueError(f'项目数据字典不能重新引用原同事字典文件：{location}')

    check_boundary(catalog, '')
    for section in ('standard_fields', 'sources', 'source_mappings', 'query_methods'):
        if not isinstance(catalog.get(section), dict):
            raise ValueError(f'公共数据字典缺少有效的 {section}')
    standards, sources = catalog['standard_fields'], catalog['sources']
    methods = catalog['query_methods']
    if not standards or not catalog['source_mappings']:
        raise ValueError('项目字典不能没有数据项或来源映射')
    validate_method_contracts(catalog)
    for gid,guide in catalog.get('lookup_guides',{}).items():
        if not guide.get('source_name') or (guide.get('query_method_ref') and guide['query_method_ref'] not in methods):
            raise ValueError('字典查找指南缺少来源名或引用未知取数方法：'+gid)
    keys = set()
    for standard_id, standard in standards.items():
        if standard.get('standard_id') != standard_id or not standard.get('definition_status'):
            raise ValueError(f'统一数据项编号或定义状态无效：{standard_id}')
        if not set(standard.get('related_fields', [])) <= set(standards):
            raise ValueError(f'统一数据项引用了不存在的相关数据项：{standard_id}')
        if not re.fullmatch(r'D\d{3}', standard_id) or not standard.get('key') or standard['key'] in keys:
            raise ValueError('统一数据项编号无效或机器键重复：' + standard_id)
        keys.add(standard['key'])
        parameters = standard.get('definition_parameters', {})
        if not isinstance(parameters, dict):
            raise ValueError('定义维度必须是对象：' + standard_id)
        for parameter, specification in parameters.items():
            if (not isinstance(parameter,str) or not parameter
                    or not isinstance(specification,dict)
                    or specification.get('required') is not True):
                raise ValueError('定义维度缺少明确约束：' + standard_id)
            values = specification.get('allowed_values')
            if values is not None and (not isinstance(values,list) or not values
                    or any(not isinstance(x,str) or not x for x in values)
                    or len(values)!=len(set(values))):
                raise ValueError('定义维度可选值无效：' + standard_id)
        history = standard.get('historical_source')
        if history is not None:
            if (history.get('kind') != 'historical_report_reference'
                    or history.get('status') != 'historical_equality_verified_fixedness_pending'
                    or history.get('sample_count') != 24 or history.get('exact_unique_count') != 1
                    or history.get('automatic_use') is not False
                    or history.get('observed_value') is None
                    or not history.get('evidence_file')
                    or not re.fullmatch(r'[a-f0-9]{64}', history.get('evidence_sha256', ''))):
                raise ValueError('历史同值来源不能冒充自动取值批准：' + standard_id)
            contexts=history.get('context_keys')
            if contexts is not None:
                allowed=parameters.get('collection',{}).get('allowed_values',[])
                if (not isinstance(contexts,list) or not contexts
                        or any(not isinstance(x,str) or x not in allowed for x in contexts)
                        or len(contexts)!=len(set(contexts)) or not history.get('context_note')):
                    raise ValueError('历史同值的集合范围无效：' + standard_id)
        for key in ('name', 'definition', 'grain', 'period_semantics'):
            if not isinstance(standard.get(key), str) or not standard[key].strip():
                raise ValueError(f'数据项缺少名称、含义、范围或期间：{standard_id}/{key}')
        basis = standard.get('name_basis', {})
        names = basis.get('mapping_ids', [])
        if basis.get('status') == 'business_definition_source_pending':
            if names or basis.get('preferred_mapping_id') is not None:
                raise ValueError('未核来源的定义不能伪造名称来源：' + standard_id)
            pending = standard.get('retrieval', {})
            if (pending != {'source_id': None, 'query_method_id': None, 'endpoint': None,
                           'response_field': None, 'parameters': None,
                           'status': 'pending_verification'}
                    or not standard.get('pending_questions')
                    or any(m.get('standard_id') == standard_id for m in catalog['source_mappings'].values())):
                raise ValueError('待核数据项必须保持空来源与明确待核问题：' + standard_id)
            continue
        if not names or basis.get('preferred_mapping_id') not in names:
            raise ValueError('名称依据缺少有效来源映射：' + standard_id)
        for mapping_id in names:
            mapping = catalog['source_mappings'].get(mapping_id, {})
            if mapping.get('standard_id') != standard_id:
                raise ValueError('名称依据与来源映射的数据项不一致：' + standard_id)
    unique_mappings = set()
    for mapping_id, mapping in catalog['source_mappings'].items():
        if mapping.get('mapping_id') != mapping_id or not mapping.get('semantic_status'):
            raise ValueError(f'来源映射编号或含义确认状态无效：{mapping_id}')
        if mapping.get('standard_id') not in standards:
            raise ValueError(f'来源映射引用了不存在的统一数据项：{mapping_id}')
        if mapping.get('source_id') not in sources:
            raise ValueError(f'来源映射引用了不存在的数据来源：{mapping_id}')
        method_id = mapping.get('query_method_id')
        if method_id is not None and method_id not in methods:
            raise ValueError(f'来源映射引用了不存在的取数方法：{mapping_id}')
        if mapping.get('technical_status') == 'verified_source_values' and not method_id:
            raise ValueError(f'已验证的来源值缺少取数方法：{mapping_id}')
        standard = standards[mapping['standard_id']]
        validate_conversion(mapping.get('normalization'), mapping.get('original_unit'), standard.get('unit'), mapping_id)
        if method_id:
            method = methods[method_id]
            if mapping['source_id'] != method['source_id']:
                raise ValueError('来源映射和取数方法不属于同一来源：' + mapping_id)
            if not mapping.get('response_field'):
                raise ValueError('已配置取数方法但缺返回字段：' + mapping_id)
            if method.get('path'):
                if mapping.get('endpoint') != method['path'] or mapping.get('method') != method['method']:
                    raise ValueError('来源映射接口或路径参数与方法不一致：' + mapping_id)
                if method_id == 'power.pv.report' and mapping['request_parameters'] != method['parameters']:
                    raise ValueError('来源映射请求参数与方法不一致：' + mapping_id)
                if method_id == 'power.station.archive':
                    if mapping['request_parameters'] != {'station_code': '${station_code}'}:
                        raise ValueError('档案参数未绑定本站：' + mapping_id)
                    if mapping['response_path'] != 'data.record.' + mapping['response_field']:
                        raise ValueError('来源映射返回字段与路径不一致：' + mapping_id)
            binding = mapping.get('value_binding', {})
            if binding.get('field') != mapping['response_field']:
                raise ValueError('来源映射的结构化读取字段不一致：' + mapping_id)
            allowed = {
                'power.station.archive': {'record': {'stationCode', 'stationName', 'stationCapacity'}},
                'power.pv.report': {'rows_pending': {'genValid', 'genInternet'}},
                'power.electricitybill.settlement.v1': {
                    'settlement': {'totalpower_name', 'totalowner_name', 'totalonline_name'},
                    'settlement_assets': {'meter_reading_photos'},
                    'order': {'tbl_number', 'settlement_month', 'settlement_month_start', 'settlement_month_end', 'cne_station'}},
                'power.other_work.executions.v1': {
                    'execution': {'process_table_tbl_photos','process_table_tbl_instructions','process_table_st_time'},
                    'order': {'tbl_category'}},
                'power.electricitybill.attachment_meter.v1': {'bill_attachment': {'generation_kwh','consumption_kwh','grid_kwh','station_use_kwh'}},
                'power.plan.triggers.v1': {'issued_plan': {'tbl_description'}},
            }
            if binding['field'] not in allowed[method_id].get(binding.get('scope'), set()):
                raise ValueError('来源映射返回字段不在已支持的接口位置：' + mapping_id)
            labels = {'stationCode': '电站编码', 'stationName': '电站名称', 'stationCapacity': '装机容量',
                      'genValid': '实发电量', 'genInternet': '上网电量',
                      'totalpower_name': '总发电量-电量（kWh）', 'totalowner_name': '企业自用电量-电量（kWh）',
                      'totalonline_name': '上网电量-电量（kWh）', 'tbl_number': '工单编号',
                      'settlement_month': '结算月份', 'settlement_month_start': '结算开始日期',
                      'settlement_month_end': '结算结束日期', 'cne_station': '电站名称',
                      'process_table_tbl_photos':'添加照片','process_table_tbl_instructions':'处理记录',
                      'process_table_st_time':'处理时间','tbl_category':'工单类别','meter_reading_photos':'meter_reading_photos',
                      'generation_kwh':'发电总量（结算附件复算）','consumption_kwh':'消纳电量（结算附件复算）',
                      'grid_kwh':'上网总量（结算附件复算）','station_use_kwh':'光伏运营用电量（表底或月台账）','tbl_description':'计划说明'}
            if mapping['source_field_name'] != labels[binding['field']]:
                raise ValueError('来源字段名称与已核验的返回键不一致：' + mapping_id)
            if method_id=='power.electricitybill.attachment_meter.v1':
                if binding.get('metric')!=binding['field'] or mapping.get('response_path')!='attachment_sources[*].metrics.'+binding['field']:
                    raise ValueError('结算附件数据项和返回位置不一致：'+mapping_id)
            if method_id=='power.plan.triggers.v1':
                if mapping.get('response_path')!='data.plans[*].text':
                    raise ValueError('计划说明归一返回位置不一致：'+mapping_id)
            if method_id == 'power.electricitybill.settlement.v1':
                steps = method['steps']
                if (mapping['method'] != ' → '.join(s['method'] for s in steps)
                        or mapping['endpoint'] != ' → '.join(s['path'] for s in steps)):
                    raise ValueError('结算映射接口链与查询方法不一致：' + mapping_id)
                if mapping['request_parameters'] != {
                    'station_in': ['${station_code}'], 'settlement_month': '${settlement_month}',
                    'processInsId': '${selected.processInstanceId}', 'taskId': '${selected.taskId}',
                    'Electricitybill_CORRELATION_ID': '${detail.data.process.variables.r_id}'}:
                    raise ValueError('结算映射站点、期间或关联参数错误：' + mapping_id)
                prefix = '结算子表 data.datas[唯一关联记录].' if binding['scope'] in {'settlement','settlement_assets'} else '详情 data.process.variables.'
                if mapping['response_path'] != prefix + binding['field']:
                    raise ValueError('结算映射返回路径与读取位置不一致：' + mapping_id)
            identity = json.dumps({key: mapping.get(key) for key in (
                'source_id', 'query_method_id', 'method', 'endpoint', 'response_path',
                'value_binding', 'request_parameters', 'original_unit', 'normalization', 'time_field')},
                ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if identity in unique_mappings:
                raise ValueError('同一来源字段重复映射：' + mapping_id)
            unique_mappings.add(identity)
        elif mapping.get('technical_status', '').startswith('verified'):
            raise ValueError('未配置方法的来源不能标为已验证：' + mapping_id)
    for relation in catalog.get('relations', []):
        if not set(relation.get('fields', [])) <= set(standards):
            raise ValueError('数据项关系引用了不存在的统一数据项')
    alignment = catalog.get('deduplication', {}).get('business_alignment_key', [])
    if 'source_id' in alignment or not {'standard_id', 'station_id', 'business_period', 'grain_object', 'caliber', 'version'} <= set(alignment):
        raise ValueError('跨来源对齐键缺少范围或混入source_id')
    return catalog
