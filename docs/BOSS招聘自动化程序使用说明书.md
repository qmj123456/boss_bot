# BOSS 招聘自动化程序使用说明书

适用环境：Windows 项目目录 `E:\boss_bot\boss_bot`

本说明书面向日常操作人员，覆盖首次准备、一键运行、计划任务的创建与管理、岗位时间和数量调整、岗位切换、筛选要求修改、结果查看以及紧急停止。程序会控制专用 Edge 浏览器访问 BOSS 招聘端；真实打招呼会立即联系候选人，因此修改配置后建议先用演练模式验证。

## 1 使用前检查

1. 保留完整项目目录，不要单独移动批处理文件。程序当前采用可编辑安装，移动源码目录后需要重新运行 `install-windows.bat`。
2. 电脑需要安装 Python 3.10 或更高版本以及 Microsoft Edge。
3. 项目根目录需要有 `.env` 文件，并填写 AI、飞书和各岗位的 BOSS 岗位 ID。不要把 `.env` 发给他人或提交到代码仓库。
4. BOSS 登录只使用程序自动打开的专用 Edge。不要同时在日常 Edge 中登录同一个 BOSS 账号，避免登录状态互相挤掉。
5. 首次安装时双击 `install-windows.bat`。看到 `boss-hr --help` 正常输出后，安装完成。

项目根目录是：

```text
E:\boss_bot\boss_bot
```

以下命令如无特别说明，都在该目录打开 PowerShell 或命令提示符后执行。

## 2 最常用的一键运行

### 2.1 双击运行一次

双击项目根目录中的 `boss_auto_run_desktop.bat`。

程序会按 `config\app.yaml` 中的岗位顺序运行一次，自动执行岗位同步、创建任务、确认、抓取简历、筛选、同步飞书和打招呼。窗口会保留最终结果和退出码。

首次运行或登录失效时，程序会打开专用 Edge。请在该窗口登录 BOSS 招聘端，然后重新双击批处理文件。

运行结果可在以下位置查看：

```text
data\scheduler\latest_run.json
logs\scheduled\
data\runs\<岗位加密ID>\runs\<本次run_id>\
```

### 2.2 双击运行并保存屏幕日志

双击 `run_with_log.bat`。该入口会在屏幕显示运行信息，并把输出保存到 `logs\scheduled_时间戳.log`。

### 2.3 常驻运行到当天目标完成

双击 `run_daemon.bat`。程序会按各岗位的工作时段循环处理，直到所有启用岗位达到当天打招呼目标、到达最晚结束时间、达到平台总上限，或遇到需要停止的异常。

常驻模式日志保存在：

```text
logs\daemon_时间戳.log
```

### 2.4 先演练再真实运行

演练不会真实打招呼，也不会同步飞书，适合修改配置后检查：

```powershell
python -X utf8 tools\scheduled_boss_run.py --allow-auto-confirm --dry-run
```

确认演练结果无误后，再使用桌面启动器或计划任务真实运行。

## 3 创建定时任务

### 3.1 按岗位时段自动创建

推荐使用以下命令。程序会读取 `config\app.yaml` 中所有岗位的 `time_window`，按不同的开始时间创建多个 Windows 计划任务：

```powershell
python tools\install_windows_scheduled_task.py --auto-windows --allow-auto-confirm
```

任务名称按开始时间排序生成：最早时段为 `BossHrDaemon`，后续时段依次为 `BossHrDaemon_2`、`BossHrDaemon_3` 等。当前配置有几个不同的开始时间，就可能生成几个任务。

按本说明书编写时的 `config\app.yaml`，三个不同的开始时间为 07:00、09:00 和 20:00，因此对应 `BossHrDaemon`、`BossHrDaemon_2` 和 `BossHrDaemon_3`。安装程序会读取所有写有 `time_window` 的岗位，即使该岗位当前数量为 0，也可能据此创建时间触发任务；实际运行时数量为 0 的岗位仍会被跳过。

