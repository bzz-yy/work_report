"""Resumable, reviewed local onboarding; official data and business approval stay separate."""
from contextlib import contextmanager
from copy import deepcopy
import csv
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from report_catalog import load_catalog, SERVICE_CATEGORIES
from report_route import (read, digest, rows, route_request, station_inventory,
                          template_candidates, binding_fingerprint)
from remote_catalog import load_catalog as load_source_catalog, catalog_digest


def dump(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump(value))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def case_file(case, relative, required=True):
    case = Path(case).resolve()
    path = (case / relative).resolve()
    if not path.is_relative_to(case) or (required and not path.is_file()):
        raise ValueError('接入材料路径越界或不存在：' + str(relative))
    return path


def fingerprint(project):
    project = Path(project).resolve()
    result = {str(p.relative_to(project)): digest(p) for p in sorted(project.rglob('*'))
              if p.is_file() and not any(x.startswith('.') or x == '__pycache__' for x in p.relative_to(project).parts)
              and p.suffix not in {'.pyc', '.pyo'}}
    # Distribution must change along with new runtime configuration.
    manifest = project.parent.parent / 'distribution-files.json'
    result['../../distribution-files.json'] = digest(manifest)
    result['remote:dictionary_readonly'] = catalog_digest(project)
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def init_case(project, request, kind, out, sample=None, report_type=None, period=None, station_id=None):
    project, out = Path(project).resolve(), Path(out).resolve()
    skill = project.parent.parent
    if out.is_relative_to(skill):
        raise ValueError('接入材料放在Skill外，避免原始报告进入运行包')
    if kind not in {'existing', 'new-contract'}:
        raise ValueError('接入类型必须为existing或new-contract')
    route = route_request(project, request, station_id, period, report_type)
    if route['route'] == 'clarification':
        raise ValueError(route['reason'])
    if not route.get('station_name') or not route.get('report_type'):
        raise ValueError('先明确电站名称、报告类型和期间')
    out.mkdir(parents=True, exist_ok=False)
    case_id = uuid.uuid4().hex
    document = None
    if sample:
        sample = Path(sample).resolve()
        if sample.suffix.lower() != '.docx' or not sample.is_file():
            raise ValueError('模板样本须为可读DOCX；其他格式先转换并保留原件')
        target = out / '原件' / sample.name
        target.parent.mkdir()
        shutil.copy2(sample, target)
        document = {'path': str(target.relative_to(out)), 'sha256': digest(target),
                    'original_path': str(sample), 'usage': '仅作结构和字段分析，禁止补填当期值'}
    station = next((s for s in station_inventory(project) if s['station_id'] == route.get('station_id')), None)
    template = route.get('template_id') or None
    case_data = {'schema_version': 1, 'case_id': case_id, 'created_at': now(), 'kind': kind,
                 'request': request, 'status': 'planning', 'route': route, 'source_document': document,
                 'simulation': False, 'business_approved': False,
                 'contract_context': ({'service_effective_date': None, 'handover_status': None,
                                       'platform_registration_status': None, 'pending_questions': []}
                                      if kind == 'new-contract' else None)}
    plan = {'schema_version': 1, 'case_id': case_id, 'base_fingerprint': fingerprint(project),
            'report_type': route['report_type'], 'service_category': load_catalog(project)['reports'].get(route['report_type'], {}).get('服务大类'),
            'period': route['period'], 'simulation': False,
            'identity': {'action': 'reuse' if station else 'register', 'station_id': route.get('station_id'),
                         'station_name': route['station_name'], 'aliases': [], 'powerplus_station_code': None,
                         'powerplus_station_name': None, 'status': 'verified_existing' if station else 'pending',
                         'evidence': [], 'resource_parameters': {}},
            'requirement': {'status': 'confirmed_from_request', 'evidence': [request]},
            'template': {'action': 'reuse', 'template_id': template, 'sha256': None,
                         'fit_status': 'pending', 'evidence': [], 'differences': []},
            'profile': {'cover_name': None, 'overview_short_name': None, 'revenue_name': None},
            'profile_evidence': [], 'field_decisions': [], 'pending_questions': [], 'evidence_files': [],
            'review': {'status': 'pending', 'reviewed_by': None, 'evidence': []},
            'business_approved': False}
    write_json(out / '接入记录.json', case_data)
    write_json(out / '接入计划.json', plan)
    write_json(out / '可选模板.json', route['template_candidates'])
    if document:
        inspect_case(project, out)
    return {'case': str(out), 'case_id': case_id, 'status': 'planning', 'plan': str(out / '接入计划.json'),
            'route': route['reason'], 'source_preserved': document, 'can_generate': False}


def inspect_case(project, case):
    from template_inspect import inspect_docx, compare_templates
    case = Path(case).resolve()
    data = read(case / '接入记录.json')
    source = data.get('source_document')
    if not source:
        raise ValueError('尚未提供报告样本；仍可依据明确模板要求选择已有模板')
    path = case_file(case, source['path'])
    if digest(path) != source['sha256']:
        raise ValueError('原始报告摘要变化')
    analysis = inspect_docx(path)
    comparison = (compare_templates(project, path, data['route']['report_type'])
                  if data['route']['report_type'] in load_catalog(project)['reports']
                  else {'sample': analysis['source'], 'candidates': [], 'business_fit': 'unreviewed', 'reason': '新报告类型，尚无同类型候选'})
    write_json(case / '样本结构.json', analysis)
    write_json(case / '模板比对.json', comparison)
    return {'structure': str(case / '样本结构.json'), 'comparison': str(case / '模板比对.json'),
            'business_fit': 'unreviewed', 'source_sha256': source['sha256']}


