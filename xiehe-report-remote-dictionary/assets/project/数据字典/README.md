# 服务器公共数据字典读取

公共定义和取法不在本地维护。通过系统SSH、psycopg只读事务读取dictionary_readonly的current_published_fields、current_published_field_aliases、current_published_value_contracts。个人连接文件由Skill外0600配置提供。

新发布包含query_condition.retrievalRule、result_path等列。读取器按明确STD对应和已核验内容摘要选择命名实现，不执行任意文字命令。当前模板绑定在报告模板/计划/运维月报/模板/NW-MONTHLY-STD-01/远程字段对应.json，它不是公共定义副本。

`remote_catalog`读取/校验，`remote_power`按本次内存规格查询，`remote_monthly`处理报告位置与显示。原数据定义未匹配的人工/模板需求明确待填；已配置合同缺失或变化停止。详细协议、支持范围和当次证据见[服务器适配说明](../../../references/server-adapter.md)。

本次证据可输出到Skill外，下一次命令仍实时读取。未匹配来源、合同不完整、平台缺数、版式验收和业务批准分开说明。
