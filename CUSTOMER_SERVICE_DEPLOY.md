# 客服岗位增量部署说明

本压缩包按项目根目录保留路径。将压缩包内容解压到已部署项目根目录并覆盖同名文件即可。

## 部署机 `.env` 配置

为保护部署环境中的密钥和既有岗位配置，本压缩包不包含 `.env`。请把下面三项补充到部署机原有 `.env`，并填写实际值：

```dotenv
FEISHU_TABLE_CUSTOMER_SERVICE=
BOSS_JOB_NAME_CUSTOMER_SERVICE=
BOSS_JOB_ID_CUSTOMER_SERVICE=
```

- `FEISHU_TABLE_CUSTOMER_SERVICE`：客服岗位独立飞书数据表 ID；启用自动建表时可以暂时留空。
- `BOSS_JOB_NAME_CUSTOMER_SERVICE`：BOSS 后台显示的客服岗位名称。
- `BOSS_JOB_ID_CUSTOMER_SERVICE`：客服岗位的 `encrypt_job_id`。

## 默认安全状态

`config/app.yaml` 中客服岗位的 `count`、`max_greetings`、`daily_greet_target` 均为 `0`，覆盖部署后不会自动筛选或打招呼。确认上述环境变量后，再按实际需要设置执行配额和时间窗口。

## 客服卡片预筛规则

- 最近一份工作经历必须明确结束于 `2026.08` 或之后，或标注“至今”；结束时间缺失、无法解析或早于 `2026.08` 时不打开完整简历。
- 只看最近两份工作经历，其中至少一份岗位名称包含“客服”，且该同一段经历须满 8 个月；岗位名称与时长不能取自不同经历。
- 卡片规则不满足或关键信息缺失时直接拦截，不进入在线简历页面。

## 数据隔离

客服使用独立 BOSS 岗位 ID、独立飞书表变量，并继续按 `encrypt_job_id` 创建运行和状态目录，不与其他岗位共用候选池、简历或打招呼记录。
