"""Create an onboarding dossier; no live station creation or fabricated platform IDs."""
from pathlib import Path
import json,csv,re

def intake(project, kind, name, out, period='2026-06'):
    if kind not in {'existing','new-contract'} or not name.strip():raise ValueError('需要明确接入类型和电站名称')
    if not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',period):raise ValueError('月份须为YYYY-MM')
    project,out=Path(project).resolve(),Path(out).resolve()
    for active in ('电站','报告模板/计划/运维月报/电站','报告模板/专项/定检报告/电站','报告模板/专项/组件清洗报告/电站'):
        if out.is_relative_to(project/active):raise ValueError('待接入材料不得写入正式电站目录')
    out.mkdir(parents=True,exist_ok=False)
    rules=json.loads((project/'报告模板/计划/运维月报/模板/NW-MONTHLY-STD-01/通用取值规则.json').read_text())
    from fixed_fields import template_content
    template_defaults=template_content(rules)
    common=['核验公司资料编号与平台编码，保留身份依据','明确需要的报告类型、期间、模板和附件','逐项判断标准月报是否适配；缺数不等于不适配','按90字段清单查字典，核对本站当期来源','核验后建立正式电站基础配置及模板关联','配置检查→实际取数→待填版生成→逐页验收→业务审核']
    prefix=['核验公司现有电站档案与在运/服务状态'] if kind=='existing' else ['核验新签合同、客户与电站主体、服务范围及生效日期','完成资料交接：图纸、设备台账、计量边界、人员与安全资料','确认平台建档责任人、真实编码和取数权限；未建档保持null','确认运维起始日与首份报告期间；合同签订不代表已投运']
    dossier={'schema_version':1,'kind':kind,'station_name':name,'station_id':None,'powerplus_station_id':None,'status':'pending_identity_and_requirements','simulation':False,'period':period,'report_requirement':{'report_type':'运维月报','status':'candidate_pending_confirmation','template_candidate':'NW-MONTHLY-STD-01','template_approved':False},'can_generate_live':False,'blocking_items':['电站身份尚未核验','报告需求及模板适配未确认'],'steps':prefix+common,'business_data':None}
    (out/'接入申请.json').write_text(json.dumps(dossier,ensure_ascii=False,indent=2)+'\n')
    rows=[{'字段编号':fid,'字段名称':f['filling_rule']['label'],'公共数据ID':f['data_definition']['standard_id'],'本站当期值':None,'核查状态':'待核查','已知来源候选':','.join(f['data_definition'].get('input_mapping_ids',[])) or None,'备注':'候选可复用；不得照抄其他站实际值、表号、人员、照片或期间特例'} for fid,f in rules['fields'].items()]
    fixed_reviews = {}
    for row in rows:
        fid = row['字段编号']; candidate = rules['fields'][fid].get('fixedness')
        row['历史同值来源'] = '24份原月报同值已复核' if candidate else ''
        row['固定候选原文'] = json.dumps(candidate['observed_value'],ensure_ascii=False) if candidate else ''
        row['固定性二次确认'] = '待二次确认：固定或动态' if candidate else '按原字段规则核验'
        row['二次确认问题'] = candidate['question'] if candidate else ''
        if fid in template_defaults.get('fields', {}):
            row['固定性二次确认']='模板已确认固定；适配本模板后自动沿用'
            row['二次确认问题']='无需本站再确认；不同模板不继承'
            row['核查状态']='模板内容已确认；电站及模板关联仍待核'
            row['备注']='此模板位置不查平台；有意空白不标待填。不是本站本期平台实值。'
        elif candidate:
            fixed_reviews[fid] = {'status':'pending_confirmation','value':None,'scope':None,
                'confirmed_by':None,'confirmed_at':None,'reason':None,'evidence':None}
    dossier['fixedness_decisions'] = fixed_reviews
    dossier['steps'].insert(dossier['steps'].index('按90字段清单查字典，核对本站当期来源'),
                            f'共享模板适配后沿用{len(template_defaults.get("fields",{}))}个模板固定位置；其余{len(fixed_reviews)}个历史同值候选仍待确认，不继承其他站期间批准')
    (out/'接入申请.json').write_text(json.dumps(dossier,ensure_ascii=False,indent=2)+'\n')
    (out/'固定字段二次确认.json').write_text(json.dumps({
        'station_name':name,'station_id':None,'template_id':rules['template_id'],
        'rules_version':rules['rules_version'],'template_fit_must_be_confirmed_first':True,
        'template_fixed_content':template_defaults,
        'decisions':fixed_reviews,'note':'模板固定内容在确认本模板适配后沿用；其余候选若做本站期间固定，须填写原文、本站身份、模板版本、起止月份、确认人及依据。模板内容不复制到本站批准。'},ensure_ascii=False,indent=2)+'\n')
    with (out/'90字段接入核验.csv').open('w',encoding='utf-8-sig',newline='') as s:
        w=csv.DictWriter(s,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (out/'接入步骤.md').write_text('# '+name+' 接入材料\n\n当前为待接入申请，尚未注册为可生成电站。\n\n'+'\n'.join(f'{i}. {s}' for i,s in enumerate(dossier['steps'],1))+'\n\n未知值使用JSON null；CSV空值代表未取得，不是0。身份和模板确认后由Agent按电站与模板接入规则执行正式配置。\n')
    return {'out':str(out),'kind':kind,'status':dossier['status'],'can_generate_live':False,'field_count':len(rows)}

def simulate(project,out):
    project,out=Path(project).resolve(),Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    results=[]
    for kind,name in [('existing','模拟甲站（公司已有电站）'),('new-contract','模拟乙站（公司新签电站）')]:
        r=intake(project,kind,name,out/kind);p=out/kind/'接入申请.json';d=json.loads(p.read_text());d['simulation']=True;p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n');results.append(r)
    (out/'模拟结果.json').write_text(json.dumps({'simulation':True,'network_calls':0,'active_station_mutations':0,'results':results,'conclusion':'两条接入分支均生成90字段核验清单，缺身份时停在待接入；未虚构平台编码或冒充真实新站接通。'},ensure_ascii=False,indent=2)+'\n')
    return {'out':str(out),'results':results,'simulation':True,'network_calls':0}
