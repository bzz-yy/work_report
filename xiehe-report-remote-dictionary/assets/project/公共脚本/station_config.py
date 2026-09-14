"""One station identity shared by all report-specific adaptations."""
import json
from report_catalog import project_root
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_station_config(config_path, read=read_json):
    config_path = Path(config_path).resolve()
    cfg = read(config_path)
    project = project_root(config_path)
    if not cfg.get('station_ref'):
        raise ValueError('报告电站适配缺少基础配置引用')
    forbidden = {'station_name', 'aliases', 'powerplus_station_id', 'powerplus_identity_evidence'}
    if forbidden.intersection(cfg):
        raise ValueError('报告适配不能重复维护电站身份；请修改电站基础配置')
    base_path = (config_path.parent / cfg['station_ref']).resolve()
    if not base_path.is_relative_to(project/'电站') or not base_path.is_file():
        raise ValueError('电站基础配置引用无效')
    base = read(base_path)
    if base['station_id'] != cfg['station_id']:
        raise ValueError('报告适配与电站基础配置串站')
    platform = base['platforms']['powerplus']
    evidence = platform['identity_evidence']
    if (evidence['project_station_id'] != base['station_id']
            or evidence['powerplus_station_id'] != platform['station_code']
            or evidence['powerplus_station_name'] != platform['station_name']
            or evidence['status'] != 'verified_unique_name_match'):
        raise ValueError('电站基础身份与核验证据不一致')
    return {**cfg, 'station_name': base['station_name'], 'aliases': base['aliases'],
            'powerplus_station_id': platform['station_code'],
            'powerplus_identity_evidence': evidence, 'station_base_file': str(base_path)}
