# boss-hr-agent-toolkit 文件管理规范

## 📁 统一输出结构

所有 skill 产生的文件必须遵循以下结构：

```
data/runs/                                   # 相对项目根目录
└── <encryptJobId>/                            # 按 BOSS 岗位 ID 建子文件夹
    ├── state/                                 # 跨 run 累计，不覆盖
    │   ├── candidate_pool.json
    │   ├── download_state.json
    │   ├── resumes_master.json
    │   └── geek_positions.json
    └── runs/
        └── <run_id>/                          # 一次筛选任务
            ├── <run_id>_<岗位名>_简历筛选报告.html
            └── process/
                ├── job_detail.json
                ├── recommend_geek_ids.json
                ├── new_resumes.json
                ├── screening_results.json
                ├── greet_log.json
                └── failed_resumes.json
```

## 🚨 重要规则

### 1. 禁止在项目根目录散落运行文件
- ❌ 错误：`test_resumes.json`
- ✅ 正确：`data/runs/<encryptJobId>/runs/<run_id>/process/test_resumes.json`

### 2. HTML 报告放岗位文件夹根目录
- ❌ 错误：`data/runs/<encryptJobId>/runs/<run_id>/process/报告.html`
- ✅ 正确：`data/runs/<encryptJobId>/runs/<run_id>/<run_id>_screening_report.html`

### 3. 中间数据放 process/ 子文件夹
- 所有 JSON 数据文件
- 所有临时日志文件
- 所有中间计算结果

### 4. 临时 Python 脚本任务结束后删除
- ❌ 错误：任务完成后 `generate_report.py` 还留在项目根目录
- ✅ 正确：调用 `output.cleanup_temp_scripts()` 清理

### 5. 复用 skill 内已有的 Python 脚本
- ❌ 错误：智能体重新写一个 `generate_report.py`
- ✅ 正确：使用 skill 的 `scripts/` 文件夹中已有的脚本

### 6. 岗位文件夹不存在时自动创建
- 不要询问用户，直接创建
- 使用 `os.makedirs(JOB_DIR, exist_ok=True)`

## 🔧 使用方法

### 在 Python 脚本中

```python
import sys
import os

# 添加 shared 目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
from output_manager import JobOutputManager

# 初始化输出管理器
output = JobOutputManager('车架工程师')

# 获取文件路径
print(output.report_path)           # HTML 报告路径
print(output.jd_path)               # JD 数据路径
print(output.resumes_path)          # 简历数据路径
print(output.recommend_geek_ids_path)  # 候选人 ID 路径

# 保存数据
with open(output.resumes_path, 'w', encoding='utf-8') as f:
    json.dump(resumes, f, ensure_ascii=False, indent=2)

# 任务结束后清理临时脚本
output.cleanup_temp_scripts()
```

### 在 Skill 文档中

所有 skill 的 SKILL.md 必须包含：

```markdown
### 输出文件

本 skill 产生的文件：
- `<run_id>_screening_report.html` → `data/runs/<encryptJobId>/runs/<run_id>/`
- `xxx.json` → `data/runs/<encryptJobId>/runs/<run_id>/process/`

使用 `output_manager.py` 获取文件路径，不要硬编码路径。
```

##  已有 Python 脚本清单

以下脚本位于各 skill 的 `scripts/` 文件夹中，**必须复用，禁止重复造轮子**：

### boss-recommend-downloader/scripts/
- `recommend_list.py` — 获取推荐牛人候选人列表
- `recommend_download.py` — 批量获取完整简历
- `run_all.py` — 一键运行全流程

### boss-hr-greet/scripts/
- `auto_greet.py` — 硬性条件通过（passed=true）候选人自动打招呼（位置表 + 倒序招呼）

### shared/
- `output_manager.py` — 统一文件路径管理（所有 skill 共用）
- `run_orchestrator.py` — 跨 Step run_id 编排（让所有产物落在同一 runs/<run_id>/）
- `job_resume_store.py` — 候选人键 + 三态状态读写 + 简历合并

## 🧹 清理项目运行缓存

先在项目根目录中预览待清理内容：

```bash
python tools/cleanup_cache.py --dry-run
```

确认后去掉 `--dry-run`。该工具使用项目内的 `data/runs`，不依赖桌面目录或固定盘符。

### 清理 toolkit 根目录的临时诊断脚本

排查过程中可能落在 toolkit 根目录的 `_diag_*.py` 临时脚本（不属于任何 skill，不该留）。运行：

```python
import os, glob

toolkit_root = os.path.dirname(os.path.abspath(__file__))
patterns = ['_diag_*.py', '_tmp_*.py']

removed = []
for pattern in patterns:
    for path in glob.glob(os.path.join(toolkit_root, pattern)):
        os.remove(path)
        removed.append(os.path.basename(path))

if removed:
    print('已删除：', removed)
else:
    print('toolkit 根目录无临时诊断脚本')
```

## 📖 示例：完整工作流

> v1.2 起统一走 CLI（`boss-hr automate` 一键完成抓取 → AI 硬性条件分析 → 落库 →
> 飞书同步 → 打招呼），**无 AI 评分**。以下为底层脚本视角的等价流程，供排查/维护参考。

```bash
# 1. Step 1 提取 JD
python -X utf8 boss-job-detail/scripts/boss_jd.py "<encryptJobId>" --run-id "$RUN_ID"

# 2. Step 2 候选人列表 + 卡片预筛 + 下载简历
python -X utf8 boss-recommend-downloader/scripts/recommend_list.py \
  --job-name "车架工程师" --run-id "$RUN_ID"
python -X utf8 boss-recommend-downloader/scripts/recommend_download.py \
  --job-name "车架工程师" --run-id "$RUN_ID" --max 25

# 3. Step 3 AI 硬性条件分析（boss-hr automate 内部执行）
#    在线简历原文 + 岗位 yaml 硬性要求交给 AI 逐条对照，
#    只输出 passed（是否满足全部硬性条件）+ reason（逐条判断依据）
#    → process/screening_results.json

# 4. Step 4 生成报告
python -X utf8 html-report/scripts/generate_html_report.py \
  --input  "runs/$RUN_ID/process/screening_results.json" \
  --output "runs/$RUN_ID/${RUN_ID}_车架工程师_简历筛选报告.html"

# 5. Step 5 打招呼（只招呼 passed=true 的候选人）
python -X utf8 boss-hr-greet/scripts/auto_greet.py \
  --job-name "车架工程师" --run-id "$RUN_ID"

# 中间产物
# - process/job_detail.json           Step 1 boss_jd.py
# - process/recommend_geek_ids.json   Step 2 recommend_list.py
# - process/new_resumes.json          Step 2 recommend_download.py
# - process/card_prescreen_rejected.json  Step 2 卡片预筛未通过审计
# - process/screening_results.json    Step 3 AI 硬性条件判断
# - <run_id>_<岗位>_简历筛选报告.html    Step 4
# - process/greet_log.json            Step 5 auto_greet.py
```

最终交付给用户的只有一份 HTML 报告 + 一份打招呼日志（greet_log.json），其他都是留痕查阅的过程文件。
