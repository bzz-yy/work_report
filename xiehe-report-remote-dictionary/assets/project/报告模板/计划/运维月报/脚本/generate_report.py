"""Single live-data entry point: request -> rules -> Power+ -> editable DOCX."""
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from xml.sax.saxutils import escape

from resolve_report import resolve,ROOT
from selected_source import choose_value
from work_evidence import execution_values,format_execution,validate_photos
from source_guides import apply_guidance,write_pending_guide


def write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def validate_collection(plan,collected):
    """Prevent trusted statuses from hiding a wrong station, period, or empty value."""
    allowed={'real','request','config','derived','manual','missing','unverified','fixed'}
    from fixed_fields import fresh_bindings, validate_item as validate_fixed_item
    fixedness = fresh_bindings(plan)
    definitions={f['field_id']:f for f in plan['fields']}
    catalog=json.loads(Path(plan['source_catalog_file']).read_text(encoding='utf-8'))
    unknown=set(collected.get('fields',{}))-set(definitions)
    if unknown:raise ValueError('取数结果包含模板外字段：'+','.join(sorted(unknown)))
    trusted={'real','request','config','derived','fixed'}
    def check_value(value,label):
        if value is None or value=='' or isinstance(value,bool):raise ValueError('已填状态没有有效值：'+label)
        if isinstance(value,float) and not math.isfinite(value):raise ValueError('不能填写NaN或无穷值：'+label)
    def check_context(item,expected_period,label):
        if item.get('station_id')!=plan['station_id'] or item.get('period')!=expected_period:
            raise ValueError('取数结果电站或期间不匹配：'+label)
    def check_source(item,label):
        source=item.get('source')
        if not isinstance(source,dict) or not source.get('system'):
            raise ValueError('已填字段缺少结构化来源：'+label)
        if item['status']=='real':
            if source.get('system')!='Power+':raise ValueError('线上真实字段缺少Power+来源：'+label)
            p=Path(source.get('evidence_file',''))
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=source.get('evidence_sha256'):
                raise ValueError('平台响应证据不存在或摘要不一致：'+label)
            if source.get('station_id')!=plan['station_id'] or str(source.get('powerplus_station_id'))!=str(plan['bound_parameters']['powerplus_station_id']):
                raise ValueError('平台来源证据串站：'+label)
            if source.get('period')!=item.get('period'):
                raise ValueError('平台来源证据期间不一致：'+label)
            try:captured=dt.datetime.fromisoformat(source['captured_at'])
            except (ValueError,KeyError,TypeError) as e:raise ValueError('平台来源采集时间无效：'+label) from e
            if captured.tzinfo is None:raise ValueError('平台采集时间缺少时区：'+label)
            value_path=source.get('value_path')
            if not isinstance(value_path,str) or not value_path:raise ValueError('真实值缺少响应字段路径：'+label)
            raw=json.loads(p.read_text(encoding='utf-8'))
            if label in definitions:
                if definitions[label].get('attachment_selection') and source.get('source_method')=='power.electricitybill.attachment_meter.v1':
                    from bill_meter_values import validate_meter_field
                    validate_meter_field(item,definitions[label],raw,catalog,plan)
                    return
                if definitions[label].get('plan_selection'):
                    from plan_values import validate_plan_field
                    validate_plan_field(item,definitions[label],raw,source,catalog)
                    return
                records=definitions[label].get('record_selection')
                if records:
                    if definitions[label]['source_mode']!='record_collection' or source.get('source_method')!=records['method_id']:
                        raise ValueError('执行记录来源不符合字段规则')
                    values=execution_values(raw,source['execution_index'],source)
                    entry=raw['data']['executions'][source['execution_index']]
                    expected_path=(entry['order_path']+'.tbl_category' if records['formatter']=='category_only'
                                   else entry['row_path']+'.process_table_tbl_instructions')
                    if source['value_path']!=expected_path:
                        raise ValueError('执行记录原始路径不一致')
                    if format_execution(values,records)!=item['value']:
                        raise ValueError('执行记录文字与原始来源不一致')
                    return
                selection=definitions[label].get('source_selection')
                if not selection or definitions[label]['source_mode']!='catalog' or source.get('mapping_id')!=selection['mapping_id']:
                    raise ValueError('真实业务字段缺少模板选定来源：'+label)
                expected=choose_value(definitions[label],selection,catalog,raw,source,plan.get('source_controls',{}))
                if (expected['status']!='real' or expected['value']!=item['value']
                        or source.get('value_path')!=expected['source']['value_path']
                        or source.get('transformation')!=selection['display']
                        or source.get('raw_value')!=expected['source']['raw_value']
                        or source.get('source_unit')!=expected['source']['source_unit']
                        or source.get('standard_id')!=expected['source']['standard_id']):
                    raise ValueError('报告值、来源或换算与有效配置不一致：'+label)
                return
            try:
                for key in value_path.split('.'):
                    raw=raw[int(key)] if isinstance(raw,list) else raw[key]
            except (ValueError,KeyError,IndexError,TypeError) as e:raise ValueError('真实值的响应路径无效：'+label) from e
            if raw!=item['value']:raise ValueError('填写值与原始平台响应不一致：'+label)
        elif item['status']=='config' and not Path(source.get('file','')).is_file():
            raise ValueError('本站配置来源不存在：'+label)
        elif item['status']=='request' and source.get('request')!=plan['request']:
            raise ValueError('请求字段来源不一致：'+label)
    def check_item(item,fid,period,location='scalar',row_index=None):
        if item.get('status') not in allowed:raise ValueError('未知取数状态：'+fid)
        binding=fixedness.get(fid,{})
        if (binding.get('scope_kind')=='template' and binding.get('active')
                and binding.get('value_kind')=='scalar' and item.get('status')!='fixed'):
            raise ValueError('模板固定位置必须按已确认原文填写：'+fid)
        if item.get('status')=='fixed':
            validate_fixed_item(plan,fid,item,fixedness.get(fid),location,row_index)
            return
        if item.get('status') in trusted:
            check_value(item.get('value'),fid)
            check_context(item,period,fid)
            check_source(item,fid)
            if item.get('unit')!=definitions[fid]['unit']:raise ValueError('字段单位未归一：'+fid)
            definition = definitions[fid]
            if item['status'] == 'real' and not any(definition.get(k) for k in ['source_selection','record_selection','attachment_selection','plan_selection']):
                raise ValueError('真实业务字段没有选定来源：'+fid)
            if item['status'] == 'request':
                position = definition.get('source_position') or {}
                expected = {'year': int(plan['period'][:4]), 'month': int(plan['period'][5:])}.get(position.get('param'))
                if fid=='F052':
                    expected=str(int(period[-2:]))
                    if str(item['value']) not in {expected,'累计','合计'}:raise ValueError('台账请求月份绑定错误')
                elif definition['source_mode'] != 'request' or item['value'] != expected:
                    raise ValueError('请求字段值与年月绑定不一致：'+fid)
            if item['status'] == 'config':
                position = definition.get('source_position') or {}
                source = item['source']
                config = json.loads(Path(plan['station_config']).read_text(encoding='utf-8'))
                key = position.get('property')
                if (definition['source_mode'] != 'station_profile' or not key
                        or Path(source['file']).resolve() != Path(plan['station_config']).resolve()
                        or source.get('pointer') != '/profile/' + key
                        or item['value'] != config.get('profile', {}).get(key)):
                    raise ValueError('配置字段值与本站绑定不一致：'+fid)
            if item['status']=='derived':
                if definition.get('calculation_rule') or definition.get('ledger_total_rule'):
                    if item['source'].get('system')!='模板计算':raise ValueError('计算字段缺少模板计算来源')
                    slot='ledger_total_rule' if location=='ledger_total' else 'calculation_rule'
                    rule=definition.get(slot)
                    permitted={location} if location!='scalar' else {'scalar','ledger_month'}
                    if not rule or rule.get('target') not in permitted:
                        raise ValueError('派生字段位置没有对应计算规则：'+fid)
                elif fid in {'F059','F064'}:
                    system='计划记录序号' if fid=='F059' else '执行记录序号'
                    if item['source'].get('system')!=system or not isinstance(item['value'],int) or isinstance(item['value'],bool) or item['value']<1:
                        raise ValueError('未支持的派生序号')
                else:raise ValueError('未支持的派生字段：'+fid)
    for fid,item in collected.get('fields',{}).items():
        check_item(item,fid,plan['period'])
    for key,group in collected.get('repeat_groups',{}).items():
        if key not in plan['repeat_group_rules']:raise ValueError('模板外循环表：'+key)
        if group.get('status') not in allowed or not isinstance(group.get('records'),list):raise ValueError('循环表结构无效：'+key)
        if group.get('status') in trusted:check_context(group,plan['period'],key)
        fids=set(plan['repeat_group_rules'][key]['field_ids'])
        template_fixed=any(fixedness.get(fid,{}).get('scope_kind')=='template'
                           and fixedness[fid].get('active') for fid in fids)
        if template_fixed and group.get('status')!='fixed':
            raise ValueError('模板固定循环表必须按已确认内容成组填写：'+key)
        fixed_cells = [cell for row in group['records'] for cell in row.values()
                       if isinstance(cell,dict) and cell.get('status')=='fixed']
        if group.get('status')=='fixed' or fixed_cells:
            if (not all(fixedness.get(fid,{}).get('active') for fid in fids)
                    or any(set(row)!=fids for row in group['records'])
                    or any(not isinstance(cell,dict) or cell.get('status')!='fixed'
                           for row in group['records'] for cell in row.values())
                    or any(len(fixedness[fid]['decision']['value'])!=len(group['records']) for fid in fids)):
                raise ValueError('固定循环表没有完整的成组确认：'+key)
        seen_months=set()
        for row_index,row in enumerate(group['records']):
            if not isinstance(row,dict) or not set(row)<=fids:raise ValueError('循环行字段无效：'+key)
            expected_period=plan['period']
            location='record'
            if key=='records.1':
                cell=row.get('F052');month_value=cell.get('value') if isinstance(cell,dict) else cell
                label=str(month_value).strip()
                if label not in ['累计','合计']:
                    match=re.fullmatch(r'(?:(20\d{2})[-年])?(\d{1,2})月?',label)
                    if not match or (match[1] and match[1]!=plan['period'][:4]):raise ValueError('台账年份不匹配')
                    month=int(match[2])
                    if not 1<=month<=int(plan['period'][5:]):raise ValueError('台账月份超出报告范围')
                    expected_period=plan['period'][:4]+f'-{month:02}'
                if label in ['累计','合计']:expected_period=plan['period']
                identity='累计' if label in ['累计','合计'] else expected_period
                location='ledger_total' if label in ['累计','合计'] else 'ledger_month'
                if identity in seen_months:raise ValueError('台账月份重复')
                seen_months.add(identity)
            for fid,value in row.items():
                inherited={k:group.get(k) for k in ['status','station_id','period','source']}
                item={**inherited,'unit':definitions[fid]['unit']}
                item.update(value if isinstance(value,dict) else {'value':value})
                check_item(item,fid,expected_period,location,row_index if item.get('status')=='fixed' else None)
                if fid in {'F059','F064'} and item.get('status')=='derived' and item['value']!=row_index+1:raise ValueError('记录序号不连续')
            if key=='records.2' and isinstance(row.get('F059'),dict) and row['F059'].get('status')=='derived':
                from plan_values import bind_plan_records
                text_item=row.get('F060',{})
                if text_item.get('status')!='real':raise ValueError('计划序号缺少实际计划行')
                source=text_item['source'];raw=json.loads(Path(source['evidence_file']).read_text(encoding='utf-8'))
                expected_rows=bind_plan_records(plan,raw,source,catalog)
                if row_index>=len(expected_rows) or row['F059']['source']!=expected_rows[row_index]['F059']['source']:
                    raise ValueError('计划序号来源与实际计划行不一致')
    snapshot=collected.get('platform_snapshot',{})
    if snapshot.get('items'):
        check_context(snapshot,plan['period'],'平台当前档案')
        if str(snapshot.get('powerplus_station_id'))!=str(plan['bound_parameters']['powerplus_station_id']):raise ValueError('平台档案编码串站')
        if not snapshot.get('captured_at') or not snapshot.get('period_scope'):raise ValueError('当前档案缺少采集时间或范围')
        for item in snapshot['items']:
            if item.get('status')!='real':raise ValueError('平台附页只展示已核验的真实条目')
            check_value(item.get('value'),'平台档案')
            check_context(item,plan['period'],'平台档案')
            check_source(item,'平台档案')
    for observation in collected.get('observations',[]):
        if observation.get('status')=='real':
            source=observation.get('source',{})
            item={**source,**observation}
            check_value(item.get('value'),'平台观测')
            check_context(item,plan['period'],'平台观测')
            check_source(item,'平台观测')
    validate_photos(plan,collected.get('photos',{}))
    if any(f.get('calculation_rule') or f.get('ledger_total_rule') for f in definitions.values()):
        from derived_fields import validate_derivations
        validate_derivations(plan,collected)
    for fid,binding in fixedness.items():
        if (binding.get('scope_kind')=='template' and binding.get('active')
                and binding.get('value_kind')=='scalar' and fid not in collected.get('fields',{})):
            raise ValueError('模板固定位置未返回：'+fid)
    for key,group in plan['repeat_group_rules'].items():
        if (any(fixedness.get(fid,{}).get('scope_kind')=='template' and fixedness[fid].get('active')
                for fid in group['field_ids']) and key not in collected.get('repeat_groups',{})):
            raise ValueError('模板固定循环表未返回：'+key)
    for fid,definition in definitions.items():
        collected.setdefault('fields',{}).setdefault(fid,{
            'status':'missing','value':None,'unit':definition['unit'],'station_id':plan['station_id'],
            'period':plan['period'],'reason':'取数器未返回本字段，保留人工填写占位。'})


