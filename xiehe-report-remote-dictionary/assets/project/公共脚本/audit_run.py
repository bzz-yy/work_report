"""Summarize actual report slots, including repeated rows and photos; never query data."""
from pathlib import Path
import csv, json, hashlib
from collections import Counter

TRUSTED={'real','request','config','derived','fixed'}
def audit_run(run, out):
    run,out=Path(run).resolve(),Path(out).resolve()
    data=json.loads((run/'取数结果.json').read_text()); record=json.loads((run/'运行记录.json').read_text())
    from fixed_fields import validate_run_fixed_content
    template_fixed_fields=validate_run_fixed_content(record,data)
    source_path=run/'取数结果.json'
    csv_name='90字段测试明细.csv'
    if record.get('executor')=='monthly_mapped_v1':
        source_path=run/'报告字段取数结果.json'
        mapped=json.loads(source_path.read_text())
        data={'station_id':record['station_id'],'period':record['period'],'fields':mapped['field_results'],
              'repeat_groups':{key:{'records':values[:mapped['record_counts'][key]]}
                               for key,values in mapped.get('record_results',{}).items()}}
        csv_name='报告字段测试明细.csv'
    pending=set(record['fill_result']['pending_fields'])
    photos=record.get('embedded_photos',0)
    rows=[]
    for fid,field in data['fields'].items():
        instances=[]
        for group in data.get('repeat_groups',{}).values():
            for row in group.get('records',[]):
                if fid in row:instances.append(row[fid])
        if not instances:instances=[field]
        available=[x for x in instances if x.get('status') in TRUSTED and x.get('value') is not None and (x.get('value')!='' or x.get('status')=='fixed')]
        real=any(x.get('status')=='real' for x in available) or (fid=='F090' and photos>0)
        has=bool(available) or (fid=='F090' and photos>0)
        state='部分已填' if has and fid in pending else '已填' if has else '待填'
        values=[str(x['value']) for x in available]
        detail='；'.join(values)
        if fid=='F090' and photos:detail=f'{photos}张已嵌入照片'
        reason=field.get('runtime_reason') or field.get('reason','')
        if state=='已填':reason='已填写当前取得的记录；不据此认定全集完整。' if instances!=[field] or fid=='F090' else '当前位置已填。'
        if state=='部分已填':reason='部分月份或合计缺失；保留已取得值，不能算全项齐备。'
        rows.append({'字段编号':fid,'字段名称':field.get('label',fid),'填写状态':state,'来源类别':'真实业务内容' if real else '已确认模板固定值' if any(x.get('status')=='fixed' for x in available) else '请求/配置/派生序号' if has else '待核查/待补资料','已填内容':detail,'缺项或范围说明':reason,'原始顶层状态':field.get('status'),'有效记录数':len(available) if fid!='F090' else photos})
    counts=Counter(r['填写状态'] for r in rows)
    summary={'station_id':data['station_id'],'period':data['period'],'source_run':str(run),'source_sha256':hashlib.sha256(source_path.read_bytes()).hexdigest(),'total_fields':len(rows),'counts':dict(counts),'fields_with_real_content':sum(r['来源类别']=='真实业务内容' for r in rows),'fully_unfilled_fields':[r['字段编号'] for r in rows if r['填写状态']=='待填'],'partial_fields':[r['字段编号'] for r in rows if r['填写状态']=='部分已填'],'business_complete':False,'note':'按最终填充记录和集合内容统计；已填不证明完整业务集合。audit仅复核指定运行，不另外发起线上查询；来源时间以该次运行记录为准。'}
    out.mkdir(parents=True,exist_ok=False)
    summary['template_fixed_fields']=template_fixed_fields
    summary['template_fixed_field_count']=len(template_fixed_fields)
    (out/'缺项统计.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    with (out/csv_name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    plan_rows=len(data.get('repeat_groups',{}).get('records.2',{}).get('records',[]))
    work_rows=len(data.get('repeat_groups',{}).get('records.3',{}).get('records',[]))
    lines=[f"# {data['station_id']} {data['period']} 测试缺项复核",'',f"共{len(rows)}个填写字段：已填{counts['已填']}，部分已填{counts['部分已填']}，完全待填{counts['待填']}。",'',f"其中{summary['fields_with_real_content']}个字段包含真实业务内容；其余已填项来自请求、配置或序号。",'', f'已填集合：{plan_rows}条计划、{work_rows}条工作记录、{photos}张照片。只表示本次取得范围；计划下发不等于完成，已填记录不证明全集齐备。','',f'原始测试目录：{run}','', f'[逐字段明细]({csv_name})','', '| 字段 | 名称 | 状态 | 缺项或范围说明 |','|---|---|---|---|']
    lines[4]=lines[4].replace('其余已填项来自请求、配置或序号。',f'另有{len(template_fixed_fields)}个用户确认的模板固定位置；其他已填项来自请求、配置或序号。')
    for r in rows:lines.append('| '+' | '.join(str(r[k]).replace('|','／').replace('\n',' ') for k in ['字段编号','字段名称','填写状态','缺项或范围说明'])+' |')
    (out/'测试缺项说明.md').write_text('\n'.join(lines)+'\n')
    return summary
