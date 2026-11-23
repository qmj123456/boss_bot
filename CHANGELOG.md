# Changelog

## 未发布 — 新增岗位：编导 / 渲染（2026-11-12）

### 新增

- `config/jobs/编导.yaml`：BOSS 岗位「短视频编导（创意内容/团队稳定/3C）」。推荐页筛
  「近14天没看过 + 刚刚活跃 + 大专/本科 + 离职-随时到岗」；卡片筛「≤32岁 + 深圳 +
  期望/最近关注含剪辑/新媒体运营/内容运营/编导/拍剪 + 最近一份岗位含剪辑/运营/编导/拍剪/
  内容/摄影」；简历要求最近3份有一段连续满8个月、最近两份至少一段有有效数据成果
  （量词/产出数量/日期/0-1 不算；金额、消耗、播放量、曝光、观看、涨粉算），
  竞品段可按「近3年 + 岗位名含剪拍/剪辑/拍摄/摄像/编导/内容 + 满8个月」豁免数据；
  竞品段命中 TVC/品牌广告/宣传片、天猫京东主图详情页、纯摄影师、纯广告编导任一即淘汰，
  同段明确出现千川/抖音/短视频/信息流则按例外保留。
  - 竞品名单沿用剪拍/运营那份 52 项（含简称）。
  - 岗位内 `greet_message` 为编导专用话术。
- `config/jobs/渲染.yaml`：BOSS 岗位「电商渲染师（3c类目）」。推荐页筛「近14天没看过 +
  刚刚活跃 + 离职-随时到岗」；卡片筛「≤32岁 + 深圳 + 期望/最近关注含 3D设计师/渲染 +
  最近两份岗位含 3D设计师/渲染」；简历要求任意位置同时出现 `ks`/`keyshot` 与
  `犀牛`/`rhino`，缺一淘汰；不筛学历、不要求数据成果。
- `config/app.yaml`：调度表新增 `编导`（每日 50 个招呼，单轮上限 50）与
  `渲染`（每日 20 个招呼，单轮上限 20），时段均为 09:00–24:00。
- `boss_hr/job_id_resolver.py`：新增 `编导`/`渲染` 的
  `BOSS_JOB_NAME_*`、`BOSS_JOB_ID_*`、`FEISHU_TABLE_*` 绑定
  （后缀 `BIANDAO`、`RENDER`）。
- `config/feishu.yaml`、`config/message.yaml`、`.env`、`.env.example`：
  同步补齐两个岗位的飞书分表、话术模板与环境变量占位。
- `ai/resume_rules.py`：`resume_analysis.recent_outcomes.traffic_keywords` 配置项。
  命中关键词且带数字的流量类成果（播放量/曝光/观看/涨粉）直接计为有效成果，
  不受 `exclude_quantity_only` 的 kind 白名单限制；未配置的岗位行为不变。

### 调整

- `workflow/recruit_workflow.py`：`编导` 与 `运营`/`高级剪拍` 一致，竞品公司命中
  不再直接放行，竞品名单只用于简历阶段豁免数据成果。
- `docs/BOSS招聘自动化程序使用说明书.md`：补充 `recent_outcomes` /
  `traffic_keywords` 字段说明。

### 待办

- BOSS 账号侧岗位 ID 由用户手动填写（`BOSS_JOB_ID_BIANDAO`、
  `BOSS_JOB_ID_RENDER`）；若 BOSS 标题再改名，需同步 `boss_match_names`。

## 未发布 — 浏览器层防风控增强（2026-10-23）

参考 `Viy1204/boss-cli` 的防风控模式与反检测实践，在不改变业务主流程/功能的前提下新增：

### 新增

- `shared/anti_detection.py`：统一安装浏览器层守卫
  - 页面注入：`navigator.webdriver=false`（prototype getter + 原生
    `toString` 形态）、清理 Playwright/Puppeteer/ChromeDriver 全局、
    `console` 大对象参数归一化、`console.clear` 置空
  - 请求拦截：阻断 `risk-detection` / `zhipin-security/web/boss`（Aegis）
    安全脚本；`logapi` / `apm-fe` / `warlock` / `shink` 上报返回 HTTP 204
  - 风控熔断：60 秒内第 4 次跳转 403/verify/security 页，或同一 URL
    15 秒内加载 5 次 → 退出码 30 并提示人工验证；事件写
    `logs/risk_guard_events.jsonl`；验证页不拦截
