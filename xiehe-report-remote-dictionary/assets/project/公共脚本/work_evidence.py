"""Bind verified execution records and photo assets to report-owned selections."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from source_values import at_path


def execution_values(response,index,context):
    data=response.get('data') or {}
    if str(data.get('station_code'))!=str(context['powerplus_station_id']) or data.get('period')!=context['period']:
        raise ValueError('执行记录返回错站或错期')
    record=data['executions'][index]
    raw=at_path(response,record['row_path']);order=at_path(response,record['order_path'])
    category=order.get('tbl_category')
    when=raw.get('process_table_st_time');text=raw.get('process_table_tbl_instructions')
    if (str(order.get('station'))!=str(context['powerplus_station_id']) or order.get('id')!=record['order_id']
            or not isinstance(when,str) or when[:7]!=context['period']
            or when!=record['execution_time'] or text!=record['text'] or category!=record['category']):
        raise ValueError('执行记录与原始详情不一致')
    return {'category':category,'text':text,'time':when}


def format_execution(values,selection):
    if selection.get('formatter') not in {'category_text_time','category_only'}:raise ValueError('不支持的执行记录文字格式')
    if values['category'] not in selection['categories']:raise ValueError('执行记录类别不在本报告选取范围')
    if selection['formatter']=='category_only':return values['category']
    return f"{values['category']}：{values['text']}（处理时间 {values['time']}）"


def bind_work_records(plan,response,source):
    definition=next((f for f in plan['fields'] if (f.get('record_selection') or {}).get('formatter')=='category_text_time'),None)
    if not definition or not response.get('ok'):return []
    selection=definition['record_selection'];rows=[]
    for index,record in enumerate((response.get('data') or {}).get('executions',[])):
        values=execution_values(response,index,source)
        if values['category'] not in selection['categories']:continue
        value=format_execution(values,selection)
        rows.append({selection['sequence_field']:{'value':len(rows)+1,'status':'derived','unit':'序号',
            'station_id':plan['station_id'],'period':plan['period'],'source':{'system':'执行记录序号'}},
            definition['field_id']:{'field_id':definition['field_id'],'value':value,'status':'real','unit':definition['unit'],
                'station_id':plan['station_id'],'period':plan['period'],
                'source':{**deepcopy(source),'source_method':'power.other_work.executions.v1','execution_index':index,
                    'value_path':record['row_path']+'.process_table_tbl_instructions'},
                'reason':'来自本站当期工单处理记录；分类层级未据历史报告补写。'}})
        category_field=next((f for f in plan['fields'] if (f.get('record_selection') or {}).get('formatter')=='category_only'),None)
        if category_field:
            rows[-1][category_field['field_id']]={
                **deepcopy(rows[-1][definition['field_id']]),'field_id':category_field['field_id'],
                'unit':category_field['unit'],'value':values['category']}
            rows[-1][category_field['field_id']]['source']['value_path']=record['order_path']+'.tbl_category'
    return rows


def bind_photos(plan,response,source,mapping_id):
    policy=plan.get('photo_selection') or {}
    if mapping_id not in policy.get('mapping_ids',[]) or not response.get('ok'):return []
    data=response.get('data') or {};items=[]
    for index,item in enumerate(data.get('photos',[])):
        if item.get('download_status')!='downloaded':continue
        if mapping_id=='SM016' and item.get('category') not in policy.get('site_categories',[]):continue
        items.append({**deepcopy(item),'station_id':plan['station_id'],'period':plan['period'],
            'kind':'现场检查照片' if mapping_id=='SM016' else '现场抄表照片',
            'source':{**deepcopy(source),'mapping_id':mapping_id,'photo_index':index,
                'value_path':item['attachment_path']}})
    return items


def validate_photos(plan,photos):
    policy=plan.get('photo_selection') or {};seen=set()
    for item in photos.get('items',[]):
        if item.get('station_id')!=plan['station_id'] or item.get('period')!=plan['period']:
            raise ValueError('图片集合串站或串期')
        source=item.get('source') or {};mid=source.get('mapping_id')
        if mid not in policy.get('mapping_ids',[]):raise ValueError('图片来源未在模板中选定')
        evidence=Path(source.get('evidence_file',''))
        if not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest()!=source.get('evidence_sha256'):
            raise ValueError('图片来源响应摘要不一致')
        response=json.loads(evidence.read_text());data=response.get('data') or {}
        source_period=data.get('period') if mid=='SM016' else data.get('settlement_month')
        if str(data.get('station_code'))!=str(plan['bound_parameters']['powerplus_station_id']) or source_period!=plan['period']:
            raise ValueError('图片原始响应错站或错期')
        raw_photo=data['photos'][source['photo_index']]
        attachment=at_path(response,raw_photo['attachment_path'])
        if item['sha256']!=raw_photo['sha256'] or item['file']!=raw_photo['file'] or item.get('caption')!=raw_photo.get('caption') or item['name']!=(attachment.get('name') or attachment.get('label') or ''):
            raise ValueError('图片清单与原始附件关联不一致')
        if source.get('value_path')!=raw_photo['attachment_path']:raise ValueError('图片附件路径被替换')
        if mid=='SM016':
            if raw_photo['execution_time'][:7]!=plan['period'] or raw_photo['category'] not in policy['site_categories']:
                raise ValueError('现场照片处理期间或工单类别不适用')
        elif raw_photo['period']!=plan['period']:raise ValueError('抄表照片结算期间不适用')
        image=Path(item['file'])
        if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('图片文件缺失或内容变化')
        if item['sha256'] in seen:raise ValueError('图片集合包含重复原图')
        seen.add(item['sha256'])
