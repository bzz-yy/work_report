"""Read OtherWorkOrder execution records and original photos through the CLI session."""
from copy import deepcopy
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin,urlsplit,urlunsplit,parse_qs

from catalog_schema import validate_source_catalog
from remote_catalog import load_catalog
from query_power_bill import bind_parameters, ContractError, require, BASE

METHOD='power.other_work.executions.v1'
ROOT=Path(__file__).resolve().parents[2]
TZ=dt.timezone(dt.timedelta(hours=8))
ROW_KEYS=('process_table_st_time','process_table_tbl_instructions','process_table_tbl_photos')
VARIABLE_KEYS=('id','r_id','station','cne_station','tbl_number','tbl_category','cne_tbl_category','status','cne_status','created_at','completed_at')


def variables(row):
    value=row.get('variables')
    if isinstance(value,str):value=json.loads(value)
    require(isinstance(value,dict),'工单variables不是对象')
    return value


def project_order(row):
    value=variables(row)
    return {**{k:row.get(k) for k in ('id','processInstanceId','taskId')},
            'variables':{k:value.get(k) for k in VARIABLE_KEYS}}


def list_orders(client,method,code,evidence):
    step=method['steps'][0];rows=[];seen=set();expected=None
    for page in range(1,1001):
        body=bind_parameters(step['body'],{'station_code_as_integer':int(code),'page':page,'page_size':100})
        response=client.request(step['method'],step['path'],body=body)
        data=response.get('data') or {};batch=data.get('datas');total=data.get('totalCount')
        require(isinstance(batch,list) and str(total).isdigit(),'工单分页结构无效')
        total=int(total);require(expected is None or expected==total,'查询期间工单总数变化');expected=total
        for row in batch:
            require(row.get('id') and row['id'] not in seen,'工单分页重复或缺标识');seen.add(row['id'])
            require(str(variables(row).get('station'))==str(code),'工单列表返回错站')
        evidence.append({'request':{'method':step['method'],'path':step['path'],'body':body},
            'response_excerpt':{'data':{'totalCount':total,'datas':[project_order(row) for row in batch]}}})
        rows.extend(batch)
        require(len(rows)<=total,'工单分页超过总数')
        if len(rows)==total:return rows
        require(bool(batch),'工单分页提前返回空页')
    raise ContractError('工单分页超过支持范围')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


class IncompleteDownload(OSError):
    pass


def _download_picture_once(client,url,target):
    parsed=urlsplit(url)
    require(parsed.scheme=='https' and parsed.hostname=='powersaber-xhyw.cnecloud.com'
            and parsed.path=='/api/blade-resource/minio/endpoint/getFile'
            and set(parse_qs(parsed.query))=={'fileName'},'照片下载地址不符合已核验接口')
    opener=urllib.request.build_opener(NoRedirect())
    request=urllib.request.Request(url,headers=client._headers())
    try:
        response=opener.open(request,timeout=40)
    except urllib.error.HTTPError as redirect:
        if redirect.code not in (301,302,303,307,308):raise
        location=urljoin(url,redirect.headers['Location']);storage=urlsplit(location)
        require(storage.scheme=='https' and storage.hostname=='powers3-xhyw.cnecloud.com','未核验的对象存储重定向')
        # The object-storage URL carries its own access scope. Never forward API auth.
        response=opener.open(urllib.request.Request(location),timeout=40)
    with response:
        length=getattr(response,'headers',{}).get('Content-Length')
        chunks=[];total=0
        while True:
            chunk=response.read(65536)
            if not chunk:break
            total+=len(chunk)
            require(total<=32*1024*1024,'图片超过支持大小')
            chunks.append(chunk)
        payload=b''.join(chunks)
    if length and int(length)!=len(payload):raise IncompleteDownload('照片长度与HTTP声明不一致')
    require(len(payload)<=32*1024*1024,'图片超过支持大小')
    require(payload[:3]==b'\xff\xd8\xff' or payload.startswith(b'\x89PNG\r\n\x1a\n'),'附件不是JPEG或PNG')
    if payload[:3]==b'\xff\xd8\xff' and payload.rfind(b'\xff\xd9')<0:
        raise IncompleteDownload('JPEG缺少结束标记')
    if payload.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' not in payload[-20:]:
        raise IncompleteDownload('PNG缺少结束块')
    suffix='.jpg' if payload[:3]==b'\xff\xd8\xff' else '.png'
    digest=hashlib.sha256(payload).hexdigest();path=target/(digest+suffix)
    if path.exists():require(hashlib.sha256(path.read_bytes()).hexdigest()==digest,'已有图片内容不一致')
    else:
        with path.open('xb') as stream:stream.write(payload)
    return path,digest,len(payload)


def download_picture(client,url,target):
    for attempt in range(3):
        try:return _download_picture_once(client,url,target)
        except (IncompleteDownload,TimeoutError):
            if attempt==2:raise