- `human_interaction.human_type_text`：消息按 38~125ms 随机间隔逐字输入，
  换行用 `Shift+Enter`；鼠标按下时长 55~180ms 随机
- Edge 启动参数增加 `--disable-blink-features=AutomationControlled` 与
  `--lang=zh-CN`；仍保持有窗口模式

### 接入

- `shared/cdp_preflight.connect_cdp`：doctor / start / sync-jobs / 岗位目录
  查询统一生效
- `shared/boss_job_switcher.get_or_create_recommend_page`：recommend_list /
  recommend_download / auto_greet 统一生效
- `boss_jd` / `recommend_list` / `recommend_download` / `auto_greet` 在关键
  检查点调用 `exit_if_risk_page`，命中熔断时以退出码 30 安全停下

### 兼容

- 所有守卫均为 best-effort：页面已关闭 / API 不支持时静默跳过，不影响主流程
- 逐字输入失败自动回退到原 JS 赋值，消息发送功能不退化
- 所有开关可用环境变量关闭（见 README「浏览器层防风控增强」）

## v1.2.1 — 岗位名/ID 自动从 env 解析（2026-09-06）

### 修复

- SQLite 迁移崩溃（`duplicate column name: card_prescreen_reason`）：
  旧实现 additions 先 ADD 新列，renames 阶段又因 Inspector info_cache
  缓存的旧列清单误判新列不存在而执行 RENAME。修复为 rename 先行 +
  PRAGMA 实时查列；半迁移库（新旧列并存）跳过 rename 并把旧列转入
  drops 清理（新增 `tests/test_db_migration.py` 覆盖三种库状态）

### 变更

- confirm / fetch / automate / report / greet / status 的 `--job-name` 不再
  必填：缺省用 `app.default_job_name`（或 env `HR_AGENT_DEFAULT_JOB_NAME`）
- `--encrypt-job-id` 缺省时按岗位从 `.env` 的 `BOSS_JOB_ID_*` 反查；
  `--job-name` 传 BOSS 显示名（如"高级主播（往直播运营发展）"，即
  `BOSS_JOB_NAME_*` 的值）自动映射回本地岗位名（"全职主播"），保证
  `BOSS_JOB_ID_*` 与 `config/jobs/<岗位>.yaml` 都能命中；显示名匹配做了
  归一化（去括号/斜杠/空白），括号打错的岗位名也能对齐
- `boss_hr/commands/_argparse_helpers.require_encrypt_job_id` 统一处理
  job_name 缺省 + 显示名翻译 + eid 反查（6 个命令共用一处生效）；
  `job_id_resolver` 新增 `local_name_for()`
- `--run-id` 仍永远必填（数据边界不变）

## v1.2 — 移除 AI 评分，全面改为 yaml 硬性条件筛选（2026-09-06）

**无评分流程**。筛选只剩两层，判断标准全部来自 `config/jobs/<岗位>.yaml`：

1. **卡片预筛（fetch 阶段，无 AI）**：按 `card_prescreen` 配置（活跃状态 /
   年龄上限 / 最低学历 / 期望城市+岗位关键词）检查推荐卡片，全部满足才下载
   完整简历，未通过写 `card_prescreen_rejected.json` 审计。
2. **AI 硬性条件判断（automate 阶段，语义理解）**：在线简历原文 + 岗位
   yaml 硬性要求交给 AI（`ai/resume_analyzer.py`）逐条对照，只输出
   `passed`（是否满足全部硬性条件）+ `reason`（逐条判断依据）。
   `passed=true` 才进入自动打招呼名单。

### 移除

- `boss-hr score` 命令链路（`commands/score.py` + `application/scoring_service.py`）
- `resume-screener/scripts/` 下三个评分脚本（prepare_scoring_inputs /
  collect_llm_scores / score_resumes）及 cli_runner 白名单映射
- 卡片评分（`card_score`）与简历评分（`score`/`greeting_score`）全部字段：
  SQLite 迁移自动删旧列；飞书表 `ensure_candidate_fields` 尝试删除废弃列
  （卡片得分/打招呼得分/匹配结论等）
