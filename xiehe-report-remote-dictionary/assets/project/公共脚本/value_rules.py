"""Shared decimal conversion and formatting, driven by template configuration."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from catalog_schema import validate_conversion


def number(value):
    if value is None or isinstance(value, bool): raise ValueError('缺失或布尔值不是数值')
    try: result=Decimal(str(value).strip())
    except InvalidOperation as exc: raise ValueError('来源不是数值') from exc
    if not result.is_finite() or result < 0: raise ValueError('来源数值非法')
    return result


def render_number(value, display):
    places=display.get('decimal_places')
    if isinstance(places,bool) or not isinstance(places,int) or not 0 <= places <= 10:
        raise ValueError('显示小数位须为0至10的整数')
    if display.get('rounding') != 'half_up': raise ValueError('不支持的舍入方式')
    rounded=number(value).quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP)
    text=format(rounded,'f')
    if display.get('trim_trailing_zeros') and '.' in text:text=text.rstrip('0').rstrip('.')
    return text


def transform(raw, mapping, standard, display):
    validate_conversion(mapping['normalization'],mapping['original_unit'],standard['unit'],mapping['mapping_id'])
    validate_conversion(display,standard['unit'],display['to_unit'],mapping['mapping_id'])
    if mapping['normalization']['operation']=='pending' or display['operation']=='pending':
        raise ValueError('未确认单位转换不能执行')
    normalized=number(raw)*number(mapping['normalization']['factor'])
    return render_number(normalized*number(display['factor']),display)


def validate_selection(selection, rule, catalog):
    required={'mapping_id','operation','display','validation_ref','validation_sha256'}
    if set(selection)!=required:raise ValueError('报告选源配置字段不完整或含未实现项')
    mid=selection['mapping_id']
    if mid not in rule.get('source_mapping_candidates',[]):raise ValueError('选定来源未登记为本字段候选')
    mapping=catalog['source_mappings'][mid]
    if mapping['technical_status']!='verified_source_values':raise ValueError('选定来源的真实返回契约尚未验证')
    if selection['operation'] != 'value':raise ValueError('未实现的报告取值计算，须先实现再配置')
    if selection['operation']=='sum_year_to_date' and rule['filling_rule'].get('group'):
        raise ValueError('累计计算不能作为逐月记录值')
    display=selection['display'];standard=catalog['standard_fields'][mapping['standard_id']]
    if display.get('source_unit')!=standard['unit'] or display.get('to_unit')!=rule['filling_rule'].get('unit'):
        raise ValueError('报告选源的输入或输出单位不一致')
    if not isinstance(display.get('trim_trailing_zeros'),bool):raise ValueError('末尾零显示规则须明确')
    transform('1',mapping,standard,display)
    matched=[a for a in rule.get('report_source_assessments',[]) if mid in a['mapping_ids'] and a['use']=='value_candidate']
    if len(matched)!=1 or matched[0]['status']!='validated_against_history':
        raise ValueError('选定来源缺少本字段的历史对账验证')
