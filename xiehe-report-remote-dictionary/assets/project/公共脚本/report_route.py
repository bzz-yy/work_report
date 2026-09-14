"""Read-only request routing using global station identity and explicit readiness."""
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '数据字典/脚本'))
from remote_catalog import catalog_digest
from report_catalog import load_catalog
from request_period import monthly_period


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def station_inventory(project):
    result = []
    for path in sorted((Path(project) / '电站').glob('*/基础配置.json')):
        item = read(path)
        result.append({**item, 'base_file': str(path.resolve())})
    ids = [s['station_id'] for s in result]
    if len(ids) != len(set(ids)):
        raise ValueError('全局电站ID重复')
    return result


def station_text(request, report_type=None):
    """Remove known request syntax, never remove an unknown station suffix."""
    text = request.strip()
    text = re.sub(r'(20\d{2})\s*(?:年|[-/])\s*\d{1,2}\s*月?', '', text)
    text = re.sub(r'上上个?月|上个月|上月|本月|这个月|当月', '', text)
    for phrase in ['月度运维月报', '运维月报', '组件清洗报告', '定检报告', '清洗报告', '月报']:
        text = text.replace(phrase, '')
    if report_type:
        text = text.replace(report_type, '')
    text = re.sub(r'^(?:请|帮我|给我|为我|麻烦|生成|制作|出具|输出|提供|查询|我要|一份|一个|为|给|的|一下|\s)+', '', text)
    text = re.sub(r'(?:的|一份|一版|报告|谢谢|一下|[，。！？!?,：:;；\s])+$', '', text)
    return text.strip()


def template_candidates(project, report_type):
    cat = load_catalog(project)['reports']
    if report_type not in cat:
        return []
    root = Path(project) / cat[report_type]['报告目录']
    result = []
    for path in sorted(root.glob('模板/*/通用取值规则.json')):
        rules = read(path)
        mapping_path = path.parent / '模板字段映射.json'
        mapping = read(mapping_path)
        file = mapping.get('template_file', mapping.get('file'))
        result.append({'template_id': rules['template_id'], 'template_file': str(path.parent / file),
                       'template_sha256': digest(path.parent / file),
                       'mapping_file': str(mapping_path), 'rules_file': str(path),
                       'field_count': len(rules['fields']),
                       'field_summary': [f.get('filling_rule', {}).get('label', fid) for fid, f in list(rules['fields'].items())[:12]],
                       'executor': 'monthly_nw_v1' if report_type == '运维月报' and rules['template_id'] == 'NW-MONTHLY-STD-01' else ('monthly_mapped_v1' if report_type=='运维月报' and rules.get('executor')=='monthly_mapped_v1' else 'unsupported'),
                       'preview': 'Word原件可预览；使用文档渲染器生成PNG后逐页查看',
                       'business_fit': 'requires_evidence'})
    return result