- 评分相关测试与文档章节

### 变更

- `CardPrescreenResult` 去掉 score，仅按硬性条件判 passed/missing/rejected
- `screening_results.json` 新格式：candidates 带 `passed`/`reason`（旧评分
  格式仅作历史数据兼容渲染，auto_greet 对旧格式回退 total >= threshold）
- SQLite `candidate` 表新增 `card_prescreen_*`；`candidate_analysis` 表收敛为
  `passed`/`reason`/`decision`
- 飞书字段与 SQLite 一一对应：卡片筛选结果/原因 + 简历筛选结果/原因 + 筛选结论
- HTML 报告生成器支持新格式（通过/未通过徽章 + 判断依据），legacy 评分格式
  仍可渲染历史 run
- `boss-hr automate` 一键完成：抓取 → AI 硬性条件分析 → 落库 → 飞书同步 →
  打招呼；`tools/scheduled_boss_run.py` 定时链路去掉 threshold 传参
- 新增 `boss-hr sync-jobs` 命令（公开命令 8 → 9）：登录后拉 BOSS 实时岗位，
  按 `config/jobs/*.yaml` 精确匹配并更新 `.env` 岗位名/ID；CDP 未运行自动
  拉起 Edge 重试一次；未匹配岗位清空旧值防串岗（warning 不算硬失败）
- `tools/scheduled_boss_run.py` 默认先同步岗位信息（`--skip-sync` 跳过），
  同步结果（含 os.environ 刷新）直接用于本轮 start → confirm → automate；
  同步失败（Edge 未启动/未登录/配置校验错）整轮中止并输出 `stage=sync_jobs`
- `tools/resolve_boss_job_ids.py` 抽出可复用 `run_sync()` 供命令与定时任务共用

### 修复

- Windows 下 `boss_page_lock` 用 `os.kill(pid, 0)` 误发 Ctrl+C 杀掉锁持有
  进程 → 改 `OpenProcess + GetExitCodeProcess` 探活
- `_candidate_from_rejected_item` 只读 `raw` 字段导致卡片预筛拒绝数据丢失 →
  兼容 `card` 字段（recommend_list.py 的真实产物）
- `.env` 中 E 盘失效路径、测试未隔离 `JOBS_REGISTRY_PATH` 等

## v1.1.3 — greet 定位算法收口 + dry-run 严格化（2026-08-05）

修复 v1.1.2 真实交互式 smoke 暴露的 greet not_found bug，并强化
`--dry-run` 的零副作用契约。

### 修复（来自真实 v1.1.2 smoke）

v1.1.2 smoke 真实 run `9a7759badfd95d350nFz3d-_F1NX` / `2026-08-04_164328`
出现 `targeted=4 greeted=3 not_found=1`：评分 rank 1（张庆祝）被标记为
not_found 但另外 3 位在同浏览器会话中真实点击发送成功。根因 + 修复：

1. **身份标识**：原 `scan_and_record_positions` 只按候选人姓名匹配，
   `screening_results.json` 不含 encryptGeekId。修复：
   - `load_high_score_candidates` 从 `recommend_geek_ids.json`
     （优先）/ `new_resumes.json` / `candidate_pool.json` 反查
     `encrypt_geek_id` 并填入每个目标。
   - DOM 卡片扫描 `_CARD_SCAN_JS` / `_FIND_CARD_JS` / `_FIND_BTN_JS`
     按 `data-geek` / `data-geekid`（实测真实 BOSS DOM）→
     `data-geek-id` / `data-uid` / href 查询参数的优先级收集。
2. **虚拟列表懒加载**：原一次性 `scrollHeight → scrollY=0` 扫描无法
   加载 BOSS 全部候选人卡片。修复：渐进式滚动 `scan_all_cards_progressively`
   每屏 sleep + 重扫 + `scrollHeight` 停止增长才算完成。
3. **未找到时的恢复路径**：原算法找到 `pos` 才招呼，找不到直接跳过。
   修复：`greet_one_by_id` 实时按 encrypt_geek_id 找 → 找不到则渐进
   滚动 ≤6 次 + `reached_bottom` 终止 → 仍无则 `status='not_found_after_full_scan'`。
   **不刷新页面**（删除原 `_refresh_page_once` 调用方；
   函数本身已彻底移除以防误用）。
