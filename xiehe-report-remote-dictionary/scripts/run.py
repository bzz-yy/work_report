#!/usr/bin/env python3
"""Run the bundled project; paths supplied by the caller keep their meaning."""
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import uuid

skill = Path(__file__).resolve().parents[1]
project = skill / 'assets/project'
if not (project / 'report.py').is_file():
    raise SystemExit('运行资源缺失，请安装整个 xiehe-report-remote-dictionary 文件夹。')
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg in {'--out', '--run', '--case', '--sample', '--source', '--positions', '--fields', '--photo-review'} and i + 1 < len(args):
        args[i + 1] = str(Path(args[i + 1]).expanduser().resolve())
    elif arg.startswith(('--out=', '--run=', '--case=', '--sample=', '--source=', '--positions=', '--fields=', '--photo-review=')):
        key, value = arg.split('=', 1)
        args[i] = key + '=' + str(Path(value).expanduser().resolve())
if args and args[0] == 'generate' and not any(a == '--out' or a.startswith('--out=') for a in args):
    base = Path.cwd().resolve()
    if base == skill or skill in base.parents:
        base = Path.home() / 'Documents'
    output = base / '报告输出' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    args += ['--out', str(output)]
for i, arg in enumerate(args):
    value = args[i + 1] if arg == '--out' and i + 1 < len(args) else arg[6:] if arg.startswith('--out=') else None
    if value and (Path(value) == skill or skill in Path(value).parents):
        raise SystemExit('输出目录必须放在 Skill 安装目录之外，以免更新或清理时丢失结果。')
raise SystemExit(subprocess.call([sys.executable, '-B', str(project / 'report.py'), *args], cwd=project))
