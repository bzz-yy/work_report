"""Extract source values by the dictionary's structured response bindings."""
from pathlib import Path
import json
from decimal import Decimal, InvalidOperation


def at_path(payload,path):
    value=payload
    for key in path.split('.'):
        value=value[int(key)] if isinstance(value,list) else value[key]
    return value


def settlement_value(response,mapping,station_code,period):
    if not response.get('ok'):return None,'query_failed',None
    data=response.get('data')
    if not isinstance(data,dict) or not data:return None,'missing_response',None
    if str(data.get('station_code'))!=str(station_code) or data.get('settlement_month')!=period:
        raise ValueError('来源返回错站或错期')
    if data.get('status') not in {'source_values_retrieved','source_values_partial'}:
        return None,data.get('status','source_missing'),None
    order=data.get('order',{})
    if str(order.get('station'))!=str(station_code) or order.get('settlement_month')!=period:
        raise ValueError('原始工单身份或月份与来源值不一致')
    binding=mapping['value_binding']
    if binding['scope']!='settlement':raise ValueError('尚未实现该来源位置的报告数值读取')
    observed=data.get('standard_values',{}).get(mapping['standard_id'],{})
    if not observed or observed.get('value') in (None,''):return None,'source_missing',None
    if (observed.get('source_mapping_id')!=mapping['mapping_id'] or observed.get('unit')!=mapping['original_unit']
            or str(observed.get('station_code'))!=str(station_code) or observed.get('business_period')!=period):
        raise ValueError('来源数据项、映射、单位或期间不一致')
    matches=[]
    for i,step in enumerate(data.get('evidence',[])):
        if step.get('request',{}).get('body',{}).get('formKey')!='ElectricitybillSettlement':continue
        for j,row in enumerate(step['response_excerpt']['data']['datas']):
            if row['id']==observed.get('source_record_id'):
                if row.get('Electricitybill_CORRELATION_ID')!=data.get('order',{}).get('r_id'):
                    raise ValueError('原始子表证据关联错位')
                path=f'data.evidence.{i}.response_excerpt.data.datas.{j}.{binding["field"]}'
                raw=at_path(response,path)
                if raw!=observed['value']:raise ValueError('来源数据项与原始子表值不一致')
                if isinstance(raw,bool):raise ValueError('原始来源数值不能是布尔值')
                try:number=Decimal(str(raw).strip())
                except InvalidOperation as exc:raise ValueError('原始来源不是数值') from exc
                if not number.is_finite() or number<0:raise ValueError('原始来源数值无效')
                quality='zero_requires_confirmation' if number==0 else 'positive_source_value'
                if quality!=observed['quality']:raise ValueError('来源质量状态与原始数值不一致')
                matches.append((raw,quality,path))
    if len(matches)!=1:raise ValueError('来源值无法唯一关联原始响应')
    return matches[0]


def bill_command(catalog,station_code,period,profile):
    method=catalog['query_methods']['power.electricitybill.settlement.v1']
    dictionary=Path(__file__).resolve().parents[1]
    adapter=(dictionary/method['adapter_script']).resolve()
    if adapter!=dictionary/'脚本/query_power_bill.py':raise ValueError('字典适配入口不支持')
    interpreter=Path.home()/'Library/Application Support/xhyw-power-cli/runtime/python/bin/python3'
    return [str(interpreter),'-B',str(adapter),'--station-code',str(station_code),'--month',period,'--profile',profile,'--stdout']
