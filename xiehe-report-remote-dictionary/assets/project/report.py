#!/usr/bin/env python3
"""协合报告项目唯一外部CLI：生成、检查、回归测试、Skill打包。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'公共脚本'))
sys.path.insert(0, str(ROOT/'数据字典/脚本'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    gen = sub.add_parser('generate', help='一句话生成运维月报待填版')
    gen.add_argument('--request', required=True)
    gen.add_argument('--station-id'); gen.add_argument('--period'); gen.add_argument('--template-id')
    gen.add_argument('--profile', default='me'); gen.add_argument('--out', type=Path)
    gen.add_argument('--no-render', action='store_true', help='仅用于结构测试')
    gen.add_argument('--photo-review', type=Path, help='本次站月原图审核JSON；仅用于有明确证据的纳入/排除，不作为历史图片源')
    audit = sub.add_parser('check', help='校验字典、接口契约、模板映射和电站版本关联')
    audit.add_argument('--out', type=Path)
    sub.add_parser('test', help='运行现行模块回归测试；不访问网络')
    package = sub.add_parser('package', help='打包独立的服务器字典版Skill')
    package.add_argument('--out', type=Path, required=True)
    package.add_argument('--skill', choices=['xiehe-report-remote-dictionary'], default='xiehe-report-remote-dictionary')
    developer_package = sub.add_parser('package-project', help='导出精简源码、模板、Skill和必要离线测试样本')
    developer_package.add_argument('--out', type=Path, required=True)
    inspect_run = sub.add_parser('audit', help='统计测试报告的实际已填、部分已填和缺项')
    inspect_run.add_argument('--run', type=Path, required=True)
    inspect_run.add_argument('--out', type=Path, required=True)
    review = sub.add_parser('review', help='逐页查看PNG后记录待填版验收')
    review.add_argument('--run', type=Path, required=True)
    review.add_argument('--docx-sha256', required=True)
    review.add_argument('--pages', nargs='+', type=int, required=True)
    route = sub.add_parser('route', help='统一识别电站、报告及模板，返回生成/接入/澄清分支')
    route.add_argument('--request', required=True)
    for key in ('station-id', 'period', 'report-type', 'template-id'):
        route.add_argument('--' + key)
    dictionary = sub.add_parser('dictionary', help='实时拉取、检索并检查同事服务器发布字典')
    phases = dictionary.add_subparsers(dest='dictionary_phase', required=True)
    for phase in ('pull', 'check'):
        item = phases.add_parser(phase)
        item.add_argument('--out', type=Path, required=True)
    search = phases.add_parser('search')
    search.add_argument('--query', required=True)
    search.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if args.command == 'dictionary':
        sys.path.insert(0, str(ROOT / '数据字典/脚本'))
        import remote_catalog
        if args.dictionary_phase == 'search':
            result = remote_catalog.search(args.query, args.limit)
        else:
            result = remote_catalog.write_evidence(args.out, ROOT)
            if args.dictionary_phase == 'pull':
                result['status'] = 'server_snapshot_saved'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if args.dictionary_phase == 'check' and not result['generation_ready'] else 0
    if args.command == 'generate':
        from report_route import route_request
        routed = route_request(ROOT, args.request, args.station_id, args.period, template_id=args.template_id)
        if routed['route'] != 'generate':
            print(json.dumps(routed, ensure_ascii=False, indent=2))
            return 2
        from remote_monthly import generate
        result = generate(args.request, args.out, args.profile, not args.no_render, args.station_id, args.period, template_id=args.template_id, photo_review=args.photo_review)
    elif args.command == 'route':
        from report_route import route_request
        result = route_request(ROOT, args.request, args.station_id, args.period, args.report_type, args.template_id)
    elif args.command == 'check':
        from remote_monthly import check_config
        result = check_config(ROOT)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    elif args.command == 'test':
        tests = ROOT.parents[3] / '开发/远程字典测试'
        if not tests.is_dir():
            raise ValueError('分发包只包含运行资源；回归测试请在开发项目执行。')
        return subprocess.call([sys.executable, '-B', '-m', 'unittest', 'discover',
                                '-s', str(tests), '-p', 'test_remote_*.py'], cwd=ROOT)
    elif args.command == 'audit':
        from audit_run import audit_run
        result = audit_run(args.run, args.out)
    elif args.command == 'package-project':
        from package_skill import package_project
        result = package_project(ROOT,args.out)
    elif args.command == 'package':
        from package_skill import package_skill
        result = package_skill(ROOT, args.out, args.skill)
    else:
        run = args.run.resolve(); path = run/'运行记录.json'
        result = json.loads(path.read_text(encoding='utf-8'))
        from fixed_fields import validate_run_fixed_content
        validate_run_fixed_content(result, json.loads((run/'取数结果.json').read_text(encoding='utf-8')))
        docx = Path(result['docx'])
        digest = hashlib.sha256(docx.read_bytes()).hexdigest()
        render = result.get('render', {})
        expected = list(range(1, render.get('page_count', 0)+1))
        if (not expected or sorted(args.pages) != expected or digest != args.docx_sha256
                or digest != result['docx_sha256'] or not all(Path(p).is_file() for p in render['pages'])
                or result['status'] not in {'rendered_pending_visual_review', 'draft_ready'}):
            raise ValueError('验收须对应未变化的DOCX和全部已查看页面')
        for name, expected_digest in result.get('configuration_sha256', {}).items():
            if not Path(name).is_file() or hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected_digest:
                raise ValueError('报告使用的配置已变化，须重新生成和渲染后验收')
        for page, expected_digest in render.get('page_sha256', {}).items():
            if not Path(page).is_file() or hashlib.sha256(Path(page).read_bytes()).hexdigest() != expected_digest:
                raise ValueError('报告预览已变化，须重新生成和查看')
        if result.get('onboarding_trial'):
            from PIL import Image
            for page in render['pages']:
                if hashlib.sha256(Path(page).read_bytes()).hexdigest() != render.get('page_sha256', {}).get(page):
                    raise ValueError('接入试生成预览摘要变化，须重新渲染')
                with Image.open(page) as img:
                    if img.format != 'PNG' or min(img.size)<100: raise ValueError('预览不是有效页面PNG')
                    img.verify()
        result['status'] = 'draft_ready'
        result['render']['visual_review'] = 'passed'
        result['render']['reviewed_pages'] = args.pages
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ModuleNotFoundError as exc:
        print('未完成：当前 Python 缺少依赖 ' + str(exc.name) + '；请运行 scripts/doctor.py 并按 references/environment.md 配置运行环境。', file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        print('未完成：'+str(exc), file=sys.stderr)
        raise SystemExit(2)