def evidence(value, label):
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(label + '需要可定位的依据列表')


def verify_evidence_references(plan, case):
    proofs = plan.get('evidence_files', [])
    registered = {}
    for item in proofs:
        path = case_file(case, item['path'])
        if digest(path) != item.get('sha256'):
            raise ValueError('核验材料摘要变化：' + item['path'])
        registered[str(Path(item['path']))] = path
    def visit(value, key=None):
        if key in {'evidence', 'profile_evidence'} and isinstance(value, list):
            for ref in value:
                if not isinstance(ref, str):
                    raise ValueError('依据引用须为Case相对文件路径')
                name, _, pointer = ref.partition('#')
                if name not in registered:
                    raise ValueError('依据未登记SHA256或引用不存在：' + ref)
                if pointer and registered[name].suffix == '.json':
                    data = read(registered[name])
                    try:
                        for token in pointer.lstrip('/').split('/'):
                            token = token.replace('~1', '/').replace('~0', '~')
                            data = data[int(token)] if isinstance(data, list) else data[token]
                    except (KeyError, ValueError, IndexError, TypeError) as exc:
                        raise ValueError('依据JSON位置不存在：' + ref) from exc
            return
        if isinstance(value, dict):
            for name, item in value.items():
                if name == 'source':
                    continue  # Candidate SM is checked against the public catalog; its historical references are not new Case evidence.
                visit(item, name)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    # Requirement text is preserved directly from the user's request; all reviewed conclusions refer to files.
    visit({k: v for k, v in plan.items() if k not in {'requirement', 'evidence_files'}})


def safe_name(value, label):
    if not isinstance(value, str) or not value.strip() or value in {'.', '..'} or any(c in value for c in '/\\\0\n\r'):
        raise ValueError(label + '不是有效名称')
    return value


def csv_bytes(existing, record):
    data = list(existing)
    names = list(data[0]) if data else list(record)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=names)
    writer.writeheader()
    writer.writerows(data + [{k: record.get(k, '') for k in names}])
    return ('\ufeff' + stream.getvalue()).encode('utf-8')


def _identity(project, plan, case_data):
    identity = plan['identity']
    sid = identity.get('station_id')
    if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', sid):
        raise ValueError('公司电站ID必须明确；不能从名称或平台编码猜出')
    inventory = station_inventory(project)
    found = [s for s in inventory if s['station_id'] == sid]
    if identity['action'] == 'reuse':
        if len(found) != 1 or identity.get('station_name') != found[0]['station_name']:
            raise ValueError('复用电站身份不一致')
        path = Path(found[0]['base_file'])
        return path.relative_to(project), None, found[0]
    if identity['action'] != 'register' or found:
        raise ValueError('电站已登记；应复用全局身份，不重复注册')
    if identity.get('status') != 'verified':
        raise ValueError('新电站身份尚未核验')
    evidence(identity.get('evidence'), '电站身份')
    name = safe_name(identity['station_name'], '电站名称')
    aliases = identity.get('aliases', [])
    if not isinstance(aliases, list) or any(not isinstance(x, str) or not x.strip() for x in aliases):
        raise ValueError('电站别名无效')
    aliases = list(dict.fromkeys([sid, name, *aliases]))
    for s in inventory:
        if set(aliases) & set([s['station_id'], s['station_name'], *s.get('aliases', [])]):
            raise ValueError('新电站名称或别名与已有电站冲突')
    code = identity.get('powerplus_station_code')
    if isinstance(code, bool) or not str(code).isdigit() or int(code) <= 0:
        raise ValueError('缺少已核验Power+电站编码；不得使用另一站编码')
    code = int(code)
    if any(str(s['platforms']['powerplus']['station_code']) == str(code) for s in inventory):
        raise ValueError('Power+编码已关联另一电站')
    platform_name = safe_name(identity.get('powerplus_station_name'), '平台电站名称')
    if not isinstance(identity.get('resource_parameters', {}), dict):
        raise ValueError('本站资源参数必须为对象')
    if case_data['kind'] == 'new-contract':
        context = case_data.get('contract_context') or {}
        if not all(k in context for k in ['service_effective_date', 'handover_status', 'platform_registration_status', 'pending_questions']):
            raise ValueError('新签站须保留服务生效、交接与平台建档核验位置')
    base = {'schema_version': 1, 'station_id': sid, 'station_name': name, 'aliases': aliases,
            'platforms': {'powerplus': {'station_code': code, 'station_name': platform_name,
                'identity_evidence': {'project_station_id': sid, 'project_station_name': name,
                    'powerplus_station_id': code, 'powerplus_station_name': platform_name,
                    'status': 'verified_unique_name_match', 'captured_at': now(),
                    'evidence': identity['evidence'], 'scope': '接入计划提供的身份依据，不证明期间业务数据已接通'},
                'resource_parameters': identity.get('resource_parameters', {})}},
            'onboarding': {'case_id': case_data['case_id'], 'kind': case_data['kind'],
                           'simulation': bool(plan['simulation']), 'contract_context': case_data.get('contract_context')}}
    return Path('电站') / f'{sid}_{name}' / '基础配置.json', dump(base), base