注意：修改 `time_window` 后，必须重新执行上面的安装命令，Windows 计划任务的触发时间才会更新。如果时段数量减少，还要手动删除不再使用的旧任务，例如多余的 `BossHrDaemon_3`。

### 3.2 创建单一时间的任务

每天 09:00 触发一个名为 `BossHrScheduledRun` 的任务：

```powershell
python tools\install_windows_scheduled_task.py --time 09:00 --name BossHrScheduledRun --allow-auto-confirm --daemon
```

可把 `09:00` 和任务名换成需要的值。任务使用当前 Windows 登录用户运行；电脑关机或用户未处于可交互登录状态时，任务不会按预期操作浏览器。

## 4 查看 运行 停止 禁用和删除定时任务

### 4.1 查看所有 BOSS 相关任务

```powershell
schtasks /Query /FO LIST /V | findstr /I "BossHr"
```

也可以打开 Windows 的任务计划程序，在“任务计划程序库”中查找 `BossHrDaemon`、`BossHrDaemon_2`、`BossHrDaemon_3` 或 `BossHrScheduledRun`。

### 4.2 查看某一个任务

```powershell
schtasks /Query /TN BossHrDaemon /V /FO LIST
```

把 `BossHrDaemon` 换成实际任务名。

### 4.3 立即手动触发

```powershell
schtasks /Run /TN BossHrDaemon
```

### 4.4 停止正在运行的计划任务

```powershell
schtasks /End /TN BossHrDaemon
```

如果同时存在多个正在运行的 BOSS 任务，应分别执行 `/End`。停止后再用 `/Query` 检查状态。

### 4.5 暂停以后自动运行

禁用任务不会删除配置，适合临时停用：

```powershell
schtasks /Change /TN BossHrDaemon /Disable
```

恢复自动运行：

```powershell
schtasks /Change /TN BossHrDaemon /Enable
```

注意：禁用只阻止下一次触发，不一定终止已经运行的进程。正在运行时还要执行 `/End`。

### 4.6 彻底删除任务

```powershell
schtasks /Delete /TN BossHrDaemon /F
```

对 `BossHrDaemon_2`、`BossHrDaemon_3` 等任务逐个执行。删除计划任务不会删除程序、简历、日志或历史结果。

### 4.7 取消当天剩余运行的推荐顺序

先停止正在运行的任务，再禁用它：

```powershell
schtasks /End /TN BossHrDaemon
schtasks /Change /TN BossHrDaemon /Disable
```

需要恢复时执行 `/Enable`。如确定以后都不再使用，再执行 `/Delete`。

## 5 终止正在运行的程序

### 5.1 从批处理窗口启动

切回正在运行的黑色命令窗口，按 `Ctrl+C`。出现“终止批处理操作”提示时输入 `Y` 并回车。等待几秒后确认窗口已经停止继续输出。

### 5.2 从 Windows 计划任务启动

先执行：

```powershell
schtasks /End /TN BossHrDaemon
```

如果任务状态已停止但 Python 仍在运行，先定位本程序的进程，不要直接结束电脑上的全部 Python：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*tools\scheduled_boss_run.py*' } |
  Select-Object ProcessId, Name, CommandLine
```

核对命令行路径确实属于本项目后，按进程号终止：

```powershell
Stop-Process -Id <进程号> -Force
```

不要使用 `taskkill /IM python.exe /F`，它会把电脑上其他 Python 程序一起关闭。

### 5.3 紧急停止后的检查

1. 检查专用 Edge 是否还在继续翻页或点击。
2. 查看 `data\scheduler\latest_run.json` 和最新日志，确认停止发生在哪一步。
3. 如需当天不再自动启动，同时禁用所有 BOSS 计划任务。
4. 不要手动删除正在写入的 `data\runs` 目录。程序下次运行会创建新的 `run_id`。

## 6 修改每个岗位的时间和数量

主要文件是：

```text
config\app.yaml
```

每个岗位位于 `app.scheduler.jobs` 列表中，例如：

```yaml
- name: 亚马逊运营
  count: 30
  max_greetings: 10
  daily_greet_target: 10
  time_window: ["09:00", "24:00"]
