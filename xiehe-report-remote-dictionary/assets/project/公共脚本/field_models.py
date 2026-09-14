"""Validate reviewed field meaning and reusable lookup rules without supplying values."""
import hashlib
import json
from pathlib import Path

KINDS={'request','static_identity','effective_dated_attribute','period_measurement',
       'event_records','derived','summary_text','photo_collection'}

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def validate_field_models(common,catalog,project,rule_path):
    if 'field_model_version' not in common:
        return None
    if common['field_model_version']!=1:
        raise ValueError('字段模型版本未支持')
    path=(Path(rule_path).parent/common.get('field_model_validation_ref','')).resolve()
    if not path.is_relative_to(Path(project).resolve()) or not path.is_file():
        raise ValueError('字段模型核验记录不存在或越界')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=common.get('field_model_validation_sha256'):
        raise ValueError('字段模型核验摘要变化')
    proof=json.loads(path.read_text(encoding='utf-8'))
    if proof.get('template_id')!=common['template_id'] or set(proof.get('reviewed_fields',[]))!=set(common['fields']):
        raise ValueError('字段模型独立核验覆盖不完整或串模板')
    required={'kind','meaning','change_rule','derivation','lookup_guide_ids','missing_policy','review_status'}
    for fid,field in common['fields'].items():
        model=field.get('field_model',{})
        if set(model)!=required or model['kind'] not in KINDS:
            raise ValueError('字段模型不完整：'+fid)
        if not all(isinstance(model[k],str) and model[k].strip() for k in ['meaning','change_rule','missing_policy']):
            raise ValueError('字段含义、变化或缺项规则为空：'+fid)
        if not isinstance(model['derivation'],dict) or not model['derivation']:
            raise ValueError('字段缺少来源或计算方法：'+fid)
        ids=model['lookup_guide_ids']
        if not isinstance(ids,list) or len(ids)!=len(set(ids)) or not set(ids)<=set(catalog.get('lookup_guides',{})):
            raise ValueError('字段查找关联缺失或重复：'+fid)
        if model['review_status']!='reviewed_with_input_conditions' or proof.get('field_models',{}).get(fid)!=digest(model):
            raise ValueError('字段方法与独立核验版本不一致：'+fid)
    return str(path)