4. **同名误发保护**：若 DOM 找到的卡片 encrypt_geek_id 与目标不一致，
   一律 `not_found_after_full_scan` + reason 含 `geekId mismatch`，
   绝不 click。
5. **partial_success 不被掩盖**：`_calc_summary` 区分顶层 status
   （complete / partial_success / all_not_found / no_candidates），
   `greet_service` 把 partial_success 映射为
   `status='partial_success'` + `next_action='review_warnings'` +
   `data.partial_success_warnings=True`，**不** 自动 finish run。

### 新增 / 强化

- **`boss-hr greet ... --dry-run` 严格化**：dry-run 路径下
  `greet_one_by_id` 找到卡片 + 按钮后立即返回 `status='dry_run'`；
  不调用 click、不调 `human_move`、不调 `mark_done('greet')`、
  不调 `auto_finish()`、不修改 `run.json`、不修改候选人 BOSS 状态。
  新 dry-run 顶层 status：
  - `no_candidates`    目标列表为空
  - `dry_run_complete` 全部目标按 encrypt_geek_id 定位到（仅定位，不 click）
  - `dry_run_review`   部分或全部 not_found_after_full_scan（需人工核对）
- **每条结果必填日志字段**：`target_encrypt_geek_id`、`match_by`
  （encrypt_geek_id / name / no_gid_in_dom / none）、
  `scroll_attempts`、`cards_scanned`、`unique_ids_seen`、
  `reached_bottom`、`reason`（脱敏）。
- **`_refresh_page_once` 已删除**：原"必要时刷新一次"恢复路径移除。
  生产代码默认禁止任何刷新 BOSS 推荐页的路径。

### 测试基线

**363 passed, pytest exit code 0**。

`tests/test_v113_greet_not_found_fix.py`：33 个测试
- `_calc_summary` 完整 + partial_success + all_not_found + no_candidates
- `_calc_summary` dry_run_complete + dry_run_review + no_candidates
- 非 dry-run 路径状态语义保持不变
- `_find_card_by_id` 按 encryptGeekId 锁定 / fallback name / not_found
- `_find_btn_by_card_id` 按 ID + dy 容差拒绝
- `greet_one_by_id` 同名不同 geekId 拒绝点击
- `greet_one_by_id` 完整扫描未找到 → not_found_after_full_scan
- `greet_one_by_id` 找到 → greeted
- `scan_all_cards_progressively` 滚动后加载新卡
- `_build_geek_id_index` 从 recommend_geek_ids + 兜底 new_resumes
- `greet_service` CLI 状态映射（4 路径 + 3 dry-run 路径）
- **端到端 4 目标 / 3 greeted / 1 not_found → partial_success**
- dry-run 零点击 / run.json byte-identical / `_calc_summary` 兼容新旧 status

### 已验证（真实 Windows，2026-08-04）

- ✅ `boss-hr start` 真实路径走通；`screening_results.json` 含 4 位
  ≥70 推荐候选人（张庆祝 82.2 / 樊晓林 76.8 / 赵宇奔 76.0 / 盼盼 75.8）。
- ✅ `boss-hr confirm / fetch / score / report` 完整业务流（10 张简历）。
- ✅ `boss-hr greet` 真实点击：**3 位候选人成功发送招呼**
  （盼盼 / 赵宇奔 / 樊晓林，按 BOSS 倒序处理）；张庆祝 not_found。
- ✅ `run.json.finished=true`、`finished_at='2026-08-04 16:59:53'`、
  `steps_done=['jd','download','score','report','greet']`。
- ✅ 真实 `boss-hr greet ... --dry-run` 验证：
  - 9222 持续 True（同一 Edge 实例）；
  - BOSS recommend 页 URL 不变（**未刷新页面**）；
  - `run.json` md5 在 dry-run 前后完全相同（**零 run 修改**）；
  - 4 目标 → `greeted=0`、`not_found=4`，无任何 click。

### 未验证（明确记录，避免误用）

