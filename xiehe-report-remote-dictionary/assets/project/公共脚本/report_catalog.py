"""Discover report resources from the single service-category/report indexes."""
import csv
from pathlib import Path


SERVICE_CATEGORIES = ('计划', '专项', '消缺', '突发')


def project_root(path):
    path = Path(path).resolve()
    for candidate in (path, *path.parents):
        if ((candidate / '报告索引.csv').is_file()
                and (candidate / '数据字典/服务器连接.json').is_file()
                and (candidate / '电站').is_dir()):
            return candidate
    raise ValueError('找不到完整运行项目根目录')


def _rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def load_catalog(project):
    project = project_root(project)
    categories = _rows(project / '报告模板/分类索引.csv')
    if tuple(row['服务大类'] for row in categories) != SERVICE_CATEGORIES:
        raise ValueError('服务分类须为计划、专项、消缺、突发，且各出现一次')
    for row in categories:
        directory = project / row['分类目录']
        if (directory != project / '报告模板' / row['服务大类']
                or not (directory / 'README.md').is_file()):
            raise ValueError('服务分类目录无效：' + row['服务大类'])
    rows = _rows(project / '报告索引.csv')
    reports = {}
    for row in rows:
        report, category = row['报告类型'], row['服务大类']
        if not report or report in reports or category not in SERVICE_CATEGORIES:
            raise ValueError('报告索引重复或服务分类无效')
        expected = Path('报告模板') / category / report
        if Path(row['报告目录']) != expected:
            raise ValueError('报告目录与服务大类不一致：' + report)
        directory = (project / expected).resolve()
        if not directory.is_relative_to(project / '报告模板' / category):
            raise ValueError('报告目录越出服务分类范围')
        for key in ('关联索引', '说明', '取值规则入口'):
            target = (project / row[key]).resolve()
            if not target.is_relative_to(directory) or not target.is_file():
                raise ValueError('报告索引断链或串报告：' + row[key])
        if '字典补充清单' in row['取值规则入口']:
            raise ValueError('报告索引仍指向旧字典补充清单')
        reports[report] = row
    # Unregistered template/report directories must not silently escape check.
    indexed = {project / row['报告目录'] for row in rows}
    discovered = {p.parent for p in (project / '报告模板').glob('*/*/模板电站索引.csv')}
    templates = {p.parents[2] for p in (project / '报告模板').glob('*/*/模板/*/通用取值规则.json')}
    if discovered != indexed or not templates <= indexed:
        raise ValueError('存在未登记的报告资源或缺失报告关联索引')
    return {'categories': categories, 'reports': reports}


def report_root(project, report_type):
    project = project_root(project)
    reports = load_catalog(project)['reports']
    if report_type not in reports:
        raise ValueError('报告类型未登记：' + report_type)
    return project / reports[report_type]['报告目录']