def renderer_path():
    packaged=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime/plugins/documents/skills/documents/render_docx.py'
    if packaged.is_file():return packaged
    candidates=list((Path.home()/'.codex/plugins/cache/openai-primary-runtime/documents').glob('*/skills/documents/render_docx.py'))
    if candidates:return max(candidates,key=lambda p:p.stat().st_mtime)
    raise FileNotFoundError('未找到宿主文档渲染器，请先加载工作区依赖。')


def render_report(docx,output_dir):
    """Use the bundled renderer, giving its fontconfig access to installed CJK fonts."""
    output_dir.mkdir(parents=True,exist_ok=True)
    conf=output_dir/'fonts.conf'
    directories=[p for p in ['/Library/Fonts','/System/Library/Fonts','/System/Library/Fonts/Supplemental','/usr/share/fonts'] if Path(p).is_dir()]
    aliases={'宋体':'Songti SC','SimSun':'Songti SC','黑体':'Heiti SC','SimHei':'Heiti SC','仿宋':'STFangsong','楷体':'Kaiti SC','KaiTi':'Kaiti SC'} if sys.platform=='darwin' else {}
    alias_xml=''.join('<alias><family>'+escape(a)+'</family><prefer><family>'+escape(b)+'</family></prefer></alias>' for a,b in aliases.items())
    conf.write_text('<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'+
        ''.join('<dir>'+escape(p)+'</dir>' for p in directories)+alias_xml+'<cachedir>'+escape(str(output_dir/'fontcache'))+'</cachedir></fontconfig>',encoding='utf-8')
    env=os.environ.copy();env['FONTCONFIG_FILE']=str(conf.resolve())
    run=subprocess.run([sys.executable,str(renderer_path()),str(docx),'--output_dir',str(output_dir),'--emit_pdf'],
        capture_output=True,text=True,env=env,timeout=180)
    (output_dir/'render.log').write_text(run.stdout+'\n'+run.stderr,encoding='utf-8')
    pages=sorted(output_dir.glob('page-*.png'),key=lambda p:int(p.stem.split('-')[-1]))
    if run.returncode or not pages:raise RuntimeError('DOCX渲染未成功，查看预览/render.log。')
    return {'page_count':len(pages),'pages':[str(p.resolve()) for p in pages],
        'pdf':str((output_dir/(docx.stem+'.pdf')).resolve()),'visual_review':'pending'}