- **本次 dry-run 中 4 位目标在 BOSS 当前推荐列表均找不到**。
  DOM 当时 122 张可招呼卡片的 `data-geek` 与目标 4 位的
  encryptGeekId（前 14 字符 `78180a558b99990`、`610cef328fe58e3`、
  `cc17c4b3a020e2`、`a71956ba6d9e54`）**无交集**。这是 BOSS 后端推荐
  列表随时间替换的事实，不是 v1.1.3 算法缺陷。
- **v1.1.3 新定位算法未对当前存在目标完成新的真实点击验证**
  （v1.1.2 真实 run 已 finished=true；当前 run 目标已不在 BOSS 推荐页）。
  v1.1.2 smoke 中 `greeted=3` 是用 v1.1.1 旧算法（按 doc_y + name 匹配）
  跑出的，不算 v1.1.3 新算法的真实点击证据。
- 真实 `greet` 端到端（v1.1.3 新算法下 ≥70 候选人真实点击发送）
  需要：新建 run（fetch 新简历）→ 全新目标在 BOSS 当前推荐列表 →
  实际扫码确认 → 跑 greet。如需验证，请在一个新的真实 run 上做。

### 当前限制（GitHub Preview）

- 不支持 `continue` / `batch` / `--batch-size` / 多批累计
- 真实 `greet` 端到端（含 ≥70 候选人真实点击发送）尚未在 v1.1.3
  新算法下完成受控验证
- 仅在 Windows + Git Bash 上完整测试过；macOS / Linux 需自测
- 当前发布形态：**GitHub 源码工具包 + editable install**，**不是**独立 wheel；
  移动源码后必须重新 `pip install -e .`
- 不发布到 PyPI；不在 wheel 中包含完整业务脚本

### 安装

```bash
git clone https://github.com/<owner>/boss-hr-agent-toolkit
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help           # 验证 8 个公开命令（含 doctor）
```

或 Windows：

```bat
install-windows.bat
```

---

## v1.1.2 — 自动启动专用 Edge + waiting_user_login（CLI Preview，2026-08-04）

在 v1.1.0 / v1.1.1 之上，本版引入"无需先跑 doctor"的首次用户体验：
start / fetch / greet 共用 `boss_hr/adapters/browser_environment.ensure_browser_ready`，
9222 未监听时自动启动专用 Edge 并打开 BOSS 招聘者登录页；未登录则优雅返回
`status=waiting_user_login` 而非错误。

### 新增

- `boss_hr/adapters/browser_environment.py`
  - `ensure_browser_ready(auto_launch, login_wait_seconds)`：start/fetch/greet 共用
  - `launch_dedicated_edge()`：专用 Edge（`--user-data-dir=%LOCALAPPDATA%\boss-hr-edge-profile`
    + `--remote-debugging-port=9222`，**不污染**用户日常 Edge profile）
  - `_open_login_page()`：复用 `shared.cdp_preflight.connect_cdp` 的 session.page，
    导航到 BOSS 招聘者登录入口并 `bring_to_front`
- `boss-hr start --no-auto-launch`（调试/测试用；缺 CDP 时直接 `CDP_NOT_RUNNING`）
- `boss-hr start --login-wait-seconds N`（默认 20）
- 新 status：`waiting_user_login`（`ok=true`，`next_action=retry_same_command`，
  **不创建 run**）

### 修复（v1.1.2 fix 真实 smoke 暴露）

- `_open_login_page` 不再静默吞异常：返回 `(ok: bool, reason: str)`，reason 是
  异常类型名 + 短前缀，不含 Cookie / token / URL-secret
- 修复 `patchright` Sync API 在 asyncio loop 中冲突的 bug：
  `_open_login_page` 不再启动第二个 `sync_playwright()` 实例
- `ensure_browser_ready` 超时分支的 `message` 与 `remediation` 严格按
  `login_page_opened` 分支，**绝不伪称已打开登录页**
- `start_service` `waiting_user_login.message` 同步分支：
  - `login_page_opened=true` → "已为你打开专用 Edge，请在浏览器中扫码登录
    BOSS 招聘者后台。完成后回复"好了"，我会继续当前任务。"
  - `login_page_opened=false` → "已为你启动专用 Edge，但未能自动打开 BOSS
    招聘者登录页（<脱敏原因>）。请在已打开的专用 Edge 窗口中手动打开
    https://www.zhipin.com/web/chat/recommend 登录。完成后回复"好了"，
    我会继续当前任务。"

