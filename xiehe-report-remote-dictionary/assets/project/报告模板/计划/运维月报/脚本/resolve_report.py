"""Resolve a station request to the shared template and field-source plan.

This command is read-only unless --out is given. It does not fetch live data or
claim that an unresolved report is ready for formal delivery.
"""
from pathlib import Path
import argparse,csv,hashlib,json,re,sys

ROOT=Path(__file__).resolve().parents[1]
PROJECT=next(p for p in ROOT.parents if (p/'报告索引.csv').is_file())
sys.path.insert(0,str(PROJECT/'公共脚本'))
from report_rules import load_bundle
from station_config import load_station_config
from request_period import monthly_period
class ResolutionError(ValueError):pass
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def local(base,value):
    p=(base/value).resolve()
    if not p.is_relative_to(ROOT):raise ResolutionError('配置路径越出运维月报目录')
    if not p.is_file():raise ResolutionError('配置文件不存在：'+str(p))
    return p
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def resolve(request,station_id=None,period=None,mode='local_history_replay',template_id=None,allow_pending=False):
    if mode not in ['local_history_replay','powerplus']:raise ResolutionError('不支持的取数方式')
    if any(w in request for w in ['定检','清洗报告','周报','年报','季报']):raise ResolutionError('本入口只处理运维月报')
    if '月报' not in request:raise ResolutionError('请明确报告类型为运维月报')
    from report_route import route_request
    routed=route_request(PROJECT,request,station_id,period,'运维月报',template_id,allow_pending)
    if routed['route']!='generate':raise ResolutionError('需接入或澄清：'+routed['reason'])
    row=routed['index_row']
    cfgpath=local(ROOT,row['电站配置']);cfg=load_station_config(cfgpath,read)
    if cfg.get('readiness',{}).get('executor')=='monthly_mapped_v1':
        output_mapping_path=local(cfgpath.parent,cfg['template_mapping'])
        output_mapping=read(output_mapping_path)
        output_rules_path=local(output_mapping_path.parent,output_mapping['retrieval_rules'])
        output_rules=read(output_rules_path)
        source_cfgpath=local(cfgpath.parent,cfg['source_adapter_config'])
        if not source_cfgpath.is_relative_to(cfgpath.parent):
            raise ResolutionError('取数适配必须属于本关联，不能借同站其他版本的批准和参数')
        if ('profile' in cfg or 'profile_evidence' in cfg
                or cfg.get('profile_ref')!=cfg['source_adapter_config']+'#/profile'):
            raise ResolutionError('映射报告名称只维护取数适配中的profile，外层须使用profile_ref')
        source_cfg=load_station_config(source_cfgpath,read)
        base_mapping_path=local(source_cfgpath.parent,source_cfg['template_mapping'])
        base_mapping=read(base_mapping_path)
        base_rules=read(local(base_mapping_path.parent,base_mapping['retrieval_rules']))
        from mapped_template import validate_adapter
        validate_adapter(output_rules,output_mapping,base_rules,base_mapping)
        if source_cfg['station_id']!=cfg['station_id'] or source_cfg['template_id']!='NW-MONTHLY-STD-01':
            raise ResolutionError('新模板取数适配串站或引用了未支持基底')
        source_row={'电站编码':cfg['station_id'],'模板编号':'NW-MONTHLY-STD-01',
            '电站配置':str(source_cfgpath.relative_to(ROOT)),
            '模板文件':str((base_mapping_path.parent/base_mapping['template_file']).relative_to(ROOT))}
        result=_resolve_row(request,source_row,period,mode,output_template_id=cfg['template_id'])
        result['output_template']={'template_id':cfg['template_id'],'station_config':str(cfgpath),
            'template_file':str(local(output_mapping_path.parent,output_mapping['template_file'])),
            'mapping_file':str(output_mapping_path),'rules_file':str(output_rules_path)}
        return result
    return _resolve_row(request,row,period,mode)


