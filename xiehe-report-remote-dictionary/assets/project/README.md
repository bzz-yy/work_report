# 远程字典版运行资源

本目录属于独立的`xiehe-report-remote-dictionary`，旧本地字典版保持不变。入口为Skill的`scripts/run.py`，再调用本目录`report.py`。

公共字段和取法每次从服务器当前发布读取；[数据字典](数据字典/README.md)仅存读取/执行代码和非密默认连接配置，没有本地公共JSON或Excel。[报告索引](报告索引.csv)和模板配置保留Word位置、显示规则及本站身份引用。

月报使用`remote_monthly.py`构建STD计划、`remote_power.py`实际查询和校验，再复用低层Word填充及渲染。已匹配且当前合同与读取器一致的来源才执行；未匹配的报告需求保留待填，不调用保留下来的旧完整D/SM解析链。当前支持原四站NW-MONTHLY-STD-01月报；其他报告或新增接入尚未适配。

具体命令、照片审核与交付边界见[Skill](../../SKILL.md)、[运行规则](AGENTS.md)和[适配说明](../../references/server-adapter.md)。依赖存在、发布可读、取得站月值、填入Word和业务批准分别核验。