def generate(request,output_dir=None,profile='me',render=True,station_id=None,period=None,template_id=None,allow_pending=False,offline=False):
    from power_source import collect
    from fill_report import fill,TOKEN,sync_toc_pages
    plan=resolve(request,station_id=station_id,period=period,mode='powerplus',template_id=template_id,allow_pending=allow_pending)
    public_cfg_path=plan.get('output_template',{}).get('station_config',plan['station_config'])
    public_cfg=json.loads(Path(public_cfg_path).read_text())
    base_identity=json.loads(Path(plan['station_base_file']).read_text())
    if not offline and (public_cfg.get('readiness',{}).get('simulation') or base_identity.get('onboarding',{}).get('simulation')):
        raise ValueError('合成电站只允许onboard trial --offline，不向平台提交合成身份查询')
    stamp=dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    destination=Path(output_dir).resolve() if output_dir else ROOT/'输出'/f"{plan['station_id']}_{plan['period']}_{stamp}_{uuid.uuid4().hex[:6]}"
    destination.mkdir(parents=True,exist_ok=False)
    record={'request':request,'station_id':plan['station_id'],'period':plan['period'],'template_id':plan['template_id'],
        'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'status':'collecting','formal_report_ready':False}
    output_template=plan.get('output_template')
    if output_template:
        record['source_template_id']=plan['template_id']
        record['template_id']=output_template['template_id']
        record['executor']='monthly_mapped_v1'
    write_json(destination/'运行记录.json',record)
    try:
        evidence=destination/'取数证据';evidence.mkdir()
        if offline:
            from onboarding_offline import collect_missing
            collected=collect_missing(plan)
            record['offline_simulation']=True
            record['network_calls']=0
        else:
            collected=collect(plan,evidence,profile=profile)
        apply_guidance(plan,collected)
        validate_collection(plan,collected)
        write_json(destination/'取数结果.json',collected)
        if not output_template:
            record['pending_source_guide']=write_pending_guide(plan,collected,destination/'缺项与来源指引.md')
        docx=destination/f"{plan['station_name']}_{plan['period']}_运维月报_待填版.docx"
        if output_template:
            from mapped_template import fill_mapped
            new_mapping=json.loads(Path(output_template['mapping_file']).read_text())
            new_rules=json.loads(Path(output_template['rules_file']).read_text())
            mapped=fill_mapped(plan,collected,Path(output_template['template_file']),new_mapping,new_rules,docx)
            fill_result={**mapped,'photos':{'embedded':0,'status':'manual_pending'},
                         'real_value_fields':mapped.get('real_value_fields',[])}
            write_json(destination/'报告字段取数结果.json',mapped)
            lines=['# 缺项与来源说明','',f"本次电站：{plan['station_name']}；期间：{plan['period']}。",
                   '字段按新模板明确映射到已核公共定义；实际值和来源保留在取数结果及报告字段取数结果中。',
                   '照片仍须按本站当期材料补齐，不复制原报告图片。','']
            for fid in mapped.get('pending_fields',mapped.get('missing_fields',[])):
                lines.append('- '+new_rules['fields'][fid]['filling_rule'].get('label',fid)+'：待填，查看本次取数记录。')
            (destination/'缺项与来源指引.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
            record['pending_source_guide']=str(destination/'缺项与来源指引.md')
        else:
            fill_result=fill(plan,collected,docx)
        with zipfile.ZipFile(docx) as z:
            from lxml import etree
            text='\n'.join(''.join(etree.fromstring(z.read(n)).itertext()) for n in z.namelist()
                if n.startswith('word/') and n.endswith('.xml') and n.split('/')[-1].startswith(('document','header','footer')))
        if not output_template and TOKEN.search(text):raise ValueError('DOCX仍含未处理的模板字段编号')
        config_paths={plan['station_config'],plan['field_config'],plan['mapping_file'],plan['template_rules_file'],plan['maintenance_rules']}
        if output_template:
            config_paths.update(output_template[k] for k in ['station_config','mapping_file','rules_file'])
        config_paths.update(f['dictionary_file'] for f in plan['fields'])
        if plan.get('source_catalog_file'):config_paths.add(plan['source_catalog_file'])
        config_paths.add(plan['station_base_file'])
        if plan.get('source_guide'):config_paths.add(plan['source_guide']['path'])
        for key in ['field_model_validation_file','meter_policy_file']:
            if plan.get(key):config_paths.add(plan[key])
        config_paths.update(f['source_selection']['validation_file'] for f in plan['fields'] if f.get('source_selection'))
        record.update({'status':'generated','docx':str(docx),'docx_sha256':hashlib.sha256(docx.read_bytes()).hexdigest(),
            'configuration_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sorted(config_paths)},
            'field_status_counts':dict(Counter(v['status'] for v in collected['fields'].values())),
            'platform_snapshot_items':len(collected.get('platform_snapshot',{}).get('items',[])),
            'real_business_fields':fill_result['real_value_fields'],
            'embedded_photos':fill_result['photos']['embedded'],
            'work_record_rows':len(collected['repeat_groups'].get('records.3',{}).get('records',[])),
            'fill_result':fill_result,'historical_report_used':False,'human_completion_required':True})
        from fixed_fields import fresh_bindings
        record['template_fixed_fields']=sorted(fid for fid,binding in fresh_bindings(plan).items()
            if binding.get('scope_kind')=='template' and binding.get('active'))
        record['template_fixed_field_count']=len(record['template_fixed_fields'])
        if output_template:
            record['source_field_status_counts']=record['field_status_counts']
            record['field_status_counts']=dict(Counter(v['status'] for v in mapped['field_results'].values()))
            record['output_field_count']=len(new_rules['fields'])
            record['record_counts']=mapped.get('record_counts',{})
        if render:
            record['render']=render_report(docx,destination/'预览')
            record['toc_page_update']=sync_toc_pages(docx,record['render']['pdf'])
            if record['toc_page_update'].get('updated'):
                shutil.rmtree(destination/'预览')
                record['render']=render_report(docx,destination/'预览')
                stable=sync_toc_pages(docx,record['render']['pdf'])
                if stable.get('updated'):raise RuntimeError('目录页码更新后分页仍变化，需继续检查版式。')
            record['docx_sha256']=hashlib.sha256(docx.read_bytes()).hexdigest()
            record['status']='rendered_pending_visual_review'
        else:record['render']={'visual_review':'not_rendered'}
        write_json(destination/'运行记录.json',record)
        return record
    except Exception as exc:
        record.update({'status':'failed','error':str(exc),'error_type':type(exc).__name__})
        write_json(destination/'运行记录.json',record)
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request',required=True)
    p.add_argument('--station-id');p.add_argument('--period')
    p.add_argument('--profile',default='me')
    p.add_argument('--out',type=Path)
    p.add_argument('--no-render',action='store_true',help='仅供结构测试；未经渲染审阅不作最终交付')
    a=p.parse_args()
    try:
        result=generate(a.request,a.out,a.profile,not a.no_render,a.station_id,a.period)
    except (ValueError,OSError,RuntimeError,KeyError,subprocess.TimeoutExpired) as e:
        p.exit(2,'生成未完成：'+str(e)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
