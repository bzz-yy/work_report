"""Offline layout trial only: request/config values, no synthetic business numbers."""
from report_route import read


def collect_missing(plan):
    from fixed_fields import fresh_bindings, without_fixed_sources, apply_fixed
    plan = without_fixed_sources(plan, fresh_bindings(plan))
    cfg = read(plan['station_config'])
    result = {'station_id':plan['station_id'],'period':plan['period'],'powerplus_station_id':plan['bound_parameters']['powerplus_station_id'],'fields': {}, 'repeat_groups': {}, 'platform_snapshot': {'items': []},
              'observations': [], 'photos': {'status': 'missing', 'items': [], 'reason': '离线试排版，不查询或复用历史照片'},
              'simulation': True, 'network_calls': 0}
    for field in plan['fields']:
        fid = field['field_id']
        item = {'field_id': fid, 'label': field['label'], 'status': 'missing', 'value': None,
                'unit': field['unit'], 'station_id': plan['station_id'], 'period': plan['period'],
                'reason': '离线试排版未查询业务数据；不是平台当期缺数结论'}
        position = field.get('source_position') or {}
        if field['source_mode'] == 'request':
            param = position.get('param')
            if param not in {'year', 'month'}:
                raise ValueError('离线试排版未支持的请求绑定')
            item.update(status='request', value=int(plan['period'][:4] if param == 'year' else plan['period'][5:]),
                        source={'system': '本次请求', 'request': plan['request']})
        elif field['source_mode'] == 'station_profile':
            prop = position.get('property')
            value = cfg.get('profile', {}).get(prop)
            if value is not None:
                item.update(status='config', value=value,
                            source={'system': '本站配置', 'file': plan['station_config'], 'pointer': '/profile/' + prop})
        result['fields'][fid] = item
    result['repeat_groups'] = {key: {'status': 'manual', 'records': [],
        'station_id': plan['station_id'], 'period': plan['period'],
        'reason': '离线试排版未查询业务记录'} for key in plan.get('repeat_group_rules', {})}
    from copy import deepcopy
    if 'records.1' in plan.get('repeat_group_rules',{}):
        columns=plan['repeat_group_rules']['records.1']['field_ids']
        rows=[]
        for month in range(1,int(plan['period'][5:])+1):
            period=plan['period'][:4]+f'-{month:02}'
            row={fid:{**deepcopy(result['fields'][fid]),'period':period} for fid in columns}
            row['F052'].update(value=str(month),status='request',source={'system':'本次请求','request':plan['request']})
            rows.append(row)
        result['repeat_groups']['records.1']={'status':'manual','records':rows,'station_id':plan['station_id'],
            'period':plan['period'],'reason':'仅根据本次请求建立月份行，业务数据未查询'}
    from derived_fields import apply_derivations
    from source_guides import apply_guidance
    apply_guidance(plan, result)
    apply_derivations(plan, result)
    apply_fixed(plan, result)
    return result
