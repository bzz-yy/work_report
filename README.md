# 协合运维报告 Skill：服务器字典版

本仓库保存完整的 `xiehe-report-remote-dictionary` Skill，按项目分发白名单导出，包含使用说明、执行程序、Word 模板及电站配置。

业务输入为电站、报告期间和报告类型；输出为可编辑的运维月报待填 Word、缺项说明、当次查询证据和页面预览。

**执行流程：输入 → 确认电站与模板 → 读取服务器当前发布字典 → 实查 Power+ → 核对站月、单位和来源 → 填写 Word → 逐页检查 → 交付。**

## 从哪里开始

- [Skill 使用说明](xiehe-report-remote-dictionary/SKILL.md)
- [运行环境与外部连接配置](xiehe-report-remote-dictionary/references/environment.md)
- [服务器字段适配与照片审核](xiehe-report-remote-dictionary/references/server-adapter.md)
- [报告取值规则](xiehe-report-remote-dictionary/assets/project/报告取值规则说明.md)
- [报告索引](xiehe-report-remote-dictionary/assets/project/报告索引.csv)

## 仓库结构

```text
xiehe-report-remote-dictionary/
├── SKILL.md                 # Agent 使用说明
├── scripts/                 # 运行入口和环境检查
├── references/              # 环境与适配说明
├── agents/                  # Agent 展示配置
├── assets/project/
│   ├── report.py            # 运行命令入口
│   ├── 公共脚本/            # 路由、核验、报告生成等
│   ├── 数据字典/            # 服务器读取代码与非密连接默认值
│   ├── 报告模板/            # 模板、填写位置、规则与本站适配
│   └── 电站/                # 每站唯一身份配置
├── distribution-files.json  # 分发文件白名单
└── package-manifest.json    # 本次导出文件的 SHA-256 清单
```

完整文件夹是运行和迁移单位；仅复制 `SKILL.md` 或 Word 模板无法完成报告流程。

## 运行准备

按环境说明准备 Python 依赖、系统 SSH、数据库读取器、服务器访问条件、自己的 Power+ CLI 和登录，以及文档渲染工具和中文字体。当前已验证环境为 Mac 上的 Codex。

个人连接配置放在 Skill 和本仓库之外，使用 `XIEHE_DICTIONARY_CONFIG` 指向它；数据库读取 Python 可通过 `XIEHE_DICTIONARY_PYTHON` 单独指定。密码、私钥和平台会话不随仓库提供。

在仓库根目录，使用已准备好依赖的 Python 执行：

```sh
python3 xiehe-report-remote-dictionary/scripts/doctor.py
python3 xiehe-report-remote-dictionary/scripts/run.py --help
python3 xiehe-report-remote-dictionary/scripts/run.py dictionary check --out 报告输出/首次字典检查
python3 xiehe-report-remote-dictionary/scripts/run.py generate --request '生成济南迈大2026年8月运维月报' --out 报告输出/迈大2026-08新运行
```

每次输出使用全新目录。生成后须实际查看原图和全部最终页面，再按 Skill 说明登记版式验收。

## 当前范围

- 已配置济南迈大、中石油济柴、德州保龄宝、潍坊伊利四站的 `NW-MONTHLY-STD-01` 运维月报。
- 已适配的业务来源包括结算电量、逐月台账及结算抄表照片；未匹配项保留待填。
- 新站、新模板接入事务和定检、清洗自动填报尚未适配到本服务器版；目录中存在资源不代表该报告已接通。
- 客户归属标签和按客户推荐模板的机制尚未实现。
- 逐页检查通过只代表待填版版式验收，正式报告还需要补齐缺项和业务批准。

公共数据定义和取法来自服务器当前只读发布。本地只执行已经适配并经核验的读取方式；已绑定字段或取数契约不兼容时停止，不从历史快照或旧版补值。

本仓库仅分发服务器字典版。原本地字典版及开发项目历史继续保留在原项目中。报告输出、原始平台响应、当次服务器快照、个人凭据和开发样本均不在本仓库分发范围内。

`package-manifest.json` 用于核对本次导出时的文件字节；后续修改发布文件时应重新生成清单。完整能力边界以 `SKILL.md` 和运行规则为准。
