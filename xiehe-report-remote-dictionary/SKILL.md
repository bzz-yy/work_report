---
name: xiehe-report-remote-dictionary
description: 用同事服务器当前发布字典，为四个已配置电站生成运维月报待填Word；核对STD字段与已实现取数契约，实际查询Power+、保留证据并逐页验收。也支持实时拉取、检索和兼容检查。旧本地字典版独立保留。
---

# 协合运维报告：服务器字典版

交付指定电站、月份的可编辑运维月报待填版，以及当次字典、取数证据和PNG预览。只读同事服务器的当前发布，不内置公共JSON/Excel、不从旧Skill或历史报告补值。旧`xiehe-report-project`和根`report.py`独立保留。

输入 → 确认电站/月报模板/期间 → 读取服务器发布并核验STD对应 → 实查Power+ → 按模板填Word → 查看原图和全部PNG → 登记待填版验收。

## 运行前

先读[运行README](assets/project/README.md)、[报告索引](assets/project/报告索引.csv)及[运行规则](assets/project/AGENTS.md)。首次使用按[环境说明](references/environment.md)准备宿主Python、数据库读取器、系统SSH、自己的Power+ CLI和登录。凭据放Skill外0600配置，通过`XIEHE_DICTIONARY_CONFIG`提供；用`XIEHE_DICTIONARY_PYTHON`指定有psycopg的Python。运行`scripts/doctor.py`只检查前置环境，不证明站月有数。

`SKILL`为本Skill完整目录，`PYTHON`为宿主Python。所有输出使用Skill外的新目录：

```text
PYTHON SKILL/scripts/run.py dictionary pull --out 新字典证据目录
PYTHON SKILL/scripts/run.py dictionary search --query 发电量 --limit 20
PYTHON SKILL/scripts/run.py dictionary check --out 新兼容检查目录
PYTHON SKILL/scripts/run.py check
PYTHON SKILL/scripts/run.py generate --request '生成济南迈大2026年8月运维月报' --out 新报告目录
```

每个顶层命令重新读取三个发布视图，同一次生成仅使用一个已核验发布；传给Power读取子进程的是本次内存执行规格。连接失败、混版、字段或合同漂移即停止，不回读证据快照作为运行源。

## 本版适配范围

本版依据同事2026-09-11最新回传和服务器`DICT-R4803`实查结构，维护14个明确STD对应。档案编码/名称、结算三项电量和结算子表抄表照片已有命名只读读取器；结算工单编号及期间作为同一父单查询的核验上下文。当前容量、故障损失、工作类别和计划说明只保留映射，完整报告适用的取法/单位/期间尚不足时不启用。

模板有效数据引用使用STD；旧D/SM仅在`previous_*`和对照配置中追溯。未匹配的Word需求保留人工待填、请求、本站配置或已批准模板内容，不伪造新的公共定义。未匹配项与已配置合同失效不同：后者必须停止；只有普通平台缺数/接口失败才按本次证据待填。详细字段与读取器边界见[服务器适配说明](references/server-adapter.md)。

当前支持原四站的`NW-MONTHLY-STD-01`月报。其他报告、任意新模板和新版新增接入事务未适配，不能借用旧D/SM接入命令宣称接通。

## 取值与版式

生成前读[报告取值规则](assets/project/报告取值规则说明.md)。真实数值按本站编码、业务月份、唯一父/子表关联核验。空值不补0，异常0保留待核，多记录不取首条或相加；缺任一月份不把部分合计当年度累计。照片保留原字节、比例、水印及来源关联。

14个已批准模板固定位置继续保留：取证进展和票备注有意空白，四票不合格数0，隐患表固定一行；它们不是平台实值，不把批准扩展到其他位置。本站名称与当前平台档案名分开，当前档案不替代历史容量。

生成后实际查看全部原图和最终PNG。水印站点/日期冲突的图片不得进入交付。需排除时，在Skill外记录本次站月图片审核JSON，并用`generate --photo-review 文件 --out 新目录`重新实时生成；格式见[适配说明](references/server-adapter.md)。新图片或摘要变化须重新查看，不能用旧审核替代。

```text
PYTHON SKILL/scripts/run.py review --run 运行目录 --docx-sha256 运行记录中的摘要 --pages 1 2 3 ...
```

页码必须覆盖实际看过的全部页面。配置、DOCX或PNG变化后不能沿用旧review。`--no-render`只用于结构测试；`draft_ready`仅是待填版版式验收，正式业务批准另行处理。

## 交付与维护

交付DOCX、最终预览、缺项说明和当次证据；分别报告环境、发布兼容、实际取数、版式和业务批准。运行代码为`remote_catalog.py`、`remote_power.py`、`remote_monthly.py`及已有低层Word填充/渲染工具，不调用旧完整字典解析链。

服务端更新后先比较实际字段与合同，验证已实现读取器再更新对应摘要；禁止只改摘要或允许标志让检查通过。分发用`package --out 新ZIP路径`，白名单不含凭据、公共字典、当次快照或报告输出。开发回归用工作区`report-remote.py test`，安装包无需开发测试样本。
