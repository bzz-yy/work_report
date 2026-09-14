#!/usr/bin/env python3
"""Read and validate issued onsite plan instances through the installed Power+ CLI session.

Requests are bound from the project dictionary. This adapter never issues plans
or completes work. Current next-month text remains a non-fillable candidate.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'公共脚本'))
from plan_values import (BASE,METHOD,TZ,PLAN_KEYS,ORDER_KEYS,bind_parameters,validate_method,
    method_digest,project_plan,project_order,variables,parse_date,window,require,page_rows,recompute_plans)
from remote_catalog import load_catalog


def source_catalog():
    from catalog_schema import validate_source_catalog
    catalog=validate_source_catalog(load_catalog(ROOT))
    validate_method(catalog['query_methods'][METHOD])
    return catalog


def _success(response):
    require(isinstance(response,dict) and str(response.get('code'))=='200' and response.get('success') is not False,
            '计划接口返回非成功响应')


def _request(client,step,parameters,evidence,project):
    req={'method':step['method'],'path':step['path']}
    for key in ('body','query'):
        if key in step:req[key]=bind_parameters(step[key],parameters)
    response=client.request(req['method'],req['path'],body=req.get('body'),query=req.get('query'))
    _success(response)
    excerpt={'code':response['code'],'success':response.get('success',True),'data':project(response.get('data'))}
    evidence.append({'step_id':step['id'],'request':req,'response_excerpt':excerpt})
    return excerpt['data']


def _list(client,step,code,page_size,evidence):
    total=None;seen=set();result=[]
    def project(data):
        require(isinstance(data,dict) and isinstance(data.get('datas'),list),'计划列表响应缺data.datas')
        fn=project_plan if step['id']=='plan_list' else project_order
        return {**{k:deepcopy(data[k]) for k in ('totalCount','pageNo','pageSize','hasNextPage') if k in data},
                'datas':[fn(x) for x in data['datas']]}
    for page in range(1,1001):
        data=_request(client,step,{'station_code_as_integer':int(code),'page':page,'page_size':page_size},evidence,project)
        raw_total=data.get('totalCount');require(not isinstance(raw_total,bool) and str(raw_total).isdigit(),'计划totalCount非法')
        current=int(raw_total);require(total is None or total==current,'计划分页总数发生变化');total=current
        require(data.get('pageNo')==page and data.get('pageSize')==page_size,'计划页码或大小与请求不符')
        require(isinstance(data.get('hasNextPage'),bool),'计划分页缺hasNextPage')
        batch=data['datas'];require(len(batch)<=page_size,'计划返回过多分页条目')
        for row in batch:
            require(isinstance(row.get('id'),str) and row['id'] and row['id'] not in seen,'计划分页重复或缺id')
            seen.add(row['id']);value=variables(row) if step['id']=='order_list' else row
            require(str(value.get('station'))==str(code),'计划或派发工单返回错站')
        result.extend(batch);require(len(result)<=total,'计划分页超过totalCount')
        if len(result)==total:
            require(data['hasNextPage'] is False,'计划总数与hasNextPage不一致')
            return result
        require(data['hasNextPage'] is True and bool(batch),'计划分页提前结束')
    raise ValueError('计划分页超过1000页上限')


def collect(client,station_code,period,page_size=100,catalog=None):
    catalog=source_catalog() if catalog is None else catalog
    method=catalog['query_methods'][METHOD];steps=validate_method(method)
    require(client.base.rstrip('/')==BASE,'计划客户端不是已核验域名')
    code=str(station_code);require(re.fullmatch(r'\d{7}',code),'计划查询站码必须为七位数字')
    require(not isinstance(page_size,bool) and isinstance(page_size,int) and 1<=page_size<=100,'计划page_size需1到100')
    start,end=window(period);next_end=window(end.strftime('%Y-%m'))[1]
    result={'source_method':METHOD,'base_url':BASE,'contract_sha256':method_digest(method),'station_code':code,'period':period,
            'captured_at':dt.datetime.now(TZ).isoformat(timespec='seconds'),'evidence':[],
            'coverage':'本站可见计划和派发工单完整分页；只采用同月已下发且OtherWorkOrder关联一致的现场计划，非完整月度作业安排。'}
    evidence=result['evidence']
    plans=_list(client,steps['plan_list'],code,page_size,evidence)
    orders=_list(client,steps['order_list'],code,page_size,evidence)
    result['station_plan_count']=len(plans);result['station_other_order_count']=len(orders)
    fetched_orders=set()
    def project_detail(data):
        return project_plan(data)
    def project_process(data):
        require(isinstance(data,dict) and isinstance(data.get('process'),dict),'派发工单详情缺process')
        p=data['process'];v=p.get('variables');require(isinstance(v,dict),'派发工单详情缺variables')
        return {'process':{**{k:deepcopy(p[k]) for k in ('processInstanceId','taskId','formKey','processIsFinished') if k in p},
                           'variables':{k:deepcopy(v[k]) for k in ORDER_KEYS if k in v}}}
    for row in plans:
        scheduled=parse_date(row.get('triggerDay'),'应下发时间')
        if not start<=scheduled<next_end:continue
        if scheduled<end and row.get('pl_type')!='其他计划':continue
        detail=_request(client,steps['plan_detail'],{'selected_row.id':row['id']},evidence,project_detail)
        require(str(detail.get('station'))==code and detail.get('id')==row['id'],'计划详情返回错站或错误id')
        if scheduled>=end:continue
        linked=[o for o in orders if variables(o).get('PlanIssuanceTrigger_CORRELATION_ID')==detail.get('r_id')]
        require(len(linked)<=1,'计划实例关联多个派发工单')
        for order in linked:
            if order['id'] in fetched_orders:continue
            require(order.get('processInstanceId') and order.get('taskId'),'派发工单缺流程实例或任务编号')
            _request(client,steps['order_detail'],{'selected_row.processInstanceId':str(order['processInstanceId']),
                         'selected_row.taskId':str(order['taskId'])},evidence,project_process)
            fetched_orders.add(order['id'])
    computed=recompute_plans(result,{'powerplus_station_id':code,'period':period},method)
    result.update(computed)
    result['status']='source_plans_partial' if result['plans'] else 'no_verified_issued_onsite_plan'
    result['missing']=[{'fields':['F061','F062','F063'],'reason':'计划说明未提供与当次作业绑定的主要风险点、风险等级或管控措施，不能自动写低风险。'},
                       {'fields':['F069'],'reason':'下一月返回的是当前计划说明，缺报告期末内容版本，候选仅保存在证据中。'}]
    if not result['plans']:result['missing'].append({'fields':['F060'],'reason':'没有取得同月已下发且同站关联工单可核验的现场计划；不等于无计划。'})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--station-code',required=True);parser.add_argument('--period',required=True)
    parser.add_argument('--profile',default='me');parser.add_argument('--page-size',type=int,default=100)
    args=parser.parse_args()
    sys.path.insert(0,str(Path.home()/'Library/Application Support/xhyw-power-cli'))
    from power_ui.paths import load_dotenv_local,apply_profile_session_env
    from power_ui.session import client_for
    from power_ui.errors import CliError
    try:
        catalog=source_catalog()
        load_dotenv_local();apply_profile_session_env(args.profile)
        result=collect(client_for(args.profile),args.station_code,args.period,args.page_size,catalog)
    except CliError as exc:
        print(json.dumps({'ok':False,'error':{'code':exc.code}}));raise SystemExit(1)
    except (ValueError,KeyError,TypeError):
        print(json.dumps({'ok':False,'error':{'code':'CONTRACT_ERROR'}}));raise SystemExit(3)
    print(json.dumps({'ok':True,'data':result},ensure_ascii=False))


if __name__=='__main__':main()