```

各字段含义如下。

- `name`：本地岗位名，必须与 `config\jobs\岗位名.yaml`、`.env` 岗位映射一致。
- `count`：每轮最多抓取和评估的候选人数。它是单轮数量，不是严格的每日总量。
- `max_greetings`：每轮最多真实打招呼人数。
- `daily_greet_target`：常驻模式下该岗位当天累计打招呼目标。达到后当天不再跑该岗位。
- `time_window`：该岗位允许常驻任务运行的时间段，格式为 `["开始", "结束"]`。`24:00` 表示当天结束。

### 6.1 修改每天招呼人数

例如希望亚马逊运营每天招呼 20 人，每轮最多 5 人：

```yaml
- name: 亚马逊运营
  count: 30
  max_greetings: 5
  daily_greet_target: 20
  time_window: ["09:00", "18:00"]
```

常驻模式会重复运行，直到当天累计达到 20 人或到达结束时间。若 `daily_greet_target` 未配置或为 0，程序会把 `max_greetings` 当作当天目标。

### 6.2 暂停某个岗位

把该岗位的 `count` 或 `max_greetings` 设为 0：

```yaml
- name: 亚马逊运营
  count: 0
  max_greetings: 0
  daily_greet_target: 0
  time_window: ["09:00", "18:00"]
```

程序会跳过 `count <= 0` 或 `max_greetings <= 0` 的岗位。仅删除 `time_window` 不等于停用岗位。

### 6.3 修改岗位运行时间

直接修改 `time_window`。例如改成每天 13:30 到 19:00：

```yaml
time_window: ["13:30", "19:00"]
```

保存后重新注册计划任务：

```powershell
python tools\install_windows_scheduled_task.py --auto-windows --allow-auto-confirm
```

然后查看任务，确认下一次运行时间正确，并删除因时段减少而残留的旧任务。

### 6.4 需要同时核对的岗位安全上限

每个 `config\jobs\岗位名.yaml` 里还有以下保护项：

- `daily_send_limit`：该岗位每天真实发送上限。0 表示不限制。
- `daily_candidate_limit`：该岗位每天抓取和评估上限。0 表示不限制。
- `work_time.enabled`、`work_time.start`、`work_time.end`：岗位级工作时间保护。启用后，非工作时间会阻止真实打招呼。

如果 `config\app.yaml` 的目标高于岗位 YAML 的保护上限，最终会受更严格的限制。常驻程序另有所有岗位合计每天 200 次招呼的代码级上限。

### 6.5 YAML 修改注意事项

1. 使用空格缩进，不要使用 Tab。
2. `jobs` 下每个岗位前保留 `- name:`。
3. 时间加引号，避免 YAML 把它识别成其他类型。
4. 修改前复制备份，保存后先用 `--dry-run` 演练。

## 7 切换岗位

### 7.1 临时手动运行一个已配置岗位

不需要修改默认岗位，直接指定岗位名：

```powershell
python -X utf8 tools\scheduled_boss_run.py --job-name "亚马逊运营" --allow-auto-confirm --dry-run
```

确认无误后去掉 `--dry-run` 才会执行真实操作。

### 7.2 更改命令行默认岗位

编辑 `config\app.yaml`：

```yaml
app:
  default_job_name: 亚马逊运营