def collect(client,station_code,period,photos_directory=None):
    catalog=validate_source_catalog(load_catalog(ROOT))
    method=catalog['query_methods'][METHOD]
    require(client.base.rstrip('/')==BASE,'工单客户端不是已验证来源域')
    require(re.fullmatch(r'\d{7}',str(station_code)) is not None,'无效电站编码')
    require(re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',period) is not None,'必须提供YYYY-MM')
    destination=Path(photos_directory).resolve() if photos_directory else None
    if destination:destination.mkdir(parents=True,exist_ok=True)
    result={'source_method':METHOD,'station_code':str(station_code),'period':period,
        'captured_at':dt.datetime.now(TZ).isoformat(timespec='seconds'),'executions':[],'photos':[],
        'rejected_photos':[],'evidence':[],'coverage':'本站可见OtherWorkOrder完整分页；不代表全部运维工单类型'}
    orders=list_orders(client,method,station_code,result['evidence'])
    result['order_count']=len(orders);seen_photos=set()
    for order in orders:
        step=method['steps'][1]
        require(order.get('processInstanceId') and order.get('taskId'),'工单缺详情关联编号')
        query=bind_parameters(step['query'],{'selected_row.processInstanceId':str(order['processInstanceId']),
                                            'selected_row.taskId':str(order['taskId'])})
        response=client.request(step['method'],step['path'],query=query)
        process=(response.get('data') or {}).get('process') or {};value=process.get('variables')
        require(isinstance(value,dict),'工单详情缺variables')
        require(str(value.get('station'))==str(station_code) and value.get('id')==order['id']
                and str(process.get('processInstanceId'))==str(order['processInstanceId'])
                and process.get('formKey')=='OtherWorkOrder','工单详情身份或表单关联错误')
        raw_rows=value.get('process_table') or []
        if isinstance(raw_rows,str):raw_rows=json.loads(raw_rows)
        require(isinstance(raw_rows,list),'执行记录不是数组')
        filtered={k:value.get(k) for k in VARIABLE_KEYS}
        filtered['process_table']=[{k:row.get(k) for k in ROW_KEYS} for row in raw_rows]
        evidence_index=len(result['evidence'])
        result['evidence'].append({'request':{'method':step['method'],'path':step['path'],'query':query},
            'response_excerpt':{'data':{'process':{'processInstanceId':process['processInstanceId'],
                'formKey':process['formKey'],'processIsFinished':process.get('processIsFinished'),'variables':filtered}}}})
        for row_index,row in enumerate(raw_rows):
            when=row.get('process_table_st_time')
            if not when:continue
            require(isinstance(when,str) and re.fullmatch(r'20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}',when),'执行日期格式未核验')
            dt.datetime.strptime(when,'%Y-%m-%d %H:%M:%S')
            if when[:7]!=period:continue
            text=row.get('process_table_tbl_instructions')
            if not isinstance(text,str) or not text.strip():continue
            prefix=f'data.evidence.{evidence_index}.response_excerpt.data.process.variables'
            record={'order_id':order['id'],'order_number':value.get('tbl_number'),'row_index':row_index,
                'execution_time':when,'text':text,'category':value.get('tbl_category'),
                'status':value.get('cne_status'),'row_path':prefix+f'.process_table.{row_index}',
                'order_path':prefix,'station_code':str(station_code),'period':period}
            result['executions'].append(record)
            candidates=row.get('process_table_tbl_photos') or []
            require(isinstance(candidates,list),'执行图片字段不是数组')
            for photo_index,photo in enumerate(candidates):
                meta={**record,'photo_index':photo_index,'name':photo.get('name') or photo.get('label') or '',
                    'attachment_path':record['row_path']+f'.process_table_tbl_photos.{photo_index}'}
                url=photo.get('url') or photo.get('value')
                if not url:
                    result['rejected_photos'].append({**meta,'reason':'empty_url'});continue
                name_date=re.search(r'(20\d{2})[_-]?(\d{2})[_-]?(\d{2})',meta['name'])
                if name_date and f'{name_date[1]}-{name_date[2]}'!=period:
                    result['rejected_photos'].append({**meta,'reason':'filename_month_differs_from_execution'});continue
                if not destination:
                    result['photos'].append({**meta,'download_status':'not_requested'});continue
                try:path,digest,size=download_picture(client,url,destination)
                except ContractError:raise
                except Exception as exc:
                    result['rejected_photos'].append({**meta,'reason':'download_failed','error_type':type(exc).__name__});continue
                if digest in seen_photos:
                    result['rejected_photos'].append({**meta,'reason':'duplicate_bytes','sha256':digest});continue
                seen_photos.add(digest)
                result['photos'].append({**meta,'file':str(path),'sha256':digest,'bytes':size,
                    'download_status':'downloaded','period_basis':'execution_record_time_with_filename_check',
                    'caption':text+'（处理日期 '+when[:10]+'）'})
    result['execution_count']=len(result['executions']);result['photo_count']=len(result['photos'])
    result['status']='source_records_retrieved' if result['executions'] else 'no_visible_execution_records'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--station-code',required=True);parser.add_argument('--period',required=True)
    parser.add_argument('--profile',default='me');parser.add_argument('--photos-out',type=Path)
    args=parser.parse_args()
    sys.path.insert(0,str(Path.home()/'Library/Application Support/xhyw-power-cli'))
    from power_ui.paths import load_dotenv_local,apply_profile_session_env
    from power_ui.session import client_for
    from power_ui.errors import CliError
    try:
        load_dotenv_local();apply_profile_session_env(args.profile)
        result=collect(client_for(args.profile),args.station_code,args.period,args.photos_out)
    except CliError as exc:
        print(json.dumps({'ok':False,'error':{'code':exc.code}}));raise SystemExit(1)
    except (ValueError,KeyError,TypeError):
        print(json.dumps({'ok':False,'error':{'code':'CONTRACT_ERROR'}}));raise SystemExit(3)
    print(json.dumps({'ok':True,'data':result},ensure_ascii=False))


if __name__=='__main__':main()
