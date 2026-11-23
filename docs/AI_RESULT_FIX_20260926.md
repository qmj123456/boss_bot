# AI 分析结果链路修复（2026-09-26）

修复目录：E:/boss_bot/boss_bot。10 个 config/jobs/*.yaml 文件与修复前 SHA-256 完全一致；未修改招聘条件、阈值、金额/数量排除口径、CET-6 或岗位话术。

## 结果处理

- AI 原始 passed 先规范化为布尔值；兼容明确的符合/合格/ok 等真值和明确假值，未知值进入分析异常。
- 空响应、截断、空对象、必填字段遗漏、证据格式错误、passed 与最终文字结论冲突，进入重试/待回补，不记为候选人不合格。
- 完整证据确实不满足原岗位条件时，仍拒绝。outcomes 明确为空与 outcomes 字段丢失分别处理。
- 仅对当前岗位需要的字段校验。证据补提取不能推翻原始拒绝，也不能抹去求职状态或薪资规则的拒绝。
- 原始响应、传输元数据、JSON 修复、证据补提取、各次失败及最终判定保存在 analysis_audit；不保存请求中的截图编码或凭证。

## 岗位字段

下表使用项目实际配置加载器生成，包含岗位别名解析。

| 岗位 | 顶层输出字段 | 工作经历字段 |
| --- | --- | --- |
| tiktok运营 | passed, reason, employment_status, work_evidence, selected_work_index | title, content, start, end, region |
| 亚马逊运营 | passed, reason, employment_status, work_evidence, selected_work_index | title, content, start, end, region |
| 全职主播 | passed, reason, employment_status | 未配置本地工作证据规则 |
| 兼职主播 | passed, reason, employment_status | 未配置本地工作证据规则 |
| 初级剪拍 | passed, reason, employment_status, work_evidence | title, content, outcomes |
| 客服 | passed, reason, employment_status, expected_salary | 未配置本地工作证据规则 |
| 运营 | passed, reason, employment_status | 未配置本地工作证据规则 |
| 运营助理 | passed, reason, employment_status, work_evidence | title, content, outcomes |
| 运营助理（前期需要在中控岗位轮岗） | passed, reason, employment_status, work_evidence | title, content, outcomes |
| 高级剪拍 | passed, reason, employment_status | 未配置本地工作证据规则 |

平台岗位的日期/地区校验针对相关经历；未配置地区要求时不增加地区限制。没有平台规则的岗位不要求 selected_work_index。未配置求职状态硬筛的岗位不会仅因该字段遗漏被拒绝；有状态名单的岗位仍按原名单执行。其他额外字段保留在原始审计中，不构成别的岗位的硬要求。

## 重试、恢复与打招呼

- 实时分析仅重试已保存截图；成功返回实际布尔 passed 后进入正常招呼路径。
- 批量分析入口补齐 resume_analysis 配置，确保与实时入口使用同一岗位规则。
- 同轮已有完成分析时复用结论，不重复调用 AI 覆盖结果；仅恢复未完成的招呼。
- 已有发送记录不会被后续存储异常改写成未发送。未达到明确招呼成功状态时不标记事务成功。
- 招呼名单严格使用规范布尔值，不接受字符串 false，不允许 recommended 覆盖 passed=false。
- --only-names 的定位兜底不能重新放行已知拒绝或分析异常者；完全没有筛选记录的手工定位模式保持原行为。
- 无新增候选人时保留本轮已有简历、失败记录和结果，不再清空。
- 实时、批量、工作流分析及回补使用运行级操作系统文件锁覆盖完整读写周期。不同运行互不干扰；崩溃后锁自动释放，锁文件可以保留。关闭的处理器不能再写回旧状态，不存在的 run 不会因加锁而被创建。
- 列表停止原因写入 card_prescreen_summary.json.list_diagnostics；接口失败、元数据缺失、重复卡片与明确结束分开记录。所有现有岗位显式配置的工作经历硬要求保留。

## 已保存截图回补入口

在本轮采集/分析结束后运行以下命令，将路径替换为具体运行文件：

```powershell
cd E:\boss_bot\boss_bot
python tools/retry_resume_analysis.py --screening-results "E:\boss_bot\boss_bot\data\runs\岗位ID\runs\运行ID\process\screening_results.json"
```

该命令只重新分析 failures 队列中的候选人，不打开浏览器、不自动发送消息。它按文件中的岗位加载原配置，校验岗位/运行/候选人及截图归属；成功结果写回同轮筛选文件，供既有招呼阶段使用。已有完成结论不被覆盖；重复执行不会重复分析已恢复者。与运行中任务冲突时直接报错，等待该任务结束后再执行。

历史已经保存为正式拒绝的记录不会批量改成通过，需要逐条复核后再决定是否重新分析。修复需重启正在运行的旧脚本进程后生效。本次未调用真实 AI 服务，未联系候选人。

## 验证

最终综合回归：452 passed in 32.62s（21 个相关测试文件）。覆盖全部 10 个岗位输出结构、真假值/结论矛盾、证据缺失与真实不足、JSON/截断/并发审计、截图重试、通过后招呼一次、恢复保留结论、发送后存储失败、数据库一致性、手工名单兜底、候选池退出和跨进程锁。

测试输出见同目录 AI_RESULT_FIX_TEST_OUTPUT.txt。修复前主要源码备份及岗位哈希见 E:/boss_bot/repair_backup_20260926。