```

该设置影响未显式传 `--job-name` 的单岗位命令。多岗位定时运行仍以 `app.scheduler.jobs` 为准。

### 7.3 BOSS 岗位改名或重新发布

先确保专用 Edge 已登录，然后执行：

```powershell
boss-hr sync-jobs
```

该命令会读取 BOSS 当前岗位并更新 `.env` 中已有岗位的 `BOSS_JOB_NAME_*` 和 `BOSS_JOB_ID_*`。同步完成后检查对应的 `config\jobs\岗位名.yaml`：

- `boss_display_name` 应引用正确的 `BOSS_JOB_NAME_*` 变量。
- `boss_match_names` 可填写 BOSS 页面上可能出现的岗位标题。

如果同步提示找不到岗位，先检查本地岗位名、BOSS 显示名和 `boss_match_names` 是否对应。

### 7.4 新增一个程序原来没有的岗位

新增岗位需要同时维护以下位置，不能只复制一个 YAML 文件：

1. 在 `config\jobs` 新建 `新岗位.yaml`，并把 `recruitment.job_name` 改成完全相同的岗位名。
2. 在 `config\app.yaml` 的 `app.scheduler.jobs` 中增加岗位、数量和时段。
3. 在 `.env` 与 `.env.example` 增加该岗位的 `BOSS_JOB_NAME_*`、`BOSS_JOB_ID_*` 和需要时的 `FEISHU_TABLE_*`。
4. 在 `boss_hr\job_id_resolver.py` 的 `JOB_BINDINGS` 中增加岗位与环境变量的映射。
5. 在 `config\feishu.yaml` 的 `job_tables` 中增加岗位表映射。
6. 如使用公共话术模板，在 `config\message.yaml` 的 `message.jobs` 中增加岗位；岗位 YAML 内的 `greet_message` 优先级更高。
7. 运行 `boss-hr sync-jobs`，再用演练模式验证岗位切换和筛选结果。

岗位名称在这些文件中必须完全一致，包括大小写、中文括号和空格。

## 8 修改每个岗位的筛选要求

每个岗位的要求都在：

```text
config\jobs\<岗位名>.yaml
```

例如：

```text
config\jobs\全职主播.yaml
config\jobs\兼职主播.yaml
config\jobs\运营.yaml
config\jobs\亚马逊运营.yaml
config\jobs\tiktok运营.yaml
config\jobs\初级剪拍.yaml
config\jobs\运营助理.yaml
config\jobs\高级剪拍.yaml
config\jobs\编导.yaml
config\jobs\渲染.yaml
```

常用字段如下。

- `recruitment.job_name`：本地岗位名。
- `greet_message`：候选人通过后发送的岗位介绍。它优先于 `config\message.yaml`。
- `experience_required`：工作经历硬性要求。
- `degree_required`：学历要求。
- `skill_requirements`：需要逐条满足的核心条件。
- `additional_requirements`：更详细的通过、排除、去重和特殊口径。
- `resume_analysis.employment_status_whitelist`：完整简历允许的求职状态。
- `card_prescreen`：打开完整简历之前，根据推荐卡片判断的条件。
- `boss_filter`：切换岗位后自动设置的 BOSS 推荐页筛选条件。
- `daily_send_limit`、`daily_candidate_limit`、`work_time`：岗位级安全上限。
- `risk_control`：随机等待和验证码、登录失效、页面异常时的停止策略。

### 8.1 卡片预筛常见字段

- `enabled`：是否启用卡片预筛。
- `active_status_keywords`：允许的活跃状态。
- `max_age`：年龄上限；`null` 表示不限制。
- `min_degree`：最低学历。
- `city_keyword`：期望城市。
- `role_keyword` 或 `role_keywords`：期望岗位关键词。
- `salary_max`：期望薪资上限。
- 其他岗位专用字段：最近工作时间、经验月数、竞品公司、岗位关键词等。

### 8.1.1 完整简历数据成果口径（`resume_analysis.recent_outcomes`）

- `limit` / `min_experiences` / `min_metrics` / `mode`：最近 N 份工作中要几份、每份几项成果、全部满足还是任意一份满足。
- `exclude_quantity_only`：纯数量不算成果（量词、产出数量、日期、0-1 一律不计）。
- `exclude_money`：金额、消耗不算成果（仅个别岗位开启）。
- `traffic_keywords`：本岗位把哪些流量类指标也算作有效成果，例如
  `["播放", "曝光", "观看", "涨粉"]`。命中关键词且带数字即计为有效成果，
  与 `exclude_quantity_only` 不冲突；未配置该项的岗位仍只认金额、消耗、
  转化率、ROI、排名等经营指标。目前只有「编导」开启。

修改筛选规则时，应把同一硬性条件同步写入 `experience_required`、`skill_requirements` 或 `additional_requirements` 中合适的位置，避免卡片预筛和完整简历判断互相矛盾。

### 8.2 修改打招呼话术

优先修改对应岗位 YAML 中的：

```yaml
greet_message: |
  第一行话术
  第二行话术
