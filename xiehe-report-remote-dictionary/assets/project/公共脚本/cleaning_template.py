"""Validate the cleaning Word template and evidence-scoped station associations."""
import csv
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile
from lxml import etree as E
from station_config import load_station_config
from report_catalog import report_root

NS = {'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}


def check_cleaning_template(project):
    root = report_root(project, '组件清洗报告')
    entry = json.loads((root/'取值规则.json').read_text())
    if not entry.get('formal_template_ready'):
        return {'status':'template_not_ready'}
    rule_path = (root/entry['rules_ref']).resolve()
    rules = json.loads(rule_path.read_text())
    mapping = json.loads((rule_path.parent/'模板字段映射.json').read_text())
    path = rule_path.parent/mapping['template_file']
    if hashlib.sha256(path.read_bytes()).hexdigest()!=mapping['template_sha256']:
        raise ValueError('清洗模板摘要与字段映射不一致')
    with ZipFile(path) as z:
        doc = E.fromstring(z.read('word/document.xml'))
        if any(n.startswith('word/embeddings/') and not n.endswith('/') for n in z.namelist()):
            raise ValueError('清洗空模板仍含历史嵌入附件')
        if [n for n in z.namelist() if n.startswith('word/media/') and not n.endswith('/')] != ['word/media/image1.jpeg']:
            raise ValueError('清洗空模板媒体超出已确认页眉标识')
    expected = {f'Q{i:03}' for i in range(1,34)}
    actual = set(re.findall(r'\{\{(Q\d{3})[^}]*\}\}', ''.join(doc.xpath('.//w:t/text()',namespaces=NS))))
    if actual!=expected or set(mapping['fields'])!=expected or set(rules['fields'])!=expected:
        raise ValueError('清洗模板字段覆盖不完整')
    for slots in mapping['fields'].values():
        for slot in slots:
            found=doc.xpath(slot['xpath'],namespaces=doc.nsmap)
            if len(found)!=1 or slot['token'] not in (found[0].text or ''):
                raise ValueError('清洗Word标记与字段位置不一致')
    if len(doc.findall('w:body/w:tbl',NS))!=7 or len(doc.findall('.//w:sectPr',NS))!=1:
        raise ValueError('清洗主模板7表或单节结构变化')
    if any(f.get('source_selection') for f in rules['fields'].values()):
        raise ValueError('清洗模板尚未接通自动取数，不能静默设置已选来源')
    with (root/'模板电站索引.csv').open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    verified=set(mapping['validated_station_ids']);pending=set(mapping['pending_station_ids']);seen=set()
    for row in rows:
        cfg=load_station_config(root/row['电站配置']);sid=cfg['station_id']
        if sid!=row['电站编码'] or sid in seen:
            raise ValueError('清洗电站索引重复或串站')
        if cfg.get('readiness', {}).get('executor') == 'unsupported':
            if cfg['readiness']['status'] != 'executor_requires_adaptation':
                raise ValueError('新清洗关联未适配不能启用')
            continue
        seen.add(sid)
        if sid in pending:
            if cfg.get('template_id') or row['模板编号'] or row['模板文件'] or cfg.get('template_rules'):
                raise ValueError('缺样本站不能绑定清洗模板')
            if cfg['template_status']!='sample_missing':
                raise ValueError('缺样本站状态不一致')
        elif sid in verified:
            if (cfg.get('template_id')!='QX-01' or row['模板编号']!='QX-01'
                    or not cfg.get('observed_source_ids') or cfg['template_status']!='verified_shared_structure'
                    or (root/row['模板文件']).resolve()!=path.resolve()
                    or ((root/row['电站配置']).parent/cfg['template_rules']).resolve()!=rule_path):
                raise ValueError('清洗模板关联与样本依据不一致')
        else:
            raise ValueError('未核验站点不能自动套用清洗模板')
    if seen!=verified|pending or verified&pending:
        raise ValueError('清洗四站状态登记不完整')
    return {'status':'template_valid','template_id':'QX-01','field_count':33,
            'verified_station_ids':sorted(verified),'pending_station_ids':sorted(pending),
            'auto_generation_ready':False,'business_approved':False}
