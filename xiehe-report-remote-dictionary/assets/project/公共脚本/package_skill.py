"""Package only the explicit distribution allowlist; no source-checkout fallback."""
from pathlib import Path, PurePosixPath
import hashlib
import json
from zipfile import ZipFile, ZIP_DEFLATED


def skill_root(project):
    project = Path(project).resolve()
    skill = project.parent.parent
    if project != skill / 'assets/project' or not (skill / 'SKILL.md').is_file():
        raise ValueError('运行资源必须属于完整 Skill 的 assets/project 目录。')
    return skill


def distribution_files(project):
    skill = skill_root(project)
    config = json.loads((skill / 'distribution-files.json').read_text(encoding='utf-8'))
    names = config['files']
    if config.get('schema_version') != 1 or len(names) != len(set(names)):
        raise ValueError('分发清单版本无效或存在重复路径')
    if config.get('skill_name') != 'xiehe-report-remote-dictionary':
        raise ValueError('分发清单必须标识独立的服务器字典版')
    files = {}
    for name in names:
        rel = PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts:
            raise ValueError('分发清单路径越界：' + name)
        path = skill / name
        if not path.is_file() or skill not in path.resolve().parents:
            raise ValueError('分发文件缺失或越界：' + name)
        if any(p.is_symlink() for p in [path, *path.parents] if p != skill and skill in p.parents):
            raise ValueError('安装包不能依赖符号链接：' + name)
        files[name] = path
    return skill, files


def package_project(project, output):
    raise ValueError('开发项目导出请在工作区根目录执行 report.py package-project；安装包不含开发样本。')


def package_skill(project, output, skill_name='xiehe-report-remote-dictionary'):
    project, output = Path(project).resolve(), Path(output).resolve()
    if skill_name != 'xiehe-report-remote-dictionary':
        raise ValueError('未知Skill')
    skill, files = distribution_files(project)
    if output == skill or skill in output.parents:
        raise ValueError('安装包应输出到 Skill 目录外')
    prohibited = {'assets/project/数据字典/数据字典.json', 'assets/project/数据字典/报告数据字典.xlsx',
                  'assets/project/数据字典/脚本/export_excel.mjs', 'assets/project/数据字典/脚本/dictionary_rows.mjs'}
    if prohibited & set(files) or any((skill / name).exists() for name in prohibited):
        raise ValueError('服务器版不能内置公共字典或本地字典维护脚本')
    config = json.loads((project / '数据字典/服务器连接.json').read_text(encoding='utf-8'))
    if any(k in config for k in ('password', 'ssh_password', 'token', 'secret')):
        raise ValueError('分发连接配置不能包含凭据')
    manifest = {name: hashlib.sha256(p.read_bytes()).hexdigest() for name, p in sorted(files.items())}
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'x', ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, skill_name + '/' + name)
        archive.writestr(skill_name + '/package-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    with ZipFile(output) as archive:
        for name, expected in manifest.items():
            if hashlib.sha256(archive.read(skill_name + '/' + name)).hexdigest() != expected:
                raise ValueError('Skill打包后摘要不一致：' + name)
    return {'package': str(output), 'files': len(files), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'runtime': 'assets/project', 'history_required': False,
            'new_station_end_to_end_verified': False,
            'generation_ready': None, 'live_check_required': True, 'dictionary_source': 'live_readonly_postgresql',
            'dictionary_contract_status': 'reviewed_reader_contracts_require_current_publication',
            'requirements': ['Python依赖见assets/project/requirements.txt', '内网/VPN、SSH认证和数据库只读密码、psycopg', '使用人自己的Power+ CLI及登录会话',
                             '文档渲染器和中文字体；当前迁移验证环境限Mac上的Codex']}