```

只有岗位 YAML 没有 `greet_message` 时，程序才会使用 `config\message.yaml` 的岗位模板或默认模板。

## 9 查看运行结果和排查失败

### 9.1 查看最近一次定时运行摘要

```text
data\scheduler\latest_run.json
```

重点查看：`ok`、`exit_code`、`stage`、`summary`、`log_file` 和各岗位的 `run_id`。

### 9.2 查看详细日志

- 单轮定时日志：`logs\scheduled\时间戳.log`
- 常驻模式窗口日志：`logs\daemon_时间戳.log`
- `run_with_log.bat` 日志：`logs\scheduled_时间戳.log`

### 9.3 查看某次岗位结果

```text
data\runs\<岗位加密ID>\runs\<run_id>\
```

常见文件包括：

- `process\job_detail.json`：岗位详情。
- `process\card_prescreen_rejected.json`：卡片预筛未通过记录。
- `process\new_resumes.json`：本次获取的简历。
- `process\screening_results.json`：完整简历判断结果和原因。
- `process\greet_log.json`：打招呼结果。
- `<run_id>_screening_report.html`：可视化报告。

### 9.4 常见问题

专用 Edge 打开但程序不继续：在专用 Edge 登录 BOSS 招聘端，登录完成后重新执行原命令。

提示找不到岗位：运行 `boss-hr sync-jobs`，检查 `.env` 对应的 `BOSS_JOB_ID_*`，并确认岗位名与配置一致。

某岗位一直跳过：检查 `count` 和 `max_greetings` 是否为 0、当前时间是否在 `time_window` 内、当天是否已达到 `daily_greet_target`。

计划任务时间没变化：修改 `config\app.yaml` 后还需要重新执行 `install_windows_scheduled_task.py --auto-windows --allow-auto-confirm`。

没有真实打招呼：检查是否使用了 `--dry-run`、岗位 `max_greetings` 是否为 0、岗位级 `daily_send_limit` 和 `work_time` 是否限制、候选人是否通过全部硬性条件。

遇到验证码、登录失效或页面异常：立即停止程序，在专用 Edge 人工处理后再运行。不要通过缩短等待时间或提高频率绕过平台风控。

## 10 日常操作检查清单

开始前：

- 专用 Edge 已登录正确的 BOSS 账号。
- `config\app.yaml` 中岗位、数量和时段正确。
- 对应岗位 YAML 的要求和话术已复核。
- 新配置已经先用 `--dry-run` 演练。

运行中：

- 关注窗口或日志是否出现验证码、登录失效、找不到岗位。
- 不要同时启动多个相同任务。计划任务已设置为忽略重复实例，但手工启动仍应避免重复操作。
- 需要停止时先用 `Ctrl+C` 或 `schtasks /End`，不要直接关机。

运行后：

- 查看 `data\scheduler\latest_run.json`。
- 抽查 `screening_results.json` 和 `greet_log.json`。
- 确认飞书数据和 BOSS 实际联系人数一致。
- 如需暂停次日运行，及时禁用计划任务。
