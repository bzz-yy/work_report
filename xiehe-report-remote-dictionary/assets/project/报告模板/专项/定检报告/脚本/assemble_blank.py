"""Apply a resolved station profile to a master; retain placeholders, never fill facts."""
from common import *
from resolve_report import resolve,ResolutionError
import argparse
from toc import write_toc

def ptext(p,value):
    texts=p.xpath('.//w:t',namespaces=NS)
    if texts:
        texts[0].text=value
        for t in texts[1:]:t.text=''
    else:E.SubElement(E.SubElement(p,W+'r'),W+'t').text=value

def assemble(plan,output):
    output=Path(output)
    if output.exists():raise FileExistsError('不覆盖已有文件：'+str(output))
    source=Path(plan['template_file'])
    if digest(source)!=plan['template_sha256']:raise ResolutionError('定位后模板发生变化')
    if plan['clause_status']=='new_period_applicability_pending':raise ResolutionError('该周期的检查标准适用性未确认，不能自动沿用历史条款')
    profile=plan['profile']
    with zipfile.ZipFile(source) as z:root=E.fromstring(z.read('word/document.xml'))
    body=root.find(W+'body');selected=profile['equipment_modules']
    allmodules=read(plan['mapping_file'])['equipment_modules']
    for key in allmodules:
        x=find_sdt(root,'equipment:'+key)
        if key not in selected:x.getparent().remove(x)
    if plan['clause_file']:
        clauses=read(plan['clause_file'])
        if clauses['station_id']!=plan['station_id'] or clauses['clause_version']!=profile['clause_version']:raise ResolutionError('条款版本不匹配')
        for key,block in clauses['blocks'].items():
            if key not in selected:continue
            x=find_sdt(root,'clause:'+key).find(W+'sdtContent')
            for child in list(x):x.remove(child)
            x.append(E.fromstring(block['xml'].encode(),parser=E.XMLParser(resolve_entities=False,no_network=True)))
    if plan['template_id']=='DJ-02' and not profile['include_tickets']:
        x=find_sdt(root,'tickets');x.getparent().remove(x)
    signatures={role:find_sdt(root,'signature:'+role) for role in profile['signature_order']};first=min(body.index(x) for x in signatures.values())
    for x in signatures.values():body.remove(x)
    for i,role in enumerate(profile['signature_order']):body.insert(first+i,signatures[role])
    for i,key in enumerate(selected,1):
        content=find_sdt(root,'equipment:'+key).find(W+'sdtContent');p=content.find(W+'p')
        if p is None:raise ResolutionError('设备模块缺标题')
        ptext(p,f'{i}、{MODULE_NAMES[key]}')
        for num in p.xpath('./w:pPr/w:numPr',namespaces=NS):num.getparent().remove(num)
    if profile['include_tickets']:
        p=find_sdt(root,'tickets').find('./'+W+'sdtContent/'+W+'p');ptext(p,'四、两票执行表')
        for num in p.xpath('./w:pPr/w:numPr',namespaces=NS):num.getparent().remove(num)
    toc_entries=write_toc(root)
    clean_package(source,output,E.tostring(root))
    with zipfile.ZipFile(output) as z:final=E.fromstring(z.read('word/document.xml'))
    actual=set(re.findall(r'J\d{3}',text(final)));expected={f['field_id'] for f in plan['fields']}
    if actual!=expected:raise ResolutionError('组装后字段覆盖不一致：'+str(actual^expected))
    record={'station_id':plan['station_id'],'template_id':plan['template_id'],'period':plan['period'],'output':str(output.resolve()),'sha256':digest(output),'equipment_modules':selected,'clause_version':profile['clause_version'],'include_tickets':profile['include_tickets'],'signature_order':profile['signature_order'],'field_count':len(actual),'table_count':len(final.xpath('//w:tbl',namespaces=NS)),'toc_entries':toc_entries,'status':'assembled_blank_structure_only','business_data_filled':False}
    return record

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--request',required=True);p.add_argument('--template-id',choices=['DJ-01','DJ-02']);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    try:v=assemble(resolve(a.request,template_id=a.template_id),a.out)
    except (ValueError,FileExistsError) as e:p.exit(2,str(e)+'\n')
    write(a.out.with_suffix('.json'),v);print(json.dumps(v,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