### 变更

- `boss-hr-auto/SKILL.md`：移除"首次必须先跑 doctor"的硬约束；新增
  `waiting_user_login` 智能体处理流程（直接告诉用户扫码登录并立即停止；
  用户回复"好了"后重试同一条 start，不传任何新参数）
- `README.md`：快速开始前置条件放宽（不再要求预先手动启动 Edge）
- `docs/CLI_WORKFLOW.md`：命令表扩为 8 个（含 doctor），每个标注浏览器依赖；
  §2 流程图加入自动启动 / `waiting_user_login` 分支；新增"waiting_user_login
  重试"子节
- `tests/test_v112_auto_browser.py`：25 个测试（v1.1.2 实现 18 个 +
  v1.1.2 fix 7 个回归保护）

### 测试基线

**336 passed, pytest exit code 0**。

### 已验证（真实 Windows 环境，2026-08-04）

- ✅ 9222 未监听 → start 自动启动专用 Edge（专用 profile + `--remote-debugging-port=9222`）
- ✅ `_open_login_page` 真实把专用 Edge 导航到 `https://www.zhipin.com/web/user/?ka=bticket`
  （BOSS 招聘者登录入口），并 `bring_to_front` 聚焦
- ✅ 未登录 → start 返回 `status=waiting_user_login`、`data.login_page_opened=true`、
  `data.login_page_open_error=""`、`run_id=null`（**不创建 run**）
- ✅ message 明确告诉用户扫码登录（与 SKILL.md 强制文案一致）
- ✅ 重试同一条命令（不传任何新参数）→ 行为幂等（再次 `waiting_user_login`，
  复用既有 Edge，再次成功导航到 zhipin.com）
- ✅ 不读取 `~/Desktop/boss-hr-output/jobs.json`（mtime 未变）
- ✅ confirm / score / report / status / greet 未触发
- ✅ `tests/test_v112_auto_browser.py` 25 个测试（含 7 个回归保护）全部通过
- ✅ 完整 pytest 336 passed

### 未验证（需真人 BOSS 招聘者扫码）

- ⏸ `status=waiting_user_confirmation` 的真实路径：本会话环境无 BOSS 招聘者
  账号扫码能力，`zp_at` / `wt2` / `bst` 三个关键 Cookie 全部缺失。第二次 start
  无法走通"实时解析岗位 → 创建 run → waiting_user_confirmation"完整路径
- ⏸ `boss-hr fetch --count N` 真实拉取 + 下载简历的端到端流程
- ⏸ `boss-hr greet` 真实点击打招呼（v1.1.0 起就标注"未受控验证"，本版不引入
  新风险）

如需在生产环境验证上述未验证路径，请在已扫码登录 BOSS 招聘者后台的
Windows + 9222 已就绪的环境执行：

```bash
boss-hr start "<encryptJobId|jobId|岗位名>"
# 第二次执行同一条命令后应返回 waiting_user_confirmation + 新 run_id
boss-hr status --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
# 应返回 confirmed=false，process/job_detail.json 已存在
```

### 当前限制（GitHub Preview）

- 不支持 `continue` / `batch` / `--batch-size` / 多批累计
- 真实 `greet` 成功路径（≥70 分候选人浏览器实际点击发送）**尚未完成受控验证**
- 仅在 Windows + Git Bash 上完整测试过；macOS / Linux 需自测
- 当前发布形态：**GitHub 源码工具包 + editable install**，**不是**独立 wheel；
  移动源码后必须重新 `pip install -e .`
- 不发布到 PyPI；不在 wheel 中包含完整业务脚本

### 安装

```bash
git clone https://github.com/<owner>/boss-hr-agent-toolkit
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help           # 验证 8 个公开命令（含 doctor）
```

或 Windows：

```bat
install-windows.bat
```

---

## v1.1.0 — 统一 CLI Preview（2026-08-04）

GitHub 首版发布状态：**v1.1.0 / CLI Preview**。

### 新增

