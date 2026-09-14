"""A fresh published server dictionary per command; no bundled or stale fallback."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
VIEWS = ('current_published_fields', 'current_published_field_aliases',
         'current_published_value_contracts')
_snapshot = None
FIELD_FINGERPRINT_KEYS = ('field_code','name_zh','normalized_key','definition_zh','value_type','unit',
    'time_attribute','collection_frequency','missing_policy','quality_rule','manual_override_allowed','valid_from','valid_to')
CONTRACT_FINGERPRINT_KEYS = ('field_code','priority','source_type','reader_method','source_status',
    'valid_from','valid_to','query_condition','result_path','formula')
BINDING_RELATIVE_PATH = '报告模板/计划/运维月报/模板/NW-MONTHLY-STD-01/远程字段对应.json'
READERS = {'station_archive.v1','electricitybill_settlement.v1','electricitybill_parent.v1',
           'electricitybill_photos.v1','not_enabled'}


class RemoteDictionaryCompatibilityError(ValueError):
    pass


def _digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def validate_snapshot(data):
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('source', {}).get('transaction_read_only') is not True):
        raise ValueError('服务器字典缺少有效结构或只读事务证据')
    tables = data.get('tables')
    if not isinstance(tables, dict) or set(tables) != set(VIEWS):
        raise ValueError('服务器字典必须完整包含三个已发布视图')
    required = {
        VIEWS[0]: {'field_code', 'name_zh', 'definition_zh', 'version_no', 'status'},
        VIEWS[1]: {'field_code', 'alias_zh', 'version_no'},
        VIEWS[2]: {'field_code', 'reader_method', 'source_status', 'version_no', 'status'},
    }
    for view, rows in tables.items():
        if not isinstance(rows, list) or len(rows) > 20000:
            raise ValueError('服务器视图返回行数或结构无效：' + view)
        if not required[view] <= set(data.get('columns', {}).get(view, [])):
            raise ValueError('服务器视图列结构变化，须核验适配：' + view)
        if any(not isinstance(row, dict) or not required[view] <= set(row) for row in rows):
            raise ValueError('服务器视图行缺少必需列：' + view)
    fields = tables[VIEWS[0]]
    if not fields:
        raise ValueError('服务器已发布字段为空；不能代入旧字典')
    codes = [field['field_code'] for field in fields]
    if any(not isinstance(code, str) or not code for code in codes) or len(codes) != len(set(codes)):
        raise ValueError('服务器字段编号缺失或重复')
    versions = {row['version_no'] for rows in tables.values() for row in rows}
    if len(versions) != 1 or any(not isinstance(v, str) or not v for v in versions):
        raise ValueError('三个服务器视图发布版本不一致；停止混用')
    if any(row['field_code'] not in codes for view in VIEWS[1:] for row in tables[view]):
        raise ValueError('别名或取值合同引用未知服务器字段')
    return data


def fetch_snapshot():
    """Fetch once in this process; a new CLI process always fetches again."""
    global _snapshot
    if _snapshot is None:
        executable = os.environ.get('XIEHE_DICTIONARY_PYTHON') or sys.executable
        try:
            run = subprocess.run([executable, '-B', str(Path(__file__).with_name('postgres_reader.py'))],
                                 capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise RuntimeError('服务器字典拉取超时；未使用历史快照') from None
        except OSError:
            raise RuntimeError('字典 Python 无法启动；检查 XIEHE_DICTIONARY_PYTHON') from None
        try:
            data = json.loads(run.stdout)
        except (ValueError, TypeError):
            raise RuntimeError('字典连接器未返回有效JSON；检查Python依赖，未使用旧字典') from None
        if not isinstance(data, dict):
            raise RuntimeError('字典连接器返回结构无效；未使用旧字典')
        if run.returncode or data.get('error'):
            raise RuntimeError(data.get('error', '字典连接器执行失败'))
        _snapshot = validate_snapshot(data)
    return deepcopy(_snapshot)


def catalog_digest(project=None):
    snapshot = fetch_snapshot()
    # Capture time and row order do not change the published dictionary identity.
    tables = {view: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
              for view, rows in snapshot['tables'].items()}
    return _digest({'source': snapshot['source'], 'columns': snapshot['columns'], 'tables': tables})


def catalog_metadata(project=None):
    snapshot = fetch_snapshot()
    return {'source': snapshot['source'], 'retrieved_at': snapshot['retrieved_at'],
            'versions': sorted({row['version_no'] for rows in snapshot['tables'].values() for row in rows}),
            'content_sha256': catalog_digest(project),
            'counts': {view: len(rows) for view, rows in snapshot['tables'].items()},
            'historical_fallback_used': False}


def template_requirements(project=None):
    project = Path(project or ROOT)
    standards, mappings, templates = set(), set(), []
    for path in sorted((project / '报告模板').glob('*/*/模板/*/通用取值规则.json')):
        rules = json.loads(path.read_text(encoding='utf-8'))
        ids = set()
        for field in rules.get('fields', {}).values():
            binding = field.get('data_definition', {})
            if binding.get('standard_id'):
                ids.add(binding['standard_id'])
            mappings.update(binding.get('input_mapping_ids', []))
            for key in ('source_selection', 'attachment_selection', 'plan_selection'):
                selection = field.get(key) or {}
                if selection.get('mapping_id'):
                    mappings.add(selection['mapping_id'])
        standards.update(ids)
        templates.append({'template_id': rules['template_id'], 'field_count': len(rules['fields']),
                          'required_standard_ids': sorted(ids)})
    return {'standard_ids': sorted(standards), 'mapping_ids': sorted(mappings), 'templates': templates}


def binding_config(project=None):
    config = json.loads((Path(project or ROOT) / BINDING_RELATIVE_PATH).read_text(encoding='utf-8'))
    if config.get('schema_version') != 1 or config.get('template_id') != 'NW-MONTHLY-STD-01':
        raise ValueError('远程模板字段对应配置无效')
    bindings = config.get('bindings')
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError('缺少有明确依据的远程字段对应')
    codes = set()
    for origin, binding in bindings.items():
        code = binding.get('standard_id')
        if (not isinstance(code, str) or not code.startswith('STD-') or code in codes
                or binding.get('reader') not in READERS
                or not isinstance(binding.get('executable'), bool)
                or len(binding.get('reviewed_contract_sha256', '')) != 64):
            raise ValueError('远程字段对应或命名读取器无效：' + str(origin))
        codes.add(code)
    return config


def contract_fingerprint(field, contract):
    """Bind an implemented reader to reviewed publication content, never to arbitrary prose commands."""
    return _digest({'field': {k: field.get(k) for k in FIELD_FINGERPRINT_KEYS},
                    'contract': {k: contract.get(k) for k in CONTRACT_FINGERPRINT_KEYS}})


def _execution_spec(snapshot, config, metadata):
    fields = {f['field_code']: f for f in snapshot['tables'][VIEWS[0]]}
    grouped = {}
    for c in snapshot['tables'][VIEWS[2]]:
        grouped.setdefault(c['field_code'], []).append(c)
    picked_fields, picked_contracts, blockers = {}, {}, []
    for old_id, binding in config['bindings'].items():
        code = binding['standard_id']
        field, contracts = fields.get(code), grouped.get(code, [])
        if field is None:
            blockers.append({'code': 'PUBLISHED_FIELD_MISSING', 'field_code': code, 'previous_id': old_id})
            continue
        if len(contracts) != 1:
            blockers.append({'code': 'CONTRACT_MISSING_OR_AMBIGUOUS', 'field_code': code, 'count': len(contracts)})
            continue
        contract = contracts[0]
        if field.get('status') != 'active' or contract.get('status') != 'active':
            blockers.append({'code': 'INACTIVE_PUBLICATION', 'field_code': code})
            continue
        if contract_fingerprint(field, contract) != binding['reviewed_contract_sha256']:
            blockers.append({'code': 'PUBLISHED_CONTRACT_CHANGED', 'field_code': code,
                'detail': '当前字段/取法与已核验读取器的合同不同；须先核查差异并验证适配，不自动执行新文字。'})
            continue
        if binding['executable'] and contract.get('source_type') != 'Power+':
            blockers.append({'code': 'SOURCE_CHANGED', 'field_code': code})
            continue
        prop = binding.get('prop')
        rule = (contract.get('query_condition') or {}).get('retrievalRule', '')
        if not isinstance(rule, str) or not rule.strip():
            blockers.append({'code': 'RETRIEVAL_RULE_MISSING', 'field_code': code})
            continue
        if binding['executable'] and prop not in (rule + str(contract.get('result_path', ''))):
            blockers.append({'code': 'RETURN_FIELD_NOT_DOCUMENTED', 'field_code': code})
            continue
        if binding['reader'] == 'electricitybill_settlement.v1' and field.get('unit') != 'kWh':
            blockers.append({'code': 'SETTLEMENT_UNIT_CHANGED', 'field_code': code})
            continue
        picked_fields[code], picked_contracts[code] = deepcopy(field), deepcopy(contract)
    return {'metadata': deepcopy(metadata), 'bindings': deepcopy(config['bindings']),
            'fields': picked_fields, 'contracts': picked_contracts}, blockers


def validate_execution_spec(spec, project=None):
    """Pure child-process validation; does not fetch or read a saved server snapshot."""
    if not isinstance(spec, dict):
        raise ValueError('执行合同必须为对象')
    config = binding_config(project)
    if spec.get('bindings') != config['bindings']:
        raise ValueError('执行字段对应与当前配置不一致')
    metadata = spec.get('metadata', {})
    versions = metadata.get('versions')
    if (metadata.get('source', {}).get('transaction_read_only') is not True
            or not isinstance(versions, list) or len(versions) != 1
            or not isinstance(versions[0], str) or not versions[0]
            or len(metadata.get('content_sha256', '')) != 64
            or not metadata.get('retrieved_at')):
        raise ValueError('执行合同缺少本次发布及只读来源证据')
    required = {b['standard_id'] for b in config['bindings'].values()}
    if set(spec.get('fields', {})) != required or set(spec.get('contracts', {})) != required:
        raise ValueError('执行合同缺少绑定字段或混入额外字段')
    if any(not isinstance(row, dict) or row.get('field_code') != code
           for group in ('fields', 'contracts') for code, row in spec[group].items()):
        raise ValueError('执行合同字段键与公共编号不一致')
    snapshot = {'tables': {VIEWS[0]: list(spec['fields'].values()), VIEWS[2]: list(spec['contracts'].values())}}
    rebuilt, blockers = _execution_spec(snapshot, config, metadata)
    versions = {row.get('version_no') for group in ('fields','contracts') for row in rebuilt[group].values()}
    if blockers or versions != set(metadata['versions']):
        raise ValueError('执行合同不兼容或发布版本不一致：' + json.dumps(blockers, ensure_ascii=False))
    return deepcopy(spec)


def compatibility(project=None):
    snapshot = fetch_snapshot()
    config = binding_config(project)
    metadata = catalog_metadata(project)
    spec, blockers = _execution_spec(snapshot, config, metadata)
    if not blockers:
        validate_execution_spec(spec, project)
    pending = [{'previous_id': d, **b} for d,b in config['bindings'].items() if not b['executable']]
    return {'status': 'remote_dictionary_compatible' if not blockers else 'remote_dictionary_requires_adapter',
            'connected': True, 'generation_ready': not blockers, 'metadata': metadata,
            'template_id': config['template_id'], 'mapped_public_fields': len(config['bindings']),
            'enabled_bindings': sum(b['executable'] for b in config['bindings'].values()),
            'mapped_but_not_enabled': pending, 'unmatched_source_mapping_ids': config['unmatched_source_mapping_ids'],
            'blockers': blockers, 'note': '仅已明确绑定且合同指纹与命名读取器一致的来源可执行；未匹配的报告需求保留待填，不作为接口错误或历史值回退。'}


def load_catalog(project=None):
    result = compatibility(project)
    if not result['generation_ready']:
        raise RemoteDictionaryCompatibilityError('服务器发布与远程月报适配不兼容：' +
            json.dumps(result['blockers'], ensure_ascii=False) + '；未回退旧字典。')
    config = binding_config(project)
    spec, blockers = _execution_spec(fetch_snapshot(), config, result['metadata'])
    if blockers:
        raise RemoteDictionaryCompatibilityError('执行合同构建失败')
    return {'schema_version': 3, 'metadata': result['metadata'], 'fields': deepcopy(spec['fields']),
            'bindings': deepcopy(spec['bindings']), 'execution_spec': validate_execution_spec(spec, project),
            'compatibility': result}


def catalog_path(project=None):
    raise RemoteDictionaryCompatibilityError('远程执行目录在本次进程中传递，不提供本地公共字典文件路径。')


def search(query, limit=20):
    if not query.strip() or not 1 <= limit <= 100:
        raise ValueError('查询词不能为空，limit 须为1至100')
    snapshot = fetch_snapshot()
    aliases = {}
    contracts = {}
    for row in snapshot['tables'][VIEWS[1]]:
        aliases.setdefault(row['field_code'], []).append(row)
    for row in snapshot['tables'][VIEWS[2]]:
        contracts.setdefault(row['field_code'], []).append(row)
    matches = []
    for field in snapshot['tables'][VIEWS[0]]:
        text = ' '.join(str(field.get(k, '')) for k in ('field_code', 'name_zh', 'normalized_key', 'definition_zh'))
        text += ' ' + ' '.join(str(a.get('alias_zh', '')) for a in aliases.get(field['field_code'], []))
        if query.casefold() in text.casefold():
            matches.append({**field, 'aliases': aliases.get(field['field_code'], []),
                            'value_contracts': contracts.get(field['field_code'], [])})
    return {'query': query, 'total_matches': len(matches), 'returned': min(len(matches), limit),
            'fields': matches[:limit], 'metadata': catalog_metadata(),
            'binding_status': 'candidates_only', 'generation_ready': False}


def write_evidence(output, project=None):
    output = Path(output).expanduser().resolve()
    skill = Path(project or ROOT).resolve().parents[1]
    if output.is_relative_to(skill):
        raise ValueError('服务器快照必须放在 Skill 目录外，仅作当次证据')
    snapshot = fetch_snapshot()
    result = compatibility(project)
    output.mkdir(parents=True, exist_ok=False)
    for filename, value in [('服务器字典快照.json', snapshot), ('字典兼容性检查.json', result)]:
        (output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / '接入缺口.md').write_text(
        '# 字典服务器接入检查\n\n'
        f"已发布版本：{','.join(result['metadata']['versions'])}。\n\n"
        '当前已成功拉取字段、别名和取值合同；它们不等于已适配的报告执行字典。\n\n'
        f"当前月报对应 {result['mapped_public_fields']} 个发布编号，"
        f"其中 {result['enabled_bindings']} 个为已实现取数或结算核验上下文；"
        f"兼容检查：{'通过' if result['generation_ready'] else '未通过'}。\n\n"
        '未匹配来源：' + '、'.join(result['unmatched_source_mapping_ids']) + '。\n\n'
        '阻断：' + json.dumps(result['blockers'], ensure_ascii=False) + '\n\n'
        '本次快照仅为证据，不作为下一次运行的字典或历史报告补值来源。\n', encoding='utf-8')
    return {**result, 'evidence_directory': str(output)}
