"""Validate active configuration without historical files or network access."""
from pathlib import Path
import csv
import hashlib
import json
import sys

from report_rules import load_bundle
from station_config import load_station_config
from cleaning_template import check_cleaning_template
from report_catalog import load_catalog, report_root


def check(project):
    project = Path(project).resolve()
    if (project/'.onboarding-transaction.json').exists():
        raise ValueError('接入事务未完成，先执行onboard recover，禁止检查半配置')
    sys.path.insert(0, str(project / '数据字典/脚本'))
    from query_power_bill import source_catalog
    catalog = source_catalog()  # includes adapter/verified-contract consistency
    bases = [json.loads(p.read_text(encoding='utf-8')) for p in (project/'电站').glob('*/基础配置.json')]
    codes = [b['platforms']['powerplus']['station_code'] for b in bases]
    if len(codes) != len(set(codes)):
        raise ValueError('电站基础配置的平台编码重复')
    report_catalog = load_catalog(project)
    profiles = []
    for report in ('运维月报', '定检报告'):
        root = report_root(project, report)
        with (root/'模板电站索引.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        seen = set()
        for row in rows:
            cfgpath = root / row['电站配置']
            cfg = load_station_config(cfgpath)
            tid = row['模板编号']
            identity = (cfg['station_id'], tid)
            if identity in seen or cfg['station_id'] != row['电站编码']:
                raise ValueError('模板电站索引重复或串站')
            seen.add(identity)
            if cfg.get('readiness', {}).get('executor') == 'unsupported':
                if cfg['readiness']['status'] != 'executor_requires_adaptation':
                    raise ValueError('未适配执行器不能标为已启用')
                continue
            if cfg.get('readiness', {}).get('executor') == 'monthly_mapped_v1':
                from mapped_template import validate_adapter
                new_map_path=root/'模板'/tid/'模板字段映射.json'
                new_map=json.loads(new_map_path.read_text())
                new_rules=json.loads((new_map_path.parent/'通用取值规则.json').read_text())
                source_cfg_path=(cfgpath.parent/cfg['source_adapter_config']).resolve()
                if not source_cfg_path.is_relative_to(cfgpath.parent):
                    raise ValueError('取数适配配置必须属于本站模板关联')
                if ('profile' in cfg or 'profile_evidence' in cfg
                        or cfg.get('profile_ref')!=cfg['source_adapter_config']+'#/profile'):
                    raise ValueError('映射报告名称不能重复维护；外层须引用取数适配profile')
                source_cfg=load_station_config(source_cfg_path)
                base_map_path=(source_cfg_path.parent/source_cfg['template_mapping']).resolve()
                base_map=json.loads(base_map_path.read_text())
                base_rules=json.loads((base_map_path.parent/'通用取值规则.json').read_text())
                validate_adapter(new_rules,new_map,base_rules,base_map)
                if source_cfg['station_id']!=cfg['station_id'] or source_cfg['template_id']!='NW-MONTHLY-STD-01':
                    raise ValueError('新模板的取数适配串站或串模板')
                load_bundle(root,source_cfg_path,base_map_path,'NW-MONTHLY-STD-01','2026-06')
                profiles.append({'report':report,'service_category':report_catalog['reports'][report]['服务大类'],
                    'station_id':cfg['station_id'],'template_id':tid,'field_count':len(new_rules['fields']),
                    'powerplus_station_id':cfg.get('powerplus_station_id'),'executor':'monthly_mapped_v1'})
                continue
            mapping_path = root / '模板' / tid / '模板字段映射.json'
            mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
            template = mapping_path.parent / mapping.get('template_file', mapping.get('file'))
            if hashlib.sha256(template.read_bytes()).hexdigest() != mapping.get('template_sha256', mapping.get('sha256')):
                raise ValueError('模板摘要与字段映射不一致：' + str(template))
            period = '2026-06' if report == '运维月报' else '2026-H1'
            bundle = load_bundle(root, cfgpath, mapping_path, tid, period)
            profiles.append({'report': report, 'service_category': report_catalog['reports'][report]['服务大类'], 'station_id': cfg['station_id'], 'template_id': tid,
                             'field_count': len(bundle['field_config']['fields']),
                             'powerplus_station_id': cfg.get('powerplus_station_id')})
    with (project/'报告索引.csv').open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            for key in ('报告目录', '说明', '取值规则入口'):
                if not (project/row[key]).exists():
                    raise ValueError('报告索引断链：' + row[key])
            if '字典补充清单' in row['取值规则入口']:
                raise ValueError('报告索引仍指向旧字典补充清单')
    mappings = catalog['source_mappings']
    definition_coverage = []
    fixedness_reviews = []
    rule_paths = sorted(path for row in report_catalog['reports'].values()
                        for path in (project / row['报告目录']).glob('模板/*/通用取值规则.json'))
    for path in rule_paths:
        rules = json.loads(path.read_text(encoding='utf-8'))
        mapping_path = path.parent/'模板字段映射.json'
        mapping = json.loads(mapping_path.read_text(encoding='utf-8'))
        template_path = path.parent/mapping.get('template_file', mapping.get('file'))
        if hashlib.sha256(template_path.read_bytes()).hexdigest() != mapping.get('template_sha256', mapping.get('sha256')):
            raise ValueError('模板摘要与字段映射不一致：'+str(template_path))
        if rules.get('executor') == 'unsupported':
            ids = [f['id'] for f in mapping['fields']]
            if len(ids) != len(set(ids)) or set(ids) != set(rules['fields']):
                raise ValueError('新模板字段位置未完整登记')
        candidates = {fid: field['fixedness']['decision']['status']
                      for fid,field in rules['fields'].items() if field.get('fixedness')}
        from fixed_fields import template_content
        template_defaults = template_content(rules)
        for fid in template_defaults.get('fields', {}):
            candidates[fid] = 'confirmed_template_content'
        if candidates:
            fixedness_reviews.append({'template_id':rules['template_id'],
                                      'rules_version':rules['rules_version'],
                                      'default_decisions':candidates,
                                      'template_fixed_fields':sorted(template_defaults.get('fields',{})),
                                      'pending_fields':[fid for fid,state in candidates.items() if state=='pending_confirmation'],
                                      'note':'模板固定内容绑定模板规则版本，所有同模板电站沿用；其余站期固定批准仍逐项核范围。'})
        for fid, field in rules['fields'].items():
            binding = field.get('data_definition', {})
            from catalog_schema import validate_definition_binding
            validate_definition_binding(catalog,binding,str(path)+'/'+fid)
            if binding.get('standard_id') not in catalog['standard_fields']:
                raise ValueError('模板字段未关联公共定义：' + str(path) + '/' + fid)
            inputs = binding.get('input_mapping_ids', [])
            if not set(inputs) <= set(mappings):
                raise ValueError('定义的来源输入引用不存在：' + fid)
            for key in ('source_selection', 'attachment_selection', 'plan_selection'):
                selected = field.get(key) or {}
                if key == 'source_selection' and selected.get('mapping_id'):
                    if mappings[selected['mapping_id']]['standard_id'] != binding['standard_id']:
                        raise ValueError('公共定义与实际采用来源不一致：' + fid)
        definition_coverage.append({'template_id': rules['template_id'], 'fields': len(rules['fields']),
                                    'bound': len(rules['fields'])})
    cleaning_template = check_cleaning_template(project)
    result = {
        'status': 'configuration_valid', 'dictionary_version': catalog['version'],
        'report_categories': [{**category, 'reports': [name for name, row in report_catalog['reports'].items()
                                                    if row['服务大类'] == category['服务大类']]}
                              for category in report_catalog['categories']],
        'standard_fields': len(catalog['standard_fields']), 'source_mappings': len(mappings),
        'query_methods': len(catalog['query_methods']), 'profiles': profiles,
        'definition_coverage': definition_coverage,
        'fixedness_reviews': fixedness_reviews,
        'field_bindings': sum(p['field_count'] for p in profiles),
        'cleaning_template': cleaning_template,
        'checks': ['四类报告索引与路径完整性', '字典职责边界与内部引用', '名称与返回字段', '接口参数与只读适配契约',
                   '来源及报告单位换算', '字段候选与适用判断', '模板摘要与电站关联',
                   '期间绑定与本站覆盖规则','历史同值来源与固定性二次确认范围'],
        'unresolved_sources': [
            {'mapping_id': mid, 'name': m['source_field_name'], 'technical_status': m['technical_status'],
             'questions': catalog['standard_fields'][m['standard_id']]['pending_questions'] + m['pending_questions']}
            for mid, m in mappings.items()
            if m['technical_status'] not in {'verified_current_attributes', 'verified_source_values'}
               or catalog['standard_fields'][m['standard_id']]['pending_questions'] or m['pending_questions']],
        'business_data_complete': False,
        'note': '结构与可执行契约校验通过不等于报告口径批准；待确认来源不能自动填值。',
    }
    return result