def _resolve_row(request,row,period,mode,output_template_id=None):
    try:period=monthly_period(request,period)
    except ValueError as e:raise ResolutionError(str(e)) from e
    config_path=local(ROOT,row['电站配置']);config=load_station_config(config_path, read)
    if config['station_id']!=row['电站编码'] or config['template_id']!=row['模板编号']:raise ResolutionError('索引与电站配置不一致')
    map_path=local(config_path.parent,config['template_mapping']);mapping=read(map_path)
    template_path=local(map_path.parent,mapping['template_file'])
    if template_path!=local(ROOT,row['模板文件']) or mapping['template_id']!=row['模板编号']:raise ResolutionError('模板关联不一致')
    if digest(template_path)!=mapping['template_sha256']:raise ResolutionError('模板摘要变化，请更新映射版本并核验')
    fields_path=local(config_path.parent,config['field_configuration'])
    try:bundle=load_bundle(ROOT,config_path,map_path,mapping['template_id'],period,output_template_id)
    except ValueError as e:raise ResolutionError(str(e)) from e
    fields=bundle['field_config']
    if fields['station_id']!=config['station_id'] or fields['template_id']!=mapping['template_id']:raise ResolutionError('字段配置串站或串模板')
    maintenance_entry=local(config_path.parent,config['maintenance_entry'])
    maintenance_path=local(config_path.parent,config['maintenance_rules']);maintenance=bundle['maintenance']
    if maintenance['station_id']!=config['station_id'] or maintenance['template_id']!=mapping['template_id']:raise ResolutionError('维护规则串站或串模板')
    if set(maintenance['fields'])!={f['id'] for f in mapping['fields']}:raise ResolutionError('维护规则未覆盖全部模板字段')
    if set(maintenance['repeat_groups'])!={g['key'] for g in mapping['repeat_groups']}:raise ResolutionError('循环表维护规则不完整')
    ids=[f['field_id'] for f in fields['fields']]
    if len(ids)!=len(set(ids)) or set(ids)!={f['id'] for f in mapping['fields']}:raise ResolutionError('字段覆盖不完整或有重复')
    source=None
    if mode=='local_history_replay':
        source_index=read(local(config_path.parent,config['local_sources']))
        if source_index['station_id']!=config['station_id']:raise ResolutionError('本地资料索引串站')
        sources=[s for s in source_index['sources'] if s['period']==period]
        if len(sources)>1:raise ResolutionError('同站同月来源不唯一')
        source=sources[0] if sources else None
        if source:
            source_path=Path(source['path'])
            if not source_path.is_file() or digest(source_path)!=source['sha256']:raise ResolutionError('本站当月资料不存在或摘要变化')
    plan=[]
    for f in fields['fields']:
        if f['station_id']!=config['station_id']:raise ResolutionError('字段选择条件串站')
        rule_file,rule_pointer=f['maintenance_rule_ref'].split('#',1)
        if local(fields_path.parent,rule_file)!=maintenance_path or rule_pointer!='/fields/'+f['field_id']:raise ResolutionError('本站字段维护规则引用错位')
        file,pointer=f['dictionary_ref'].split('#',1);dictionary_path=local(fields_path.parent,file);dictionary=read(dictionary_path)
        if dictionary['dictionary_id']!=fields['dictionary_id']:raise ResolutionError('字典编号不一致')
        if pointer!='/fields/'+f['field_id']:raise ResolutionError('字典字段引用错位')
        definition=dictionary['fields'].get(f['field_id'])
        if not definition:raise ResolutionError('字典字段不存在')
        if f['station_override'] is not None:raise ResolutionError('尚未实现覆盖项执行，请先核验并扩展解析器')
        status='local_position_available'
        if f['source_mode']=='pending_business_rule':status='business_rule_pending'
        elif f['source_mode']=='independent_photos':status='independent_photo_source_pending'
        elif f['source_mode'] in ['local_history_replay','manual'] and mode=='powerplus':status='powerplus_not_configured'
        elif f['source_mode'] in ['local_history_replay','manual'] and not source:status='monthly_source_missing'
        plan.append({'field_id':f['field_id'],'label':definition['label'],'unit':definition['unit'],'station_id':config['station_id'],'period':period,'dictionary_file':str(dictionary_path),'dictionary_pointer':pointer,'maintenance_rule_file':str(maintenance_path),'maintenance_rule_pointer':rule_pointer,'source_mode':f['source_mode'],'query_method':f.get('query_method'),'source_position':definition['powerplus'] if mode=='powerplus' and f['source_mode']=='local_history_replay' else definition['local'],'status':status})
    for item in plan:
        item['template_rule_file']=bundle['template_rules_file']
        item['template_rule_pointer']='/fields/'+item['field_id']
        item['effective_filling_rule']=bundle['maintenance']['fields'][item['field_id']]
        effective=next(f for f in fields['fields'] if f['field_id']==item['field_id'])
        item['source_catalog_candidates']=effective.get('source_catalog_candidates')
        item['report_source_assessments']=effective.get('report_source_assessments',[])
        item['source_selection']=effective.get('source_selection')
        item['record_selection']=effective.get('record_selection') if item['source_mode']=='record_collection' else None
        item['photo_selection']=effective.get('photo_selection') if item['source_mode']=='independent_photos' else None
        for key in ['field_model','calculation_rule','ledger_total_rule','attachment_selection','plan_selection','fixedness']:
            item[key]=effective.get(key)
        if mode=='powerplus' and item['source_mode']=='catalog':
            item['source_position']={'kind':'catalog','mapping_id':item['source_selection']['mapping_id']}
            item['status']='source_selected_not_collected'
    extra={key:bundle.get(key) for key in ['field_model_validation_file','meter_policy_file','plan_source_policy','calculation_adoptions']}
    extra['template_rules_sha256']=digest(Path(bundle['template_rules_file']))
    return {**extra,'source_guide':bundle.get('source_guide'),'photo_selection':next((f.get('photo_selection') for f in plan if f['field_id']=='F090'),None),'source_controls':bundle['source_controls'],'station_base_file':bundle['station_base_file'],'query_methods':bundle['query_methods'],'source_catalog_file':bundle['source_catalog_file'],'mapping_file':str(map_path),'template_rules_file':bundle['template_rules_file'],'bound_parameters':bundle['bound_parameters'],'effective_filling_rules':bundle['maintenance'],'status':'template_and_fields_resolved','report_type':'运维月报','request':request,'station_id':config['station_id'],'station_name':config['station_name'],'period':period,'data_mode':mode,'template_id':mapping['template_id'],'template_file':str(template_path),'template_sha256':mapping['template_sha256'],'station_config':str(config_path),'field_config':str(fields_path),'maintenance_entry':str(maintenance_entry),'maintenance_rules':str(maintenance_path),'repeat_group_rules':maintenance['repeat_groups'],'local_source':source if mode=='local_history_replay' else None,'fields':plan,'pending_fields':[f['field_id'] for f in plan if f['status']!='local_position_available'],'pending_delivery_rules':mapping['pending_rules'],'formal_report_ready':False,'next_action':'先读本站AGENTS.md和填写规则，按字典取当期数据、按循环规则增减行并核实缺项；此命令只定位和提供规则，尚未执行最终填充'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--request',required=True);p.add_argument('--station-id');p.add_argument('--period');p.add_argument('--mode',default='local_history_replay',choices=['local_history_replay','powerplus']);p.add_argument('--out',type=Path)
    a=p.parse_args()
    try:result=resolve(a.request,a.station_id,a.period,a.mode)
    except (ResolutionError,KeyError,ValueError) as e:p.exit(2,'无法定位：'+str(e)+'\n')
    rendered=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if a.out:
        a.out.parent.mkdir(parents=True,exist_ok=True)
        with a.out.open('x',encoding='utf-8') as f:f.write(rendered)
        print(str(a.out.resolve()))
    else:print(rendered)
if __name__=='__main__':main()
