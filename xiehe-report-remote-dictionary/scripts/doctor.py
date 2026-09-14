#!/usr/bin/env python3
"""Read-only local prerequisites; never read credentials or claim authentication."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess

skill = Path(__file__).resolve().parents[1]
packaged = skill / 'assets/project'
project = packaged if (packaged / 'report.py').is_file() else None
cli = Path.home() / 'Library/Application Support/xhyw-power-cli'
renderers = [Path.home() / '.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime/plugins/documents/skills/documents/render_docx.py']
renderers += list((Path.home() / '.codex/plugins/cache/openai-primary-runtime/documents').glob('*/skills/documents/render_docx.py'))
modules = {name: importlib.util.find_spec(name) is not None for name in ('docx', 'lxml', 'pypdf', 'PIL')}
result = {
    'project': str(project) if project else None,
    'python': sys.executable,
    'python_modules': modules,
    'platform': sys.platform,
    'power_command': shutil.which('power'),
    'power_adapter_python_exists': (cli / 'runtime/python/bin/python3').is_file(),
    'codex_renderer_exists': any(p.is_file() for p in renderers),
    'login_and_station_permissions': '未检查；实际只读查询才能核验',
    'font_and_render_result': '未检查；实际渲染及逐页查看才能核验',
    'supported_validation_environment': 'Mac上的Codex；其他环境未验证',
}
dictionary_python = os.environ.get('XIEHE_DICTIONARY_PYTHON') or sys.executable
try:
    probe = subprocess.run([dictionary_python, '-c', 'import psycopg'], capture_output=True, timeout=10)
    dictionary_driver = probe.returncode == 0
except (OSError, subprocess.TimeoutExpired):
    dictionary_driver = False
result['remote_dictionary'] = {
    'python': dictionary_python,
    'psycopg_available': dictionary_driver,
    'ssh_command': shutil.which('ssh'),
    'external_config_supplied': bool(os.environ.get('XIEHE_DICTIONARY_CONFIG')),
    'database_password_env_supplied': bool(os.environ.get('XIEHE_DICTIONARY_PASSWORD')),
    'connection_and_publication': '未检查；运行 dictionary pull 实查',
    'report_compatibility': '运行 dictionary check 核验当前STD发布和命名读取器；环境存在不代表站月有数',
}
result['local_prerequisites_found'] = bool(project and all(modules.values()) and
    result['power_command'] and result['power_adapter_python_exists'] and result['codex_renderer_exists']
    and dictionary_driver and result['remote_dictionary']['ssh_command'])
print(json.dumps(result, ensure_ascii=False, indent=2))
