"""Recompute issued plan text from retained Power+ responses before report filling.

The source is a scheduling instance, never a completed-work record. Next-month
current text is intentionally kept outside report-ready values.
"""
from __future__ import annotations
from copy import deepcopy
import datetime as dt
import hashlib
import json
import re

METHOD = 'power.plan.triggers.v1'
BASE = 'https://power-xhyw.cnecloud.com'
TZ = dt.timezone(dt.timedelta(hours=8))
PLAN_KEYS = ('id','r_id','station','cne_station','planIssuanceId','triggerDay','triggerTime',
             'due_time','tbl_description','pl_type','pl_category','triggerStatus',
             'createTime','updateTime','isDeleted','isCancelled','cancelled','disabled')
ORDER_KEYS = ('id','r_id','station','cne_station','planId','PlanIssuanceTrigger_CORRELATION_ID',
              'tbl_number','tbl_description','tbl_category','created_at','status','cne_status',
              'isDeleted','isCancelled','cancelled','disabled')
VERIFIED_STEPS = {
    'plan_list': ('POST','/api/blade-form/form/data/list','PlanIssuanceTrigger'),
    'plan_detail': ('GET','/api/blade-form/form/data/detail','PlanIssuanceTrigger'),
    'order_list': ('POST','/api/blade-form/form/data/list','OtherWorkOrder'),
    'order_detail': ('GET','/api/blade-workflow/process/detail',None),
}


def require(value, message):
    if not value: raise ValueError(message)


def bind_parameters(value, parameters):
    if isinstance(value,dict): return {k:bind_parameters(v,parameters) for k,v in value.items()}
    if isinstance(value,list): return [bind_parameters(v,parameters) for v in value]
    if not isinstance(value,str): return value
    exact=re.fullmatch(r'\{\{([^{}]+)\}\}',value)
    if exact:
        require(exact[1] in parameters,'计划字典缺请求参数：'+exact[1])
        return parameters[exact[1]]
    def replace(match):
        require(match[1] in parameters,'计划字典缺请求参数：'+match[1])
        return json.dumps(parameters[match[1]],ensure_ascii=False,separators=(',',':'))
    return re.sub(r'\{\{([^{}]+)\}\}',replace,value)


def validate_method(method):
    require(isinstance(method,dict) and method.get('contract_id')==METHOD
            and method.get('contract_revision')==1 and method.get('base_url')==BASE,
            '计划取数契约身份或版本变化，须重新核验')
    steps=method.get('steps');require(isinstance(steps,list),'计划契约缺steps')
    require(len(steps)==4 and {s.get('id') for s in steps}==set(VERIFIED_STEPS),'计划接口步骤变化')
    result={s['id']:s for s in steps}
    for sid,(verb,path,form) in VERIFIED_STEPS.items():
        s=result[sid];require((s.get('method'),s.get('path'))==(verb,path),'计划接口契约漂移：'+sid)
        if sid.endswith('list'):
            body=s.get('body') or {}
            require(body=={'formKey':form,'search':'{"station_in":[{{station_code_as_integer}}]}',
                    'query':{'current':'{{page}}','size':'{{page_size}}'},'sort':{},
                    'customCriteria':({} if sid=='plan_list' else {'excludeDraft':1})},
                    '计划列表筛选或分页契约变化：'+sid)
            require(s.get('rows_path')=='data.datas' and s.get('total_path')=='data.totalCount',
                    '计划列表返回路径变化：'+sid)
        elif sid=='plan_detail':
            require(s.get('query')=={'formKey':form,'id':'{{selected_row.id}}'} and s.get('rows_path')=='data','计划详情关联参数或返回路径变化')
        else:
            require(s.get('query')=={'processInsId':'{{selected_row.processInstanceId}}','taskId':'{{selected_row.taskId}}'} and s.get('rows_path')=='data.process.variables',
                    '派发工单详情关联参数变化')
    return result