def binding_fingerprint(project, config_path):
    """Bind activation to current identity, template, dictionary and station rules."""
    project, config_path = Path(project).resolve(), Path(config_path).resolve()
    cfg = read(config_path)
    cfg.pop('readiness', None)
    paths = set(config_path.parent.rglob('*.json')) | set(config_path.parent.rglob('*.md'))
    paths.add((config_path.parent / cfg['station_ref']).resolve())
    template = (config_path.parent / cfg['template_mapping']).resolve().parent
    paths.update(p for p in template.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    if cfg.get('source_adapter_config'):
        source_cfg_path=(config_path.parent/cfg['source_adapter_config']).resolve()
        if not source_cfg_path.is_relative_to(config_path.parent):
            raise ValueError('取数适配不能引用本站其他关联')
        source_cfg=read(source_cfg_path)
        base_template=(source_cfg_path.parent/source_cfg['template_mapping']).resolve().parent
        paths.update(p for p in base_template.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    # Stable remote content participates in activation; a snapshot's local path
    # and retrieval timestamp must not invalidate an unchanged server version.
    payload = {'remote:dictionary_readonly': catalog_digest(project)}
    for path in sorted(paths):
        if not path.resolve().is_relative_to(project):
            raise ValueError('接入配置引用越界')
        payload[str(path.relative_to(project))] = (hashlib.sha256(json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                                                  if path == config_path else digest(path))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def validate_binding(cfgpath, cfg, station, report_type, report_root, row, candidate):
    """Resolve legacy inspection profiles without bypassing identity or link checks."""
    template_id = row['模板编号']
    if (cfg.get('station_id') != station['station_id'] or cfg.get('report_type') != report_type
            or row.get('报告类型') != report_type):
        raise ValueError('电站模板关联不一致：报告配置身份或报告类型冲突')
    station_ref = cfg.get('station_ref')
    if not isinstance(station_ref, str) or (cfgpath.parent / station_ref).resolve() != Path(station['base_file']):
        raise ValueError('电站模板关联不一致：station_ref未引用本站根身份')
    if 'template_id' in cfg:
        if cfg['template_id'] != template_id:
            raise ValueError('电站模板关联不一致：配置模板与索引冲突')
    if report_type == '定检报告' and 'profiles' in cfg:
        profiles = cfg['profiles']
        profile = profiles.get(template_id) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict) or profile.get('template_id') != template_id:
            raise ValueError('电站模板关联不一致：本站未配置索引指定的定检版本')
    elif 'template_id' not in cfg:
        raise ValueError('电站模板关联不一致：缺少模板选择配置')
    if candidate is None:
        raise ValueError('电站模板关联不一致：索引模板未登记')

    references = [(report_root, row.get('模板文件'), candidate['template_file'])]
    rules = cfg.get('template_rules')
    if isinstance(rules, dict):
        rules = rules.get(template_id)
    references.append((cfgpath.parent, rules, candidate['rules_file']))
    if 'template_mapping' in cfg:
        references.append((cfgpath.parent, cfg['template_mapping'], candidate['mapping_file']))
    if row.get('模板通用取值规则'):
        references.append((report_root, row['模板通用取值规则'], candidate['rules_file']))
    for parent, reference, expected in references:
        if not isinstance(reference, str) or (parent / reference).resolve() != Path(expected).resolve():
            raise ValueError('电站模板关联不一致：模板文件、映射或取值规则引用冲突')


def route_request(project, request, station_id=None, period=None, report_type=None, template_id=None, allow_pending=False):
    project = Path(project).resolve()
    if (project / '.onboarding-transaction.json').exists():
        raise ValueError('接入事务尚未恢复，先执行 onboard recover，禁止读取半配置')
    cat = load_catalog(project)['reports']
    aliases = {'运维月报': ['月报'], '定检报告': ['定检'], '组件清洗报告': ['清洗报告']}
    detected = [name for name in cat if name in request or any(a in request for a in aliases.get(name, []))]
    if len(detected) > 1 or (report_type and detected and detected != [report_type]):
        return {'route': 'clarification', 'reason': '报告类型冲突', 'can_generate': False}
    report_type = report_type or (detected[0] if detected else None)
    if not report_type:
        return {'route': 'clarification', 'reason': '报告类型未登记，请用--report-type明确新报告名称', 'can_generate': False}
    try:
        period = monthly_period(request, period)
    except ValueError as exc:
        return {'route': 'clarification', 'reason': str(exc), 'report_type': report_type, 'can_generate': False}
    text = station_text(request, report_type)
    stations = station_inventory(project)
    matches = [s for s in stations if text.casefold() in {str(a).casefold() for a in [s['station_id'], s['station_name'], *s.get('aliases', [])]}]
    if station_id:
        exact = [s for s in stations if s['station_id'] == station_id]
        if matches and any(s['station_id'] != station_id for s in matches):
            return {'route': 'clarification', 'reason': '电站参数与请求冲突', 'can_generate': False}
        if text and exact and not matches:
            return {'route': 'clarification', 'reason': '请求中的电站名称与显式ID不一致，未知后缀不可忽略', 'station_text': text, 'can_generate': False}
        matches = exact
    base = {'request': request, 'report_type': report_type, 'period': period, 'station_text': text,
            'template_id': template_id, 'can_generate': False, 'formal_report_ready': False,
            'template_candidates': template_candidates(project, report_type)}
    if len(matches) > 1:
        return {**base, 'route': 'clarification', 'reason': '电站别名对应多个身份', 'station_candidates': [s['station_id'] for s in matches]}
    if not matches:
        return {**base, 'route': 'onboarding', 'reason': 'station_not_registered', 'station_id': station_id, 'station_name': text or None}
    station = matches[0]
    base.update(station_id=station['station_id'], station_name=station['station_name'], station_base_file=station['base_file'])
    if report_type not in cat:
        return {**base, 'route': 'onboarding', 'reason': 'report_type_not_registered'}
    report_root = project / cat[report_type]['报告目录']
    linked = [r for r in rows(report_root / '模板电站索引.csv') if r['电站编码'] == station['station_id'] and r['模板编号']]
    if template_id:
        linked = [r for r in linked if r['模板编号'] == template_id]
    if not linked:
        return {**base, 'route': 'onboarding', 'reason': 'station_report_template_not_linked'}
    if len(linked) != 1:
        return {**base, 'route': 'clarification', 'reason': 'template_version_ambiguous', 'linked_templates': [r['模板编号'] for r in linked]}
    row = linked[0]
    cfgpath = (report_root / row['电站配置']).resolve()
    if not cfgpath.is_relative_to(report_root) or not cfgpath.is_file():
        raise ValueError('报告电站索引路径无效')
    cfg = read(cfgpath)
    candidate = next((c for c in base['template_candidates'] if c['template_id'] == row['模板编号']), None)
    validate_binding(cfgpath, cfg, station, report_type, report_root, row, candidate)
    base.update(template_id=row['模板编号'], station_config=str(cfgpath), index_row=row)
    if candidate['executor'] == 'unsupported':
        return {**base, 'route': 'onboarding', 'reason': 'executor_requires_adaptation'}
    readiness = cfg.get('readiness')
    if not readiness and (station.get('onboarding') or (cfgpath.parent/'接入依据.json').exists()
                          or row.get('关联状态') in {'pending_trial','executor_requires_adaptation'}):
        return {**base, 'route': 'onboarding', 'reason': 'readiness_missing_for_onboarded_binding'}
    if readiness:
        if readiness.get('status') != 'enabled':
            if not allow_pending:
                return {**base, 'route': 'onboarding', 'reason': 'pending_first_trial_or_review', 'readiness': readiness}
        elif readiness.get('binding_fingerprint') != binding_fingerprint(project, cfgpath):
            return {**base, 'route': 'onboarding', 'reason': 'configuration_changed_since_trial'}
    return {**base, 'route': 'generate', 'reason': 'configured_report_available', 'can_generate': True,
            'readiness': readiness or {'status': 'legacy_enabled', 'scope': '原已验证待填版流程，非业务正式批准'}}
