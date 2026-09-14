"""Apply a selected source to a report field; shared by report generators."""
from copy import deepcopy
from source_values import settlement_value
from value_rules import transform


def choose_value(field, selection, catalog, response, provenance, controls):
    mid=selection['mapping_id'];mapping=catalog['source_mappings'][mid]
    period=provenance['period']
    raw,quality,path=settlement_value(response,mapping,provenance['powerplus_station_id'],period)
    result={'field_id':field['field_id'],'label':field['label'],'unit':field['unit'],
        'station_id':provenance['station_id'],'powerplus_station_id':provenance['powerplus_station_id'],
        'period':period,'status':'missing','value':None,'source':deepcopy(provenance)}
    control=controls.get(mid,{})
    blocked=next((x for x in control.get('blocked_periods',[]) if x['period']==period),None)
    if blocked:
        result.update(status='unverified',reason=blocked['reason'])
        return result
    if raw is None:
        result['reason']='选定来源未取得有效值：'+quality
        return result
    record=response['data']['standard_values'][mapping['standard_id']]['source_record_id']
    zero_verified=any(x['period']==period and x['source_record_id']==record for x in control.get('confirmed_zeros',[]))
    if quality=='zero_requires_confirmation' and not zero_verified:
        result.update(status='unverified',reason='Power+原始0可能为流程填0；本站本期尚无零值核验证据。')
        return result
    standard=catalog['standard_fields'][mapping['standard_id']]
    value=transform(raw,mapping,standard,selection['display'])
    result.update(status='real',value=value,reason='按模板选定来源、本站和业务期间实际查询并换算。')
    result['source'].update(mapping_id=mid,standard_id=mapping['standard_id'],value_path=path,
        raw_value=raw,source_unit=mapping['original_unit'],source_record_id=record,
        source_quality=quality,zero_verified=zero_verified,
        transformation=deepcopy(selection['display']),validation_sha256=selection['validation_sha256'])
    return result