def build_changes(project, case):
    """Build exact file writes from a reviewed domain plan; no arbitrary file edits."""
    project, case = Path(project).resolve(), Path(case).resolve()
    plan_raw, case_raw = (case/'接入计划.json').read_bytes(), (case/'接入记录.json').read_bytes()
    plan, data = json.loads(plan_raw), json.loads(case_raw)
    plan_sha256, case_sha256 = hashlib.sha256(plan_raw).hexdigest(), hashlib.sha256(case_raw).hexdigest()
    if plan.get('schema_version') != 1 or plan.get('case_id') != data['case_id']:
        raise ValueError('接入计划版本或Case不一致')
    if plan.get('business_approved') is not False:
        raise ValueError('技术接入不能批准正式业务报告')
    if plan['base_fingerprint'] != fingerprint(project):
        raise ValueError('项目已变化，须重新审查并刷新计划基线，不能覆盖并发修改')
    if plan.get('simulation'):
        sandbox = project / '.onboarding-sandbox.json'
        if not sandbox.is_file() or read(sandbox) != {'project': str(project), 'purpose': 'isolated_offline_test'}:
            raise ValueError('合成接入只能应用于明确标记的隔离测试项目')
    proofs = plan.get('evidence_files', [])
    if not isinstance(proofs, list) or not proofs:
        raise ValueError('计划需登记本次核验材料路径及SHA256')
    verify_evidence_references(plan, case)
    if plan.get('review', {}).get('status') != 'reviewed' or not plan['review'].get('reviewed_by'):
        raise ValueError('接入计划尚未完成技术复核')
    evidence(plan['review'].get('evidence'), '技术复核')
    evidence(plan.get('requirement', {}).get('evidence'), '报告需求')
    source = data.get('source_document')
    if source and digest(case_file(case, source['path'])) != source['sha256']:
        raise ValueError('原始样本摘要变化')
    category, report = plan['service_category'], safe_name(plan['report_type'], '报告类型')
    if category not in SERVICE_CATEGORIES:
        raise ValueError('服务大类必须为计划、专项、消缺、突发')
    catalog = load_catalog(project)['reports']
    if report in catalog and catalog[report]['服务大类'] != category:
        raise ValueError('报告分类与已登记类型冲突')
    report_rel = Path('报告模板') / category / report
    template = plan['template']
    tid = safe_name(template.get('template_id'), '模板ID')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', tid):
        raise ValueError('模板ID只使用字母数字短横线和下划线')
    if template.get('fit_status') != 'reviewed':
        raise ValueError('模板适用性尚未逐项复核')
    evidence(template.get('evidence'), '模板适用性')
    dictionary = load_source_catalog(project)
    definition_count = len(dictionary['standard_fields'])
    changes = {}
    base_rel, base_bytes, base = _identity(project, plan, data)
    if base_bytes:
        changes[str(base_rel)] = base_bytes
    sid = base['station_id']
    station_rel = report_rel / '电站' / base_rel.parent.name
    index_path = project / report_rel / '模板电站索引.csv'
    existing_rows = rows(index_path) if index_path.exists() else []
    if any(r['电站编码'] == sid and r['模板编号'] == tid for r in existing_rows):
        raise ValueError('本站报告与模板关联已存在；此接入不覆盖已验证配置')
    if (project / station_rel).exists():
        station_rel = station_rel / tid
    template_rel = report_rel / '模板' / tid
    rules_rel = template_rel / '通用取值规则.json'
    mapping_rel = template_rel / '模板字段映射.json'
    decisions = plan.get('field_decisions', [])
    if decisions:
        from field_match import validate_decisions
        checked = validate_decisions(dictionary, decisions)
        if not checked['valid']:
            raise ValueError('字段决策无效：' + json.dumps(checked['errors'], ensure_ascii=False))
        if checked['changed']:
            raise ValueError('远程公共字典只读，接入不能新增或修改公共定义及来源；请由同事更新服务器字典后重新审查接入计划')
    executor = 'unsupported'
    if template['action'] == 'reuse':
        if report not in catalog or not (project / mapping_rel).is_file():
            raise ValueError('选定的已有模板不存在')
        mapping = read(project / mapping_rel)
        rules = read(project / rules_rel)
        filename = mapping.get('template_file', mapping.get('file'))
        if template.get('sha256') != digest(project / template_rel / filename):
            raise ValueError('复用模板摘要不一致')
        if report == '运维月报' and tid == 'NW-MONTHLY-STD-01':
            executor = 'monthly_nw_v1'
        elif report == '运维月报' and rules.get('executor') == 'monthly_mapped_v1':
            from mapped_template import validate_adapter
            base_dir=project/'报告模板/计划/运维月报/模板/NW-MONTHLY-STD-01'
            validate_adapter(rules,mapping,read(base_dir/'通用取值规则.json'),read(base_dir/'模板字段映射.json'))
            executor='monthly_mapped_v1'
        if decisions:
            raise ValueError('复用原模板时不重写共用字段映射；差异应另建版本并审查')
    elif template['action'] == 'new':
        if (project / template_rel).exists() or not source:
            raise ValueError('新模板必须有原件且模板ID不得已存在')
        if not decisions:
            raise ValueError('新模板必须逐位置记录字段复用/新定义决策')
        blank_plan = read(case_file(case, template.get('blank_plan', '空白化计划.json')))
        if blank_plan.get('source_sha256') != source['sha256']:
            raise ValueError('空白化计划不对应已保留原件')
        from template_inspect import blank_from_plan, inspect_docx
        with tempfile.TemporaryDirectory(prefix='xiehe-template-build-') as tmp:
            blank = Path(tmp) / '报告模板.docx'
            analysis = blank_from_plan(case_file(case, source['path']), blank, blank_plan)
            blank_bytes = blank.read_bytes()
            blank_structure = inspect_docx(blank)
        review = template.get('blank_review') or {}
        if (review.get('status') != 'reviewed' or not review.get('reviewed_by')
                or review.get('sha256') != hashlib.sha256(blank_bytes).hexdigest()
                or review.get('no_historical_values_or_signatures') is not True):
            raise ValueError('新空白模板须先生成并检查残留历史值、签字、图片，记录空白副本摘要及复核人')
        evidence(review.get('evidence'), '空白模板清理复核')
        actual_media = {image['asset_sha256'] for image in blank_structure['images']}
        if None in actual_media:
            raise ValueError('模板仍含关系无法解析的真实图片引用，不能登记为已清理')
        approved_media = review.get('retained_media', [])
        if (not isinstance(approved_media,list) or any(m.get('role') not in {'static_brand','static_diagram'} for m in approved_media)
                or {m.get('sha256') for m in approved_media} != actual_media):
            raise ValueError('历史现场图片/签名不能随模板入包；保留图片须逐一核验为静态标识或示意')
        from zipfile import ZipFile
        from io import BytesIO
        with ZipFile(BytesIO(blank_bytes)) as archive:
            if any(n.startswith(('word/embeddings/', 'word/charts/')) or n in {'word/comments.xml','word/footnotes.xml','word/endnotes.xml'} for n in archive.namelist()):
                raise ValueError('新模板仍有未支持清理核验的嵌入、批注或附注部件')
        # Local positions explicitly bind public IDs. Arbitrary F numbers have no inherited execution meaning.
        fields = {}
        for decision in decisions:
            if decision['action'] == 'needs_clarification':
                raise ValueError('待澄清字段不能写成已完成公共定义绑定')
            field = decision['field']
            fid = field.get('field_id')
            standard = decision.get('standard_id') or decision.get('definition', {}).get('standard_id')
            if not fid or fid in fields or not standard or standard not in dictionary['standard_fields']:
                raise ValueError('新模板字段位置重复或未关联已核公共ID；歧义决策须先澄清')
            mids = field.get('input_mapping_ids', [])
            binding = {'standard_id': standard, 'parameters': field.get('parameters', {}), 'input_mapping_ids': mids}
            sys.path.insert(0, str(project/'数据字典/脚本'))
            from catalog_schema import validate_definition_binding
            validate_definition_binding(dictionary, binding, tid + '/' + fid)
            fields[fid] = {'field_id': fid, 'data_definition': binding, 'source_mode': 'manual',
                           'source_candidates': {'mapping_ids':mids,'status':'requires_report_adoption','hypothesis_details':'保留在接入Case，不复制公共接口配置或样本值'},
                           'source_selection': None,
                           'filling_rule': {'label': field.get('name', fid), 'unit': field.get('report_unit', field.get('semantics', {}).get('unit'))},
                           'missing_policy': '来源与执行器未核验，保留待填', 'decision_evidence': decision.get('evidence', [])}
            if field.get('display') is not None:
                fields[fid]['display']=deepcopy(field['display'])
        positions = blank_plan['positions']
        placeholders = {p['placeholder'] for p in positions if p.get('action','placeholder')!='clear'} | {p['placeholder'] for p in blank_plan.get('image_actions', []) if p['action']=='placeholder'}
        if placeholders != {'{{' + fid + '}}' for fid in fields}:
            raise ValueError('空白模板位置与字段决策未一一覆盖')
        # Publish only placeholder positions from the NEW blank document, never source expected_text values.
        public_positions = []
        for paragraph in blank_structure['paragraphs']:
            for fid in fields:
                token = '{{' + fid + '}}'
                for match in re.finditer(re.escape(token), paragraph['text']):
                    public_positions.append({'field_id':fid,'location':paragraph['location'],
                        'start':match.start(),'end':match.end(),'placeholder':token})
        if {p['field_id'] for p in public_positions} != set(fields):
            raise ValueError('新空白模板占位位置缺失')
        filename = '报告模板.docx'
        changes[str(template_rel / filename)] = blank_bytes
        mapping = {'schema_version': 1, 'template_id': tid, 'template_file': filename,
                   'template_sha256': hashlib.sha256(blank_bytes).hexdigest(),
                   'source_sha256': source['sha256'], 'fields': [{'id': fid} for fid in fields],
                   'positions': public_positions, 'retrieval_rules': '通用取值规则.json',
                   'executor': {'id': 'unsupported', 'reason': '新Word位置未通过执行器适配与逐页试填，不能继承旧F号含义',
                                'adaptation_contract': '执行适配说明.json'}}
        rules = {'schema_version': 2, 'rules_version': '1.0-onboarding', 'report_type': report,
                 'template_id': tid, 'source_catalog': 'remote:dictionary_readonly',
                 'template_mapping': '模板字段映射.json', 'fields': fields, 'executor': 'unsupported'}
        changes[str(mapping_rel)] = dump(mapping)
        if template.get('adapter'):
            from mapped_template import validate_adapter
            if report != '运维月报':
                raise ValueError('映射执行器目前只支持运维月报')
            base_dir = project/'报告模板/计划/运维月报/模板/NW-MONTHLY-STD-01'
            rules['adapter'] = deepcopy(template['adapter'])
            rules['executor'] = 'monthly_mapped_v1'
            validate_adapter(rules, mapping, read(base_dir/'通用取值规则.json'), read(base_dir/'模板字段映射.json'))
            executor = 'monthly_mapped_v1'
            mapping['executor'] = {'id':executor, 'base_template_id':'NW-MONTHLY-STD-01', 'photos':'manual_pending'}
            changes[str(mapping_rel)] = dump(mapping)
        changes[str(rules_rel)] = dump(rules)
        changes[str(template_rel / '执行适配说明.json')] = dump({
            'schema_version': 1, 'template_id': tid, 'status': 'mapped_text_supported_photos_pending' if executor=='monthly_mapped_v1' else 'executor_requires_adaptation',
            'source_document_sha256': source['sha256'], 'word_positions': public_positions,
            'public_bindings': {fid: f['data_definition'] for fid, f in fields.items()},
            'source_candidates': {fid: f['source_candidates'] for fid, f in fields.items()},
            'next_steps': ['按每个公共D及语义参数比较既有模板字段，可复用取数方法而不继承局部F号',
                           '选择并核验SM、响应路径与输入契约；字段来源猜测不得作为真实值',
                           '实现位置驱动的单值/记录/照片适配或确认结构可转为已支持模板',
                           '增加跨字段与单位校验，运行check、试填并逐页查看PNG后再登记执行器'],
            'automatic_generation_supported': executor=='monthly_mapped_v1', 'photo_generation_supported':False})
    else:
        raise ValueError('模板选择须为reuse或new；需扩展时先建立明确版本')
    cfg_rel = station_rel / '电站配置.json'
    relative_rules = os.path.relpath(project / rules_rel, project / station_rel)
    cfg = {'schema_version': 1, 'station_id': sid, 'report_type': report, 'template_id': tid,
           'template_mapping': os.path.relpath(project / mapping_rel, project / station_rel),
           'station_ref': os.path.relpath(project / base_rel, project / station_rel),
           'field_configuration': '字段取数配置.json', 'maintenance_rules': '填写规则.json',
           'maintenance_entry': 'AGENTS.md', 'local_sources': '本地资料索引.json',
           'template_rules': {tid: relative_rules}, 'profile': plan.get('profile', {}),
           'production_ready': False, 'default_data_mode': 'powerplus',
           'readiness': {'status': 'pending_trial' if executor != 'unsupported' else 'executor_requires_adaptation',
                         'executor': executor, 'case_id': data['case_id'], 'business_approved': False,
                         'simulation': bool(plan.get('simulation')), 'binding_fingerprint': None}}
    if executor == 'monthly_nw_v1':
        if set(cfg['profile']) != {'cover_name', 'overview_short_name', 'revenue_name'} or any(not isinstance(v, str) or not v.strip() for v in cfg['profile'].values()):
            raise ValueError('月报封面、概况与正文名称必须按本站依据提供')
        evidence(plan.get('profile_evidence'), '报告名称')
        cfg['profile_evidence'] = {'evidence': plan['profile_evidence'], 'status': 'onboarding_reviewed'}
        profile = {'template_rules': relative_rules, 'field_notes': {}, 'field_overrides': {},
                   'source_controls': {}, 'fixedness_decisions': {}, 'calculation_adoptions': {}}
        # Station-period approvals never transfer. Explicit template content stays on its template.
        for fid, f in rules['fields'].items():
            if f.get('fixedness') and fid not in rules.get('template_fixed_content', {}).get('fields', {}):
                profile['fixedness_decisions'][fid] = {'status': 'pending_confirmation', 'value': None, 'scope': None}
        field_config = {'schema_version': 2, 'station_id': sid, 'template_id': tid,
                        'dictionary_id': rules['dictionary_id'], 'parameters': deepcopy(rules['parameter_bindings']),
                        'profiles': {tid: profile}, 'local_source_index': '本地资料索引.json'}
        notes = {'schema_version': 2, 'station_id': sid, 'report_type': report, 'template_id': tid,
                 'template_rules': {tid: relative_rules}, 'fields': {}, 'repeat_groups': {}}
    else:
        field_config = {'schema_version': 2, 'station_id': sid, 'template_id': tid,
                        'status': 'executor_requires_adaptation', 'field_ids': list(rules['fields'])}
        notes = {'schema_version': 2, 'station_id': sid, 'report_type': report, 'template_id': tid,
                 'status': 'executor_requires_adaptation', 'fields': {}}
    if executor == 'monthly_mapped_v1':
        source_rel = station_rel/'取数适配'
        source_tid = 'NW-MONTHLY-STD-01'
        base_template_rel = Path('报告模板/计划/运维月报/模板')/source_tid
        source_rules = read(project/base_template_rel/'通用取值规则.json')
        source_rule_ref = os.path.relpath(project/base_template_rel/'通用取值规则.json',project/source_rel)
        if set(cfg['profile']) != {'cover_name','overview_short_name','revenue_name'} or any(not isinstance(v,str) or not v.strip() for v in cfg['profile'].values()):
            raise ValueError('映射月报仍须提供本站的三种名称绑定')
        evidence(plan.get('profile_evidence'),'报告名称')
        source_cfg = {'schema_version':1,'station_id':sid,'report_type':'运维月报','template_id':source_tid,
            'station_ref':os.path.relpath(project/base_rel,project/source_rel),
            'template_mapping':os.path.relpath(project/base_template_rel/'模板字段映射.json',project/source_rel),
            'field_configuration':'字段取数配置.json','maintenance_rules':'填写规则.json','maintenance_entry':'AGENTS.md',
            'local_sources':'本地资料索引.json','template_rules':{source_tid:source_rule_ref},
            'profile':deepcopy(cfg['profile']),'profile_evidence':{'evidence':plan['profile_evidence']},
            'production_ready':False,'usage':'此配置仅为显式映射执行器提供已支持的公共取数语义，不单独登记报告关联'}
        fixed = {fid:{'status':'pending_confirmation','value':None,'scope':None}
                 for fid,f in source_rules['fields'].items() if f.get('fixedness')}
        source_fc={'schema_version':2,'station_id':sid,'template_id':source_tid,
            'dictionary_id':source_rules['dictionary_id'],'parameters':deepcopy(source_rules['parameter_bindings']),
            'profiles':{source_tid:{'template_rules':source_rule_ref,'field_notes':{},'field_overrides':{},
                'source_controls':{},'fixedness_decisions':fixed,'calculation_adoptions':{}}},
            'local_source_index':'本地资料索引.json'}
        source_notes={'schema_version':2,'station_id':sid,'report_type':'运维月报','template_id':source_tid,
            'template_rules':{source_tid:source_rule_ref},'fields':{},'repeat_groups':{}}
        changes[str(source_rel/'电站配置.json')]=dump(source_cfg)
        changes[str(source_rel/'字段取数配置.json')]=dump(source_fc)
        changes[str(source_rel/'填写规则.json')]=dump(source_notes)
        changes[str(source_rel/'本地资料索引.json')]=dump({'station_id':sid,'sources':[]})
        changes[str(source_rel/'AGENTS.md')]='# 映射执行器取数适配\n\n仅引用共用月报取法和本站身份；局部字段到取数语义的关系由新模板adapter声明。禁止引用其他站真实值或固定批准。\n'.encode()
        cfg['source_adapter_config']='取数适配/电站配置.json'
        cfg.pop('profile',None)
        cfg.pop('profile_evidence',None)
        cfg['profile_ref']=cfg['source_adapter_config']+'#/profile'
    changes[str(cfg_rel)] = dump(cfg)
    changes[str(station_rel / '字段取数配置.json')] = dump(field_config)
    changes[str(station_rel / '填写规则.json')] = dump(notes)
    changes[str(station_rel / '本地资料索引.json')] = dump({'station_id': sid, 'sources': [], 'usage': '历史资料只保存在接入Case，日常不补旧值'})
    changes[str(station_rel / 'AGENTS.md')] = ('# ' + base['station_name'] + report + '\n\n身份从station_ref读取。复用所选模板通用规则，只维护本站参数。\n\n不得继承其他电站的实际值、照片、签名、固定批准和限定月份规则。普通缺数保留待填；首次试生成逐页验收后才启用日常生成。\n').encode()
    changes[str(station_rel / '接入依据.json')] = dump({'case_id': data['case_id'], 'kind': data['kind'],
        'source_sha256': source['sha256'] if source else None, 'identity_evidence': plan['identity']['evidence'],
        'template_fit': template, 'profile_evidence': plan.get('profile_evidence', []),
        'pending_questions': plan.get('pending_questions', []), 'contract_context': data.get('contract_context'),
        'simulation': bool(plan.get('simulation')), 'business_approved': False})
    row = {'报告类型': report, '模板编号': tid, '电站编码': sid, '电站名称': base['station_name'],
           '识别别名': '|'.join(base['aliases']), '模板文件': str(Path('模板') / tid / filename),
           '电站配置': str(cfg_rel.relative_to(report_rel)), '关联状态': '接入已登记；生成状态见配置readiness',
           '本站维护规则': str((station_rel / 'AGENTS.md').relative_to(report_rel)),
           '模板通用取值规则': str(rules_rel.relative_to(report_rel))}
    changes[str(report_rel / '模板电站索引.csv')] = csv_bytes(existing_rows, row)
    if report not in catalog:
        changes[str(report_rel / 'README.md')] = ('# ' + report + '\n\n新报告字段和共享模板已登记，执行器尚待适配，不能自动生成当期报告。\n').encode()
        index_rows = rows(project / '报告索引.csv')
        changes['报告索引.csv'] = csv_bytes(index_rows, {'服务大类': category, '报告类型': report,
            '报告目录': str(report_rel), '关联索引': str(report_rel / '模板电站索引.csv'),
            '说明': str(report_rel / 'README.md'), '取值规则入口': str(rules_rel),
            '配置状态': '模板与字段已登记；执行器尚待适配'})
    # Package every generated runtime dependency, never the source DOCX or case directory.
    manifest = read(project.parent.parent / 'distribution-files.json')
    manifest['files'] = sorted(set(manifest['files']) | {'assets/project/' + name for name in changes})
    changes['../../distribution-files.json'] = dump(manifest)
    return {'changes': changes, 'plan': plan, 'case': data, 'plan_sha256': plan_sha256,
            'case_sha256': case_sha256, 'config_path': str(cfg_rel), 'executor': executor,
            'counts': {'definitions_before': definition_count, 'definitions_after': len(dictionary['standard_fields']),
                       'source_mappings_after': len(dictionary['source_mappings']), 'files': len(changes)}}


