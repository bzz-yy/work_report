"""Carry source availability and manual lookup locations without supplying values."""
from copy import deepcopy
import json
from pathlib import Path


def load_guide(path,station_id,station_code,period,field_ids,*,template_id=None,catalog_guides=None):
    guide=json.loads(Path(path).read_text(encoding='utf-8'))
    if guide.get('schema_version')!=1 or (template_id and guide.get('template_id')!=template_id):
        raise ValueError('来源指南版本或模板关联错误')
    if guide.get('station_id')!=station_id or str(guide.get('powerplus_station_id'))!=str(station_code):
        raise ValueError('来源指南串站')
    reviews=guide.get('period_reviews',{}).get(period,{})
    if reviews and set(reviews)!=set(field_ids):raise ValueError('来源指南未完整覆盖本报告字段')
    for fid,review in reviews.items():
        if review.get('field_id')!=fid or review.get('auto_fill_from_guide') is not False:
            raise ValueError('来源指南不能直接提供自动填充值')
        if any(key in review for key in ['value','historical_value','platform_result']):
            raise ValueError('来源指南禁止携带可被误用的原报告数值')
        if catalog_guides is not None and not set(review.get('lookup_guide_ids',[]))<=set(catalog_guides):
            raise ValueError('来源指南引用了不存在的字典查找方法')
    return {'path':str(path),'reviews':reviews,'online_files':guide.get('online_files',{}),
            'catalog_guides':deepcopy(catalog_guides or {})}


def apply_guidance(plan,collected):
    guide=plan.get('source_guide') or {};reviews=guide.get('reviews',{})
    for fid,model in guide.get('general_models',{}).items():
        item=collected.get('fields',{}).get(fid)
        if item is None:continue
        item['field_model']=deepcopy(model)
        item['lookup_methods']={gid:deepcopy(guide.get('catalog_guides',{}).get(gid,{})) for gid in model['lookup_guide_ids']}
        if item.get('status') in {'manual','missing'} and item.get('reason','').startswith('尚无经确认的当期取数方法'):
            item['reason']='已登记取数或计算方法，当前所需输入尚未齐备；'+model['missing_policy']
    if guide.get('general_models'):collected['source_guide_file']=guide['path']
    if not reviews:return
    has_work=bool(collected.get('repeat_groups',{}).get('records.3',{}).get('records'))
    for fid,review in reviews.items():
        item=collected.get('fields',{}).get(fid)
        if item is None:continue
        item['source_review']={k:deepcopy(review[k]) for k in ['review_status','reason','lookup_guide_ids','manual_locations','document_status','document_reason'] if k in review}
        item['source_review']['guide_file']=guide['path']
        if item.get('status') in {'real','request','config','derived','fixed'}:continue
        if fid=='F090' and collected.get('photos',{}).get('items'):continue
        if fid in {'F064','F065','F067'} and has_work:continue
        item.setdefault('runtime_reason',item.get('reason'))
        item['reason']=review['reason']
        # This is a source-status supplement; never change value or make a field real.
    collected['source_guide_file']=guide['path']


def write_pending_guide(plan,collected,path):
    guide=plan.get('source_guide') or {}
    if not guide.get('reviews') and not guide.get('general_models'):return None
    lines=['# 缺项与线上来源指引','',f"电站：{plan['station_name']}；报告期间：{plan['period']}。",
           '','以下记录来源核验和补充位置，不能将旧资料数值自动填入本期。已自动取得的字段以本次取数结果为准。','']
    photos=len(collected.get('photos',{}).get('items',[]))
    work=len(collected.get('repeat_groups',{}).get('records.3',{}).get('records',[]))
    if photos:lines.extend([f'现场图片：本次已取得并校验{photos}张，来自当期真实工单附件；不等于原历史报告图片已完整复现。',''])
    if work:lines.extend([f'本月工作总结：已取得{work}条安全管理处理记录，序号、类别和内容可自动填写；未明确的细分类别及其他工作仍需补充。',''])
    column_counts={}
    for group in collected.get('repeat_groups',{}).values():
        for row in group.get('records',[]):
            for fid,cell in row.items():
                if isinstance(cell,dict) and cell.get('status') in {'real','derived','config','request','fixed'} and cell.get('value') is not None:
                    column_counts[fid]=column_counts.get(fid,0)+1
    if column_counts:
        lines.extend(['已填循环列按实际单元格计数（不代表整组资料已齐备）：'+ '；'.join(f'{fid} {count}项' for fid,count in sorted(column_counts.items())),''])
    for fid,item in collected.get('fields',{}).items():
        review=item.get('source_review')
        model=item.get('field_model')
        if (not review and not model) or item.get('status') in {'real','request','config','derived','fixed'}:continue
        if (fid=='F090' and photos) or (fid in {'F064','F065','F067'} and work):continue
        if fid in {'F059','F060'} and column_counts.get(fid):continue
        if item.get('fixedness_review',{}).get('active') and column_counts.get(fid):continue
        lines.extend([f"## {fid} {item.get('label','')}",'',f"当前状态：{item['status']}。",'',item.get('reason','待填'),''])
        if item.get('fixedness_review'):
            review_fixed=item['fixedness_review']
            lines.extend(['固定性确认：'+review_fixed['reason'],'待确认问题：'+(review_fixed['question'] or '无；按已确认模板内容执行'),''])
        if model:
            lines.extend(['字段含义：'+model['meaning'],'','变化规则：'+model['change_rule'],'',
                '取数或计算规则：'+json.dumps(model['derivation'],ensure_ascii=False),''])
            for gid,method in item.get('lookup_methods',{}).items():
                lines.append('- '+method.get('source_name',gid)+'：'+json.dumps(method,ensure_ascii=False))
            lines.append('')
        if not review:continue
        lines.extend([f"本站本期核验状态：{review['review_status']}。",'',review['reason'],''])
        if review.get('document_reason'):lines.extend([review['document_reason'],''])
        if item.get('runtime_reason') and item['runtime_reason']!=review['reason']:
            lines.extend(['本次执行：'+item['runtime_reason'],''])
        for location in review.get('manual_locations',[]):
            details=[location.get('file_name','')]
            for key in ['sheet','range','page','pages','pdf_pages','identity_cell','value_cell']:
                if key in location:details.append(f'{key}={location[key]}')
            lines.append('- '+'；'.join(details))
        lines.append('')
    online=guide.get('online_files',{})
    if online:
        lines.extend(['## 线上资料定位',''])
        entry=guide.get('catalog_guides',{}).get(online.get('lookup_guide_id'),{}).get('entry_url')
        if entry:lines.extend([f'[打开Power+资料管理]({entry})',''])
        lines.extend('- '+step for step in online.get('navigation',[]))
        lines.extend(['',online.get('source_time_limit','')])
    lines.extend(['',f"详细请求、工单标识和核验时间见[本站来源配置]({guide['path']})。",''])
    Path(path).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return str(path)
