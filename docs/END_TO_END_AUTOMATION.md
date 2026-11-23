# 端到端招聘自动化

本项目使用已有的 BOSS 浏览器自动化能力，并通过统一工作流完成：

```text
BOSS 在线简历 → AI 硬性条件判断 → SQLite → 按通过结果打招呼 → SQLite 审计 → 飞书多维表格
```

## 1. 安装与配置

```powershell
python -m pip install -e .
```

敏感信息只通过环境变量提供，不写入 YAML：

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"
$env:FEISHU_APP_ID = "..."
$env:FEISHU_APP_SECRET = "..."
$env:FEISHU_BITABLE_APP_TOKEN = "..."
$env:FEISHU_BITABLE_TABLE_ID = ""  # 留空时按名称查找或创建数据表
```

飞书应用需要在开放平台人工开通多维表格读写权限，并让应用能够访问目标
多维表格。程序可以在已有多维表格应用内创建数据表，但不能替管理员授予权限。

主要配置文件：

- `config/ai.yaml`：模型、API 地址和 Prompt 文件名；
- `config/prompt/`：AI 简历分析和招呼语 Prompt；
- `config/jobs/<岗位>.yaml`：岗位硬性要求、卡片预筛条件、每日限额和操作时间；
- `config/feishu.yaml`：多维表格及字段映射；
- `config/app.yaml`：SQLite 和日志路径。

## 2. 创建并确认任务

为保留人工授权门，端到端命令只接受已经确认的 `run_id`：

```powershell
boss-hr start "Python工程师" --job-name "Python工程师" --encrypt-job-id "<job-id>"
boss-hr confirm --job-name "Python工程师" --encrypt-job-id "<job-id>" --run-id "<run-id>"
```

## 3. 先执行安全演练

```powershell
boss-hr automate --job-name "Python工程师" --encrypt-job-id "<job-id>" `
  --run-id "<run-id>" --count 10 --dry-run
```

dry-run 会抓取和分析简历并写入 SQLite，但不会真实点击招呼，也不会同步飞书。

## 4. 执行正式流程

```powershell
boss-hr automate --job-name "Python工程师" --encrypt-job-id "<job-id>" `
  --run-id "<run-id>" --count 10 --max-greetings 5
```

也可以通过 `--requirements-file requirements/python-engineer.md` 临时覆盖
`config/jobs/<岗位>.yaml` 中的岗位要求。

只有真实打招呼成功的候选人会同步到飞书。SQLite 是事实数据源；重复同步会按
BOSS 候选人 ID 更新记录，不会仅依赖姓名判断。

## 5. 数据与审计

- SQLite：`data/hr_agent.db`
- 系统日志：`logs/system.log`
- 自动化日志：`logs/automation.log`
- 审计日志：`logs/audit.log`

数据库保存候选人基础信息、原始简历、AI 硬性条件判断结果及原因、招呼消息与
时间、飞书同步状态和审计事件。`data/`、`logs/`、数据库 WAL 文件和令牌缓存均已加入
`.gitignore`，不得提交到仓库。