def validate_plan(project, case):
    built = build_changes(project, case)
    result = {'valid': True, 'case_id': built['case']['case_id'], 'executor': built['executor'],
              'writes': [{'path': p, 'sha256': hashlib.sha256(b).hexdigest()} for p, b in built['changes'].items()],
              **built['counts'], 'business_approved': False, 'can_generate': False}
    write_json(Path(case) / '计划校验.json', result)
    return result


@contextmanager
def locked(project):
    with (Path(project) / '.onboarding.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('另一个接入事务正在执行，请待其结束后重新核对') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _target(project, name):
    project = Path(project).resolve()
    target = (project / name).resolve()
    if name == '../../distribution-files.json':
        return target
    if not target.is_relative_to(project) or '..' in Path(name).parts:
        raise ValueError('事务写入路径越界')
    return target


def _replace(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rollback(project, transaction):
    for item in reversed(transaction['files']):
        target = _target(project, item['path'])
        current = digest(target) if target.exists() else None
        if current not in {item['before_sha256'], item['after_sha256']}:
            raise ValueError('恢复遇到外部修改，禁止覆盖：' + item['path'])
        if item['before'] is None:
            target.unlink(missing_ok=True)
            parent = target.parent
            while parent != Path(project) and parent.is_relative_to(Path(project)):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        else:
            backup = Path(transaction['backup_dir']) / item['before']
            if digest(backup) != item['before_sha256']:
                raise ValueError('事务备份摘要错误')
            _replace(target, backup.read_bytes())


def recover(project):
    project = Path(project).resolve()
    with locked(project):
        marker = project / '.onboarding-transaction.json'
        transaction = read(marker)
        _rollback(project, transaction)
        marker.unlink()
        record = Path(transaction['case_path']) / '已应用.json'
        if record.exists() and read(record).get('case_id') == transaction['case_id']:
            record.unlink()
        return {'status': 'rolled_back', 'case_id': transaction['case_id']}


def apply_plan(project, case):
    project, case = Path(project).resolve(), Path(case).resolve()
    with locked(project):
        if (project / '.onboarding-transaction.json').exists():
            raise ValueError('未完成事务须先执行onboard recover')
        record_path = case / '已应用.json'
        if record_path.exists():
            applied = read(record_path)
            if applied['plan_sha256'] == digest(case / '接入计划.json') and all(
                    (_target(project, p).exists() and digest(_target(project, p)) == sha)
                    for p, sha in applied['written_sha256'].items()):
                return {**applied, 'status': 'already_applied'}
            raise ValueError('此计划已应用但当前配置变化；禁止重复覆盖')
        built = build_changes(project, case)
        changes = built['changes']
        # Check the isolated candidate before writing official configuration.
        # The remote provider must support the execution schema to reach here.
        with tempfile.TemporaryDirectory(prefix='xiehe-apply-stage-') as tmp:
            stage_skill = Path(tmp) / project.parent.parent.name
            shutil.copytree(project.parent.parent, stage_skill, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.onboarding*'))
            stage = stage_skill / 'assets/project'
            for name, content in changes.items():
                _replace(_target(stage, name), content)
            check = subprocess.run([sys.executable, '-B', str(stage / 'report.py'), 'check'], cwd=stage, capture_output=True, text=True)
            write_json(case / '应用前配置检查.json', {'returncode': check.returncode, 'stdout': check.stdout, 'stderr': check.stderr})
            if check.returncode:
                raise ValueError('候选完整配置检查失败，正式项目未写入：' + check.stderr[-3000:])
        if built['plan']['base_fingerprint'] != fingerprint(project):
            raise ValueError('候选校验期间项目被外部修改，取消应用')
        if (built['plan_sha256'] != digest(case/'接入计划.json')
                or built['case_sha256'] != digest(case/'接入记录.json')):
            raise ValueError('候选校验期间Case或计划被修改，取消应用')
        verify_evidence_references(built['plan'], case)
        backup_dir = case / '事务备份' / uuid.uuid4().hex
        backup_dir.mkdir(parents=True)
        entries = []
        for i, (name, content) in enumerate(changes.items()):
            path = _target(project, name)
            before = str(i) if path.exists() else None
            if before:
                shutil.copy2(path, backup_dir / before)
            entries.append({'path': name, 'before': before, 'before_sha256': digest(path) if before else None,
                            'after_sha256': hashlib.sha256(content).hexdigest()})
        transaction = {'case_id': built['case']['case_id'], 'case_path': str(case), 'backup_dir': str(backup_dir), 'files': entries}
        marker = project / '.onboarding-transaction.json'
        write_json(marker, transaction)
        try:
            for name, content in changes.items():
                _replace(_target(project, name), content)
            applied = {'status': 'applied_pending_trial', 'case_id': built['case']['case_id'],
                       'applied_at': now(), 'plan_sha256': built['plan_sha256'],
                       'config_path': built['config_path'], 'executor': built['executor'],
                       'written_sha256': {e['path']: e['after_sha256'] for e in entries}, **built['counts'],
                       'business_approved': False, 'can_generate': False}
            write_json(record_path, applied)
            marker.unlink()
        except BaseException:
            _rollback(project, transaction)
            marker.unlink(missing_ok=True)
            record_path.unlink(missing_ok=True)
            raise
        return applied


def trial_record_fingerprint(record):
    material = deepcopy(record)
    material.pop('status', None)
    material.get('render', {}).pop('visual_review', None)
    material.get('render', {}).pop('reviewed_pages', None)
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def trial(project, case, out, offline=False, render=True, profile='me'):
    project, case = Path(project).resolve(), Path(case).resolve()
    plan = read(case / '接入计划.json')
    applied = read(case / '已应用.json')
    if applied['executor'] not in {'monthly_nw_v1','monthly_mapped_v1'}:
        raise ValueError('执行器尚需适配，详见新模板/执行适配说明.json；禁止套用旧F号填值')
    if applied['plan_sha256'] != digest(case / '接入计划.json'):
        raise ValueError('应用后计划变化；须重新核对')
    cfg = project / applied['config_path']
    route = route_request(project, read(case / '接入记录.json')['request'], plan['identity']['station_id'],
                          plan['period'], plan['report_type'], plan['template']['template_id'], allow_pending=True)
    if route['route'] != 'generate':
        raise ValueError('试生成路由不满足：' + route['reason'])
    sys.path.insert(0, str(project / '报告模板/计划/运维月报/脚本'))
    from generate_report import generate
    result = generate(route['request'], out, profile, render, route['station_id'], route['period'],
                      template_id=route['template_id'], allow_pending=True, offline=offline)
    if result.get('render', {}).get('pages'):
        result['render']['page_sha256'] = {p:digest(p) for p in result['render']['pages']}
    result['onboarding_trial'] = {'case_id': applied['case_id'], 'plan_sha256': applied['plan_sha256'],
        'binding_fingerprint': binding_fingerprint(project, cfg), 'simulation': bool(plan.get('simulation')),
        'query_mode': 'offline_missing_only' if offline else 'live', 'configuration_checked': True,
        'business_approved': False}
    write_json(Path(out) / '运行记录.json', result)
    write_json(case / '首次试生成.json', {'run': str(Path(out).resolve()), 'docx_sha256': result['docx_sha256'],
                                     'case_id': applied['case_id'], 'status': result['status'],
                                     'trial_record_sha256': trial_record_fingerprint(result)})
    return result


def activate(project, case, run):
    project, case, run = Path(project).resolve(), Path(case).resolve(), Path(run).resolve()
    with locked(project):
        applied = read(case / '已应用.json')
        record = read(run / '运行记录.json')
        claim = record.get('onboarding_trial', {})
        plan = read(case/'接入计划.json')
        receipt = read(case/'首次试生成.json')
        if (digest(case/'接入计划.json') != applied['plan_sha256']
                or Path(receipt['run']).resolve() != run
                or receipt.get('trial_record_sha256') != trial_record_fingerprint(record)):
            raise ValueError('试生成凭据或应用计划被修改，不能启用')
        cfgpath = project / applied['config_path']
        cfg = read(cfgpath)
        if cfg['readiness']['status'] == 'enabled':
            raise ValueError('此关联已启用；不重复验收')
        if claim.get('case_id') != applied['case_id'] or claim.get('plan_sha256') != applied['plan_sha256']:
            raise ValueError('试生成不属于本接入计划')
        if record.get('station_id') != cfg['station_id'] or record.get('template_id') != cfg['template_id']:
            raise ValueError('试生成站点或模板不一致')
        if claim.get('binding_fingerprint') != binding_fingerprint(project, cfgpath):
            raise ValueError('试生成后配置变化，须重新试生成')
        if record.get('status') != 'draft_ready' or record.get('render', {}).get('visual_review') != 'passed':
            raise ValueError('须先逐页查看PNG并执行review，才能启用日常生成')
        docx = Path(record.get('docx', '')).resolve()
        pages = record.get('render', {}).get('pages', [])
        if not docx.is_relative_to(run) or digest(docx) != record['docx_sha256'] or not pages:
            raise ValueError('报告摘要或预览不存在')
        if any(not Path(p).resolve().is_relative_to(run) or not Path(p).is_file() for p in pages):
            raise ValueError('预览页越界或缺失')
        from PIL import Image
        for page in pages:
            if digest(page) != record['render'].get('page_sha256', {}).get(page):
                raise ValueError('预览页摘要变化，须重新渲染验收')
            with Image.open(page) as img:
                if img.format != 'PNG' or min(img.size) < 100:
                    raise ValueError('预览不是有效页面PNG')
                img.verify()
        if record['render'].get('reviewed_pages') != list(range(1, len(pages) + 1)):
            raise ValueError('逐页验收记录不完整')
        if (claim.get('simulation') is not plan.get('simulation')
                or claim.get('simulation') is not cfg['readiness'].get('simulation')
                or claim.get('query_mode') not in {'offline_missing_only', 'live'}):
            raise ValueError('试生成方式与接入计划不一致')
        if claim.get('simulation'):
            sandbox = project/'.onboarding-sandbox.json'
            if not sandbox.is_file() or read(sandbox) != {'project':str(project),'purpose':'isolated_offline_test'}:
                raise ValueError('离线合成验收只能启用隔离测试项目')
        if claim.get('query_mode') == 'offline_missing_only' and not claim.get('simulation'):
            raise ValueError('真实电站须至少尝试一次实际查询，离线测试不能启用真实关联')
        cfg['readiness'].update(status='enabled', enabled_at=now(),
            binding_fingerprint=claim['binding_fingerprint'], trial_docx_sha256=record['docx_sha256'],
            validation_level='offline_simulation' if claim.get('simulation') else 'live_query_attempt_and_visual_review',
            business_approved=False)
        _replace(cfgpath, dump(cfg))
        write_json(case / '接入验收.json', {'status': 'enabled_for_draft', 'station_id': cfg['station_id'],
            'template_id': cfg['template_id'], 'trial_docx_sha256': record['docx_sha256'],
            'validation_level': cfg['readiness']['validation_level'], 'business_approved': False})
        return {'status': 'enabled_for_draft', 'station_id': cfg['station_id'], 'template_id': cfg['template_id'],
                'business_approved': False, 'validation_level': cfg['readiness']['validation_level']}