- **统一 CLI `boss-hr`**（子包 `boss_hr/`），7 个公开命令：
  - `boss-hr start` — 创建新 run，停在人工确认门
  - `boss-hr confirm` — `confirmed` 翻 true；不入 `steps_done`
  - `boss-hr fetch --count N` — 拉候选人列表 + 下载 N 份简历
  - `boss-hr score` — 评分协调（一次返回 1 位候选人）
  - `boss-hr report` — 生成 HTML 报告
  - `boss-hr greet` — ≥70 分候选人打招呼（需用户明确批准）
  - `boss-hr status` — 读 `runs/<run_id>/run.json` + process 目录
- 统一 JSON 输出 schema；退出码语义保留旧脚本契约
- 人工确认门：start 后必须停下，等用户回复"继续"
- 单候选人评分循环：`score` 命令必须 LLM 多次调用，每次一位
- `boss_hr/__main__.py`：让 `python -m boss_hr` 工作
- `pyproject.toml`：唯一 console_script `boss-hr = "boss_hr.cli:main"`
- Windows 脚本：`install-windows.bat` / `uninstall-windows.bat`
- 文档：
  - `docs/CLI_WORKFLOW.md`（GitHub 用户/开发者向）
  - `docs/refactor/unified-cli/HANDOFF.md`（已迁移完成）
  - `docs/refactor/unified-cli/real-smoke-2026-08-04.md`（真实 smoke 脱敏记录）
  - `docs/debug/boss_jd-cache-audit-2026-08-04.md`（JD formValues 调查）

### 修复（auto_greet.py 旧业务缺陷）

- `edc6959 fix(greet): prevent deletion of existing run data`
  - 之前 atexit 调 `prune_if_empty()` 会 `rmtree` 整个 run 目录
  - 现在改为 `note_skip_if_unsaved()` 只写日志 + sentry，永不删文件
  - 23 例回归测试覆盖（11 prune + 12 maybe_finish）
- `ad243ad fix(greet): finish the explicit run after successful greeting`
  - 之前 `orch.finish()` 缺 `run_id` 实参 + `except Exception` 吞错
  - 现在用 `maybe_finish()` 显式传 `run_id`，异常不静默

### 变更

- `boss-hr-auto/SKILL.md` 重写为唯一面向智能体的工作流入口
  （350 行 → 187 行），不再指导调用旧业务脚本
- 其他子 SKILL.md 标记为"业务实现参考"，不再暗示是工作流入口
- `README.md` 头部增加"统一 CLI + editable install"产品定位

### 安装

```bash
git clone https://github.com/<owner>/boss-hr-agent-toolkit
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help           # 验证 7 个公开命令
```

或 Windows：

```bat
install-windows.bat
```

### 测试基线

发布时：**272 passed, pytest exit code 0**。

包括：
- 业务测试（`tests/test_*`）：CLI 子层、run_id 边界、cli_runner、school_tier、job_registry
- 统一 CLI 测试（`tests/cli/test_*.py`）：每条命令都有专项覆盖
- 安装入口测试（`tests/test_install_entry.py`）：12 例

### 当前限制（GitHub 首版）

**必须如实记录**：

- 不支持 `continue` / `batch` / `--batch-size` / 多批累计
- 真实 `greet` 成功路径（≥70 分候选人浏览器实际点击发送）**尚未完成受控验证**
- 仅在 Windows + Git Bash 上完整测试过；macOS / Linux 需自测
- 当前发布形态：**GitHub 源码工具包 + editable install**，**不是**独立 wheel；
  移动源码后必须重新 `pip install -e .`
- 不发布到 PyPI；不在 wheel 中包含完整业务脚本

### 已知不修的旧脚本缺陷（按"无行为变化"约束保留）

- `auto_greet.py` 函数体引用只在 `__main__` 定义的全局 `args` — 新 CLI 走子进程规避

### 升级提示

从 v1.1-skill-stable（旧版直接调 9 个脚本）升级：

1. 旧脚本保留作为内部实现（cli_runner 子进程调用）
2. 所有调用改用 `boss-hr` 命令
3. 不再直接 `python boss_jd.py` / `python auto_greet.py`
4. 不再创建 `spec_*.json` 模板