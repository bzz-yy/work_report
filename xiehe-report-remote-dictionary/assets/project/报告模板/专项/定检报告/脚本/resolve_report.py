"""Resolve report/station/period/version and expose its exact module and field plan."""
import argparse
from common import *
import sys
sys.path.insert(0,str(PROJECT/'公共脚本'))
from report_rules import load_bundle
from station_config import load_station_config

class ResolutionError(ValueError):pass
def local(base,rel):
    p=(base/rel).resolve()
    if not p.is_relative_to(ROOT) or not p.is_file():raise ResolutionError('配置引用无效：'+str(p))
    return p
def resolve(request,station_id=None,period=None,template_id=None,mode='local_history_replay'):
    if mode not in ['local_history_replay','powerplus']:raise ResolutionError('取数方式不支持')
    if '定检' not in request or any(k in request for k in ['清洗报告','运维月报','巡检报告']):raise ResolutionError('本入口只处理定检报告')
    with (ROOT/'模板电站索引.csv').open(encoding='utf-8-sig',newline='') as f:index=list(csv.DictReader(f))
    found={row['电站编码'] for row in index if any(a and a.lower() in request.lower() for a in load_station_config(ROOT/row['电站配置'])['aliases'])}
    if len(found)>1:raise ResolutionError('请求包含多个电站')
    if station_id and found and found!={station_id}:raise ResolutionError('电站参数与自然语言冲突')
    station_id=station_id or next(iter(found),None)
    rows=[r for r in index if r['电站编码']==station_id]
    if not rows:raise ResolutionError('电站未登记，禁止使用相似站替代')
    cfgpath=local(ROOT,rows[0]['电站配置']);cfg=load_station_config(cfgpath, read)
    if cfg['station_id']!=station_id:raise ResolutionError('索引与电站配置不一致')
    dates={f'{y}-{dict(上半年="H1",下半年="H2",全年="Y")[s]}' for y,s in re.findall(r'(20\d{2})\s*(?:年度|年)?\s*(上半年|下半年|全年)',request)}
    dates|={f'{y}-{s.upper()}' for y,s in re.findall(r'(20\d{2})-(H[12]|Y)\b',request,re.I)}
    if len(dates)>1 or (period and dates and dates!={period}):raise ResolutionError('报告期间冲突')
    period=period or next(iter(dates),None)
    if not period or not re.fullmatch(r'20\d{2}-(H1|H2|Y)',period):raise ResolutionError('请明确报告年份及上半年/下半年/全年')
    explicit={m.upper() for m in re.findall(r'DJ-0[12]',request,re.I)}
    if any(w in request for w in ['详细版','检查标准','标准＋结果','标准+结果']):explicit.add('DJ-02')
    if '结果版' in request and not any(w in request for w in ['标准＋结果版','标准+结果版']):explicit.add('DJ-01')
    if len(explicit)>1 or (template_id and explicit and explicit!={template_id}):raise ResolutionError('模板版本表达冲突')
    template_id=template_id or next(iter(explicit),None)
    if not template_id:
        if len(cfg['profiles'])>1:raise ResolutionError('济柴有结果版DJ-01和详细版DJ-02，请明确交付版本，不能自动选择')
        template_id=next(iter(cfg['profiles']))
    if template_id not in cfg['profiles']:raise ResolutionError('本站未登记所要求的模板版本')
    profile=cfg['profiles'][template_id];matching=[r for r in rows if r['模板编号']==template_id]
    if len(matching)!=1:raise ResolutionError('模板关联不唯一')
    template=local(ROOT,matching[0]['模板文件']);mappath=local(template.parent,'模板字段映射.json');mapping=read(mappath)
    if mapping['template_id']!=template_id or digest(template)!=mapping['sha256']:raise ResolutionError('模板版本或摘要不一致')
    if not set(profile['equipment_modules'])<=set(mapping['equipment_modules']):raise ResolutionError('本站使用了模板不支持的设备模块')
    if len(set(profile['equipment_modules']))!=len(profile['equipment_modules']):raise ResolutionError('设备模块重复')
    if set(profile['signature_order'])!={'定检员','编制人','审核人','批准人'} or len(profile['signature_order'])!=4:raise ResolutionError('签字角色顺序无效')
    try:bundle=load_bundle(ROOT,cfgpath,mappath,template_id,period)
    except ValueError as e:raise ResolutionError(str(e)) from e
    maintenance_path=local(cfgpath.parent,cfg['maintenance_rules']);maintenance=bundle['maintenance']
    if maintenance['station_id']!=station_id or template_id not in maintenance['template_ids']:raise ResolutionError('本站维护规则串站或串模板')
    entry=local(cfgpath.parent,cfg['maintenance_entry']);fcpath=local(cfgpath.parent,cfg['field_configuration']);fc=bundle['field_config']
    if fc['station_id']!=station_id:raise ResolutionError('字段配置串站')
    fields=fc['profiles'][template_id]['fields'];ids=[f['field_id'] for f in fields]
    required=set(mapping['field_ids'])-({'J040','J041','J042'} if not profile['include_tickets'] else set())
    if len(ids)!=len(set(ids)) or set(ids)!=required:raise ResolutionError('模板适用字段覆盖不完整或重复')
    source_index=read(local(cfgpath.parent,cfg['source_index']))
    if source_index['station_id']!=station_id:raise ResolutionError('来源索引串站')
    sources=[s for s in source_index['sources'] if s['template_id']==template_id and s['period']==period]
    if len(sources)>1:raise ResolutionError('同站同版本同周期来源不唯一')
    source=sources[0] if sources else None
    if mode=='local_history_replay' and source and (not Path(source['path']).is_file() or digest(source['path'])!=source['sha256']):raise ResolutionError('历史来源文件已变化或不存在')
    clause_path=None;clause_status='not_applicable'
    if template_id=='DJ-02':
        if not profile['clause_file']:raise ResolutionError('详细版缺少条款版本')
        clause_path=local(cfgpath.parent,profile['clause_file']);clauses=read(clause_path)
        if clauses['station_id']!=station_id or clauses['clause_version']!=profile['clause_version']:raise ResolutionError('条款串站或版本不一致')
        if set(clauses['blocks'])!=set(mapping['clause_modules']):raise ResolutionError('条款模块不完整')
        clause_status='historical_period_matched_business_pending' if clauses['observed_period']==period else 'new_period_applicability_pending'
    plan=[]
    for f in fields:
        if f['station_id']!=station_id:raise ResolutionError('字段筛选条件串站')
        if f['source_override'] is not None:raise ResolutionError('覆盖取数规则尚未执行支持，不能忽略；需先实现并验证')
        fn,pointer=f['dictionary_ref'].split('#');dp=local(fcpath.parent,fn);dic=read(dp)
        if dic['dictionary_id']!=fc['dictionary_id'] or pointer!='/fields/'+f['field_id']:raise ResolutionError('字典引用错位')
        field=dic['fields'][f['field_id']]
        fn,rp=f['maintenance_rule_ref'].split('#')
        if local(fcpath.parent,fn)!=maintenance_path or rp!='/fields/'+f['field_id'] or f['field_id'] not in maintenance['fields']:raise ResolutionError('维护规则引用错位')
        status='local_position_available'
        if field['kind']=='signature':status='manual_signature_required'
        elif f['field_id'] in ['J038','J042']:status='independent_source_not_configured'
        elif field['kind'] not in ['request','station'] and mode=='powerplus':status='powerplus_not_configured'
        elif field['kind'] not in ['request','station'] and not source:status='period_source_missing'
        plan.append({'field_id':f['field_id'],'label':field['label'],'unit':field['unit'],'station_id':station_id,'period':period,'module_scope':f['module_scope'],'dictionary_file':str(dp),'dictionary_pointer':pointer,'maintenance_rule_file':str(maintenance_path),'maintenance_rule_pointer':rp,'source_position':field['local'] if mode=='local_history_replay' else field['powerplus'],'status':status})
    for item in plan:
        item['template_rule_file']=bundle['template_rules_file']
        item['template_rule_pointer']='/fields/'+item['field_id']
        item['effective_filling_rule']=bundle['maintenance']['fields'][item['field_id']]
    return {'template_rules_file':bundle['template_rules_file'],'bound_parameters':bundle['bound_parameters'],'effective_filling_rules':bundle['maintenance'],'status':'template_profile_and_fields_resolved','report_type':'定检报告','request':request,'station_id':station_id,'station_name':cfg['station_name'],'period':period,'template_id':template_id,'template_file':str(template),'template_sha256':mapping['sha256'],'mapping_file':str(mappath),'station_config':str(cfgpath),'maintenance_entry':str(entry),'maintenance_rules':str(maintenance_path),'profile':profile,'clause_file':str(clause_path) if clause_path else None,'clause_status':clause_status,'local_source':source if mode=='local_history_replay' else None,'fields':plan,'pending_fields':[f['field_id'] for f in plan if f['status']!='local_position_available'],'data_mode':mode,'formal_report_ready':False,'next_action':'可组装本站版本的空白结构；正式报告仍需实际取数、签认照片、填充及业务验收'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--request',required=True);p.add_argument('--station-id');p.add_argument('--period');p.add_argument('--template-id',choices=['DJ-01','DJ-02']);p.add_argument('--mode',choices=['local_history_replay','powerplus'],default='local_history_replay');p.add_argument('--out',type=Path);a=p.parse_args()
    try:v=resolve(a.request,a.station_id,a.period,a.template_id,a.mode)
    except (ResolutionError,KeyError,ValueError) as e:p.exit(2,'无法定位：'+str(e)+'\n')
    s=json.dumps(v,ensure_ascii=False,indent=2)+'\n'
    if a.out:
        a.out.parent.mkdir(parents=True,exist_ok=True)
        with a.out.open('x',encoding='utf-8') as f:f.write(s)
        print(str(a.out.resolve()))
    else:print(s)
if __name__=='__main__':main()
