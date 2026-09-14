# 服务器发布与月报执行适配

## 信息来源

每次顶层命令读取`dictionary_readonly.current_published_fields`、`current_published_field_aliases`、`current_published_value_contracts`，使用只读、可重复读事务。`query_condition.retrievalRule`是实际取法文字；`result_path`可能是位置/prop说明，不假设它直接是JSONPath。通用`reader_method`一句话不授予执行权限。

本地`模板/NW-MONTHLY-STD-01/远程字段对应.json`只保存明确的旧编号到STD对应、读取器名称、原返回键及已核验合同摘要，不保存公共定义或取法正文。服务器字段和合同语义内容与命名读取器一致才执行；任意服务器命令文字不作为shell或Python执行。新发布版本若绑定合同内容未变可重用读取器；字段/合同变化须核查并验证适配。

## 当前来源

- D001/D002对应的STD：当前档案stationCode/stationName，身份核验与附页使用，不覆盖报告命名配置。
- D006/D007/D008对应的STD：`form/data/list`，父单`Electricitybill`按整数station和结算月份筛选，父r_id关联`ElectricitybillSettlement`；三项分别取totalpower_name、totalowner_name、totalonline_name，单位kWh。父/子单完整分页、站月和关联核验；唯一记录才输出数值。F004/F005/F006显示万kWh，F054逐月保留kWh。
- D009—D012：同一父单的工单/期间核验上下文；不另启动sendList/todoList读取。
- D019：同一结算子表的meter_reading_photos；与已核验电量合同共同验证关联链，图片无kWh单位。
- D003/D013/D018/D021：保留STD对应和服务器原文，但当前未启用月报填值。容量缺已发布返回键，故障字段缺完整选择/单位规则，工作类别缺完整执行记录，计划描述缺本月及版本选择规则。

未匹配的原照片/附件复算/监控来源不查询。报告人工、请求年月、名称配置、固定内容保留在模板层，不把未匹配的旧D当作新公共定义。原`previous_*`只追溯，不驱动查询。

## 当次证据与错误

运行目录保存原始服务器快照、执行目录证据、平台限字段响应、STD取值、照片和独立下载凭证。子进程通过stdin收到本次经校验的公开规格；它不加载历史快照或旧字典。照片URL只留摘要/脱敏路径，不保存鉴权头、会话或签名参数。

错站、错期、关联/结构冲突、合同漂移会停止。普通缺数、接口不可用、未核验0及多条歧义保持缺项；年度累计需要完整月份输入。记录存在或取法存在不等于业务正式批准。

## 原图审核

在Skill外保存JSON，例如：

```json
{
  "station_id": "XNY086",
  "period": "2026-08",
  "reviewed_at": "带时区的实际审核时间",
  "reviewed_by": "实际检查人",
  "items": [
    {"sha256": "实际原图摘要", "decision": "include", "reason": "实际查看站点和日期的结果"},
    {"sha256": "另一张实际原图摘要", "decision": "exclude", "reason": "实际发现的冲突"}
  ]
}
```

只能填写实际看过的图片摘要；记录须覆盖该次全部照片，站月和摘要必须匹配。不把某次排除写成全局规则，不修改原图。用`generate --photo-review 文件 --out 新目录`重新取数及生成。审核记录随报告保存，最终仍须查看全部PNG。