def method_digest(method):
    validate_method(method)
    return hashlib.sha256(json.dumps(method['steps'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def window(period):
    require(isinstance(period,str) and re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',period),'计划月份需YYYY-MM')
    y,m=map(int,period.split('-'));start=dt.datetime(y,m,1,tzinfo=TZ)
    end=dt.datetime(y+1,1,1,tzinfo=TZ) if m==12 else dt.datetime(y,m+1,1,tzinfo=TZ)
    return start,end


def parse_date(value, label, timestamp=False):
    pattern=r'20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' if timestamp else r'20\d{2}-\d{2}-\d{2}'
    require(isinstance(value,str) and re.fullmatch(pattern,value),'计划时间格式无效：'+label)
    return dt.datetime.strptime(value,'%Y-%m-%d %H:%M:%S' if timestamp else '%Y-%m-%d').replace(tzinfo=TZ)


def epoch(value,label):
    require(not isinstance(value,bool) and isinstance(value,(int,float)) and value>0,'计划epoch时间无效：'+label)
    return dt.datetime.fromtimestamp(value/1000,TZ)


def variables(row):
    value=row.get('variables')
    if isinstance(value,str): value=json.loads(value)
    require(isinstance(value,dict),'派发工单缺variables')
    return value


def project_plan(row):
    require(isinstance(row,dict),'计划条目非对象')
    if row.get('variables') is not None:
        nested=variables(row)
        require(all(nested[k]==row[k] for k in PLAN_KEYS if k in nested and k in row),'计划列表顶层与variables冲突')
    return {k:deepcopy(row[k]) for k in PLAN_KEYS if k in row}


def project_order(row):
    require(isinstance(row,dict),'派发工单条目非对象');v=variables(row)
    require(all(row[k]==v[k] for k in ORDER_KEYS if k in row and k in v),'派发工单顶层与variables冲突')
    return {**{k:deepcopy(row[k]) for k in ('id','processInstanceId','taskId') if k in row},
            'variables':{k:deepcopy(v[k]) for k in ORDER_KEYS if k in v}}


def is_disabled(row):
    return any(row.get(k) in (True,1,'1','true') for k in ('isDeleted','isCancelled','cancelled','disabled'))


def _response(entry):
    response=entry.get('response_excerpt') or {}
    require(str(response.get('code'))=='200' and response.get('success') is not False,'计划原始响应未成功')
    return response


def page_rows(evidence, sid, step, code):
    pages=[(i,e) for i,e in enumerate(evidence) if e.get('step_id')==sid]
    require(bool(pages),'计划来源缺完整分页：'+sid)
    rows=[];seen=set();total=None
    for expected_page,(i,e) in enumerate(pages,1):
        req=e.get('request') or {};body=req.get('body') or {};q=body.get('query') or {};size=q.get('size')
        require(not isinstance(size,bool) and isinstance(size,int) and 1<=size<=100,'计划分页大小非法')
        expected=bind_parameters(step['body'],{'station_code_as_integer':int(code),'page':expected_page,'page_size':size})
        require(req=={'method':step['method'],'path':step['path'],'body':expected},'计划分页请求与字典不一致')
        d=_response(e).get('data') or {};batch=d.get('datas');raw_total=d.get('totalCount')
        require(isinstance(batch,list) and not isinstance(raw_total,bool) and str(raw_total).isdigit(),'计划分页响应结构非法')
        current=int(raw_total);require(total is None or total==current,'计划分页总数发生变化');total=current
        require(d.get('pageNo')==expected_page and d.get('pageSize')==size,'计划分页页码/大小与请求不一致')
        require(isinstance(d.get('hasNextPage'),bool),'计划分页缺hasNextPage')
        require(len(batch)<=size,'计划响应超出分页大小')
        for j,row in enumerate(batch):
            require(isinstance(row,dict) and isinstance(row.get('id'),str) and row['id'] and row['id'] not in seen,'计划分页重复或缺标识')
            seen.add(row['id']);v=variables(row) if sid=='order_list' else row
            require(str(v.get('station'))==str(code),'计划或派发工单列表返回错站')
            rows.append((row,f'data.evidence.{i}.response_excerpt.data.datas.{j}'))
        require(len(rows)<=total,'计划分页超过总数')
        last=expected_page==len(pages)
        require(d['hasNextPage']==(not last),'计划分页hasNextPage与保留页面不一致')
        if not last:require(bool(batch) and len(rows)<total,'计划分页提前结束或重复请求')
    require(len(rows)==total,'计划分页未覆盖总数')
    return rows


def _detail(evidence,sid,step,parameters):
    query=bind_parameters(step['query'],parameters)
    found=[(i,e) for i,e in enumerate(evidence) if e.get('step_id')==sid and (e.get('request') or {}).get('query')==query]
    require(len(found)==1,'计划详情缺失或重复：'+sid)
    i,e=found[0]
    require(e.get('request')=={'method':step['method'],'path':step['path'],'query':query},'计划详情请求与字典不一致')
    return _response(e).get('data'),f'data.evidence.{i}.response_excerpt.data'


def recompute_plans(data,context,method):
    """Return plans/candidates from only the retained raw response excerpts."""
    steps=validate_method(method);code=str(context['powerplus_station_id']);period=context['period']
    start,end=window(period);next_end=window(end.strftime('%Y-%m'))[1]
    require(str(data.get('station_code'))==code and data.get('period')==period,'计划返回错站或错期')
    require(data.get('source_method')==METHOD and data.get('base_url')==BASE,'计划返回来源不符')
    require(data.get('contract_sha256')==method_digest(method),'计划来源请求契约摘要不一致')
    evidence=data.get('evidence');require(isinstance(evidence,list),'计划来源缺evidence')
    require(all(isinstance(e,dict) and e.get('step_id') in steps for e in evidence),'计划证据包含未知步骤')
    listed=page_rows(evidence,'plan_list',steps['plan_list'],code)
    order_rows=page_rows(evidence,'order_list',steps['order_list'],code)
    plans=[];candidates=[];rejected=[];business_seen=set()
    for row,list_path in listed:
        scheduled=parse_date(row.get('triggerDay'),'应下发时间')
        if not (start<=scheduled<next_end):continue
        biz=(code,row.get('planIssuanceId'),row.get('triggerDay'))
        require(biz not in business_seen,'同计划同日期多条实例，须处理重复或版本冲突');business_seen.add(biz)
        if scheduled<end and row.get('pl_type')!='其他计划':
            rejected.append({'id':row['id'],'reason':'unsupported_plan_type','plan_type':row.get('pl_type')});continue
        detail,path=_detail(evidence,'plan_detail',steps['plan_detail'],{'selected_row.id':row['id']})
        require(isinstance(detail,dict),'计划详情不是对象')
        require(all(detail.get(k)==row.get(k) for k in PLAN_KEYS),'计划列表和详情发生变化或身份不一致')
        require(str(detail.get('station'))==code and detail.get('id')==row['id']
                and detail.get('r_id') and detail.get('planIssuanceId'),'计划详情错站或缺关系标识')
        due=parse_date(detail.get('due_time'),'要求完成时间');require(due>=scheduled,'计划要求完成时间早于应下发时间')
        created=epoch(detail.get('createTime'),'createTime');updated=epoch(detail.get('updateTime'),'updateTime')
        require(updated>=created,'计划更新时间早于创建时间')
        text=detail.get('tbl_description');require(isinstance(text,str) and text.strip(),'计划说明为空')
        if is_disabled(detail):rejected.append({'id':row['id'],'reason':'explicit_cancelled_or_disabled'});continue
        common={'plan_id':detail['id'],'relationship_id':detail['r_id'],'parent_plan_id':detail['planIssuanceId'],
                'station_code':code,'scheduled_issue_date':detail['triggerDay'],'required_completion_date':detail['due_time'],
                'category':detail.get('pl_category'),'plan_type':detail.get('pl_type'),'text':text,
                'plan_path':path,'list_path':list_path}
        if scheduled>=end:
            candidates.append({**common,'period':end.strftime('%Y-%m'),'quality':'as_of_version_unverified',
                'existed_by_report_cutoff':created<end,'updated_after_report_cutoff':updated>=end,
                'reason':'下一月当前计划说明未取得报告期末内容版本，不填写F069。'})
            continue
        actual=detail.get('triggerTime')
        if not actual:rejected.append({'id':row['id'],'reason':'no_actual_issue_time'});continue
        issued=parse_date(actual,'实际下发时间',timestamp=True)
        require(created<=issued,'计划创建时间晚于实际下发时间')
        if not start<=issued<end:rejected.append({'id':row['id'],'reason':'issued_outside_report_period'});continue
        # triggerStatus is intentionally ignored as a proof of field-work completion.
        linked=[(o,p) for o,p in order_rows if variables(o).get('PlanIssuanceTrigger_CORRELATION_ID')==detail['r_id']]
        if not linked:rejected.append({'id':row['id'],'reason':'no_matching_issued_order'});continue
        require(len(linked)==1,'一个计划实例关联多个派发工单，不能自动取首条')
        order,order_list_path=linked[0];lv=variables(order)
        require(order.get('processInstanceId') and order.get('taskId'),'派发工单缺流程或任务编号')
        raw,order_path=_detail(evidence,'order_detail',steps['order_detail'],{
            'selected_row.processInstanceId':str(order['processInstanceId']),'selected_row.taskId':str(order['taskId'])})
        process=(raw or {}).get('process') or {};ov=process.get('variables')
        require(isinstance(ov,dict),'派发工单详情缺variables')
        require(str(process.get('processInstanceId'))==str(order['processInstanceId'])
                and process.get('formKey')=='OtherWorkOrder','派发工单流程身份不一致')
        require(all(ov.get(k)==lv.get(k) for k in ORDER_KEYS),'派发工单列表与详情冲突')
        require(ov.get('id')==order['id'] and ov.get('r_id') and str(ov.get('station'))==code,'派发工单身份或r_id不一致')
        require(ov.get('PlanIssuanceTrigger_CORRELATION_ID')==detail['r_id']
                and ov.get('planId')==detail['planIssuanceId'],'计划派发关系不一致')
        require(ov.get('tbl_description')==text and ov.get('tbl_category')==detail.get('pl_category'),'计划与派发工单说明/类别不一致')
        order_created=parse_date(ov.get('created_at'),'派发工单创建时间',timestamp=True)
        require(start<=order_created<end,'派发工单创建期与报告期不符')
        if is_disabled(ov):rejected.append({'id':row['id'],'reason':'issued_order_cancelled_or_disabled'});continue
        plans.append({**common,'period':period,'actual_issue_time':actual,'order_id':order['id'],'order_relationship_id':ov['r_id'],
                      'order_path':order_path+'.process.variables','order_list_path':order_list_path,
                      'order_process_id':str(order['processInstanceId']),'quality':'issued_plan_verified',
                      'meaning':'已下发计划，非已完成工作'})
    plans.sort(key=lambda x:(x['scheduled_issue_date'],str(x['category']),x['plan_id']))
    candidates.sort(key=lambda x:(x['scheduled_issue_date'],str(x['category']),x['plan_id']))
    return {'plans':plans,'next_month_candidates':candidates,'rejected':rejected}


def plan_values(response,index,context,method):
    require(response.get('ok') is True,'计划来源查询未成功')
    data=response.get('data') or {};computed=recompute_plans(data,context,method)
    require(data.get('plans')==computed['plans'] and data.get('next_month_candidates')==computed['next_month_candidates'],
            '计划提取值与保存的原始响应不一致')
    require(not isinstance(index,bool) and isinstance(index,int) and 0<=index<len(computed['plans']),'计划索引无效')
    return computed['plans'][index]


def format_plan(values,selection):
    require(selection.get('formatter')=='issued_plan_text','不支持的计划文字格式')
    require(values['category'] in selection.get('categories',[]) and values['plan_type'] in selection.get('plan_types',[]),
            '计划类别不在报告采用范围')
    return values['text']


def _selection(definition,catalog):
    s=definition.get('plan_selection') or {}
    require(s.get('method_id')==METHOD and s.get('mapping_id')=='SM025' and s.get('sequence_field')=='F059','计划字段选源无效')
    m=catalog['source_mappings'].get(s['mapping_id']) or {}
    require(m.get('query_method_id')==METHOD and m.get('source_id')=='powerplus_plan_triggers'
            and m.get('standard_id')=='D021' and m.get('response_field')=='tbl_description','计划字典来源绑定不一致')
    return s


def bind_plan_records(plan,response,source,catalog):
    definitions=[f for f in plan['fields'] if f.get('plan_selection')]
    if not definitions or not response.get('ok'):return []
    require(len(definitions)==1 and definitions[0]['field_id']=='F060','仅支持F060当月已下发现场计划')
    definition=definitions[0];selection=_selection(definition,catalog);method=catalog['query_methods'][METHOD]
    context={**source,'period':plan['period']};computed=recompute_plans(response.get('data') or {},context,method)
    require(response['data'].get('plans')==computed['plans'],'计划提取结果不符合原始证据')
    rows=[]
    for index,item in enumerate(computed['plans']):
        if item['category'] not in selection.get('categories',[]) or item['plan_type'] not in selection.get('plan_types',[]):continue
        provenance={**deepcopy(source),'source_method':METHOD,'mapping_id':'SM025','standard_id':'D021','plan_index':index,
                    'value_path':item['plan_path']+'.tbl_description'}
        common={'station_id':plan['station_id'],'period':plan['period']}
        rows.append({'F059':{**common,'field_id':'F059','value':len(rows)+1,'status':'derived','unit':'序号',
                    'source':{**deepcopy(provenance),'system':'计划记录序号'}},
                     'F060':{**common,'field_id':'F060','value':format_plan(item,selection),'status':'real','unit':definition['unit'],
                    'source':provenance,'reason':'来自报告月已下发且同站工单关联一致的计划；不表示工作已完成，风险列另待填。'}})
    return rows


def validate_plan_field(item,definition,response,context,catalog):
    require(definition.get('field_id')=='F060' and definition.get('source_mode')=='record_collection','计划字段定义不受支持')
    selection=_selection(definition,catalog);source=item.get('source') or {}
    require(source.get('source_method')==METHOD and source.get('mapping_id')=='SM025' and source.get('standard_id')=='D021','计划来源标记与模板不符')
    values=plan_values(response,source.get('plan_index'),context,catalog['query_methods'][METHOD])
    require(source.get('value_path')==values['plan_path']+'.tbl_description','计划原始来源路径不一致')
    require(item.get('value')==format_plan(values,selection),'计划文字与原始来源或模板选择不一致')
    return values
