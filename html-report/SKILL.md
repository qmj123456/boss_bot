---
name: html-report
description: |
  生成美观的简历筛选 HTML 报告。接受 boss-hr automate 输出的
  screening_results.json（硬性条件格式：passed/reason），渲染候选人
  通过/未通过结果、判断依据、基础信息。

  **本 Skill 不是工作流入口**。通用智能体应只通过
  [boss-hr-auto](../boss-hr-auto/SKILL.md) → 统一 CLI `boss-hr report` 调用。
  本文档保留作为：HTML 报告渲染实现参考 / 模板说明 / 内部脚本接口文档。

  **唯一方案**：通用 schema 渲染。v1.2 起主格式为硬性条件筛选结果（无评分），
  历史评分格式（total/dimensions/tier）仅作兼容渲染。
---

# HTML 报告生成（统一方案）

## 设计核心

- **通用 schema**：接受 `screening_results.json`，**任意岗位都能用**
- **双格式支持**：v1.2 起主格式为硬性条件筛选（candidates 带 `passed`/`reason`，无分数）；
  历史 run 的旧评分格式（`total`/`dimensions`/`tier`）自动走 legacy 渲染路径
- **字段鲁棒**：所有字段 `.get()` 处理，缺失自动跳过
- **视觉风格**：黑蓝渐变 header + 玻璃质感 meta-item + 三色 stat-card 配色

---

## 核心不变量（不要修改）

| 项 | 值 |
|----|----|
| 输入文件 | `screening_results.json`（boss-hr automate 产出） |
| 输出文件 | `*.html`（自包含单文件，无外部依赖） |
| 字体栈 | Inter, "PingFang SC", "Microsoft YaHei" |
| 主色（header） | `linear-gradient(135deg, #1a1a2e, #16213e, #0f3460)` |
| 背景色 | `#f5f7fa` |
| 卡片圆角 | `border-radius: 12px` |
| 结果徽章（主格式） | 通过 #059669（绿）/ 未通过 #dc2626（红） |
| stat-card 配色 | 蓝（总数）/ 绿（通过）/ 红（未通过）/ 灰（失败） |

---

## 工具脚本

### `scripts/generate_html_report.py`（**唯一**渲染脚本）

**工具函数**（agent 可直接 import）：

| 函数 | 作用 |
|------|------|
| `render(data: dict) → str` | 输入 screening_results.json 数据，输出 HTML 字符串（自动识别格式） |
| `_is_hard_condition_format(data) → bool` | 检测是否为 v1.2 硬性条件格式 |
| `render_hard(data) → str` | 硬性条件格式渲染（主路径） |
| `render_hard_candidate(c, rank) → str` | 渲染单个候选人卡片（通过/未通过徽章 + 判断依据） |
| `bar_color(pct) → str` | legacy：根据百分比返回进度条颜色（绿/黄/红） |
| `tier_badge(tier) → str` | legacy：根据 tier 返回徽章 HTML |

**CLI**：

```bash
python generate_html_report.py \
  --input <screening_results.json> \
  --output <report.html> \
  --job-name "<岗位名>" \
  --encrypt-job-id "<BOSS 的 encryptJobId>" \
  --run-id <run_id>
```

> 🚨 **新接口必传 `--encrypt-job-id`**：与上游脚本保持一致。也可以设 env `BOSS_HR_ENCRYPT_JOB_ID` 作为 fallback。
> 兼容模式：如果不传，脚本会从 `--input` 路径里反推 `job_name`（旧中文目录）作为兜底，但**新设计必须显式传**，避免与新 job 混目录。

### `templates/report.html`（HTML 模板）

Jinja2 风格模板占位符。**当前未使用**（脚本直接生成自包含 HTML），保留作为参考。

---

## 完整工作流

```
boss-hr fetch / automate（boss-recommend-downloader）
    拉推荐列表 + 卡片预筛 + 下载在线简历
    → 输出 runs/<run_id>/process/{recommend_geek_ids,new_resumes,failed_resumes,run_summary}.json
    ↓
boss-hr automate（AI 硬性条件判断）
    在线简历原文 + 岗位 yaml 硬性要求 交给 AI 逐条对照
    → 只输出 passed（是否满足全部硬性条件）+ reason（逐条判断依据）
    → 输出 runs/<run_id>/process/screening_results.json
    ↓
html-report/scripts/generate_html_report.py
    读取 screening_results.json（自动识别格式）
    → 输出 runs/<run_id>/<run_id>_<岗位名>_简历筛选报告.html（自包含）
        ↑ 文件名含 run_id，永不覆盖历史报告
        ↑ 头部展示 run-badge：🆔 run_id: <run_id> | 🕐 生成时间: ...
    ↓
preview_url 在 IDE 内置浏览器打开
```

> **v1.2 起没有 AI 评分**：不再有 score_resumes.py / 维度打分 / tier 分档。
> screening_results.json 由 `boss-hr automate` 直接产出。

## 工作区路径约定（2026-07-29+）

**所有数据统一存放在 `data/runs/<encryptJobId>/` 下**（或 `.env` 配置的
`BOSS_HR_OUTPUT_DIR`），**目录名直接用 BOSS 的 `encryptJobId`**（不再用中文岗位名）。
`job_name` 仅作为 `jobs.json` 里的可读元数据。

```
<output_dir>/
├── jobs.json                               # JobRegistry：encryptJobId → {name, company}
└── <encryptJobId>/
    ├── state/                              # 跨 run 保留
    └── runs/<run_id>/                      # 一次筛选任务
        ├── <run_id>_screening_report.html  ← 本 skill 输出
        └── process/
            ├── screening_results.json      ← 本 skill 读取
            └── ...
```

> **路径选择集中在 `shared/output_manager.JobOutputManager`**——CLI 脚本只接收并透传 `--encrypt-job-id`，不参与路径拼接。

| Step | Skill | 脚本/命令 | 输出文件 |
|:----:|-------|------|----------|
| 1 | `boss-job-detail` | `boss_jd.py` | `runs/<run_id>/process/job_detail.json` |
| 2B | `boss-recommend-downloader` | `run_all.py` | `runs/<run_id>/process/{recommend_geek_ids,new_resumes,failed_resumes,run_summary}.json` |
| 3 | `boss-hr-auto` | `boss-hr automate`（AI 语义理解） | `runs/<run_id>/process/screening_results.json` |
| 4 | `html-report` | `generate_html_report.py` | `runs/<run_id>/<run_id>_<岗位名>_简历筛选报告.html` |

> 上游脚本（Step 1 / 2）已自动写入工作区，下游脚本（Step 3 / 4）通过 `--input/--output` 读取工作区。
>
> `JobOutputManager` 提供的标准路径属性：`jd_path` / `recommend_geek_ids_path` / `new_resumes_path` / `screening_results_path` / `report_path` / `run_summary_path`。
>
> **同一 run 必须传同一个 `--run-id`** 给所有脚本，产物才落在同一个 `runs/<run_id>/`。

---

## 输入 schema（screening_results.json）

### 主格式：硬性条件筛选（v1.2+，boss-hr automate 产出）

```json
{
  "job": "主播",
  "summary": {"total": 20, "passed": 5, "rejected": 14, "failed": 1},
  "candidates": [
    {
      "rank": 1,
      "name": "陈瀚",
      "passed": true,
      "decision": "recommend",
      "reason": "年龄 24 岁 ≤30；学历大专 ≥高中；期望城市深圳；期望岗位主播……全部硬性条件满足",
      "age": 24,
      "degree": "大专",
      "city": "深圳",
      "expected_salary": "8-12K",
      "work_years": "3年",
      "current_role": "带货主播"
    }
  ]
}
```

**格式检测规则**：candidates[0] 有 `passed` 且无 `total`/`dimensions` → 主格式。

**可选字段**（缺失自动跳过对应渲染块）：

| 字段 | 渲染位置 |
|------|----------|
| `candidates[].age` / `degree` / `city` | 候选人卡片 meta 行 |
| `candidates[].work_years` / `current_role` / `expected_salary` | 候选人卡片 meta 行 |
| `candidates[].reason` | 通过（绿）/ 未通过（红）判断依据块 |
| `candidates[].decision` | 辅助信息（recommend/reject） |

### 兼容格式：旧评分（历史 run 数据）

历史 run 的 `screening_results.json` 带 `total`/`dimensions`/`tier`，
脚本自动走 legacy 渲染路径（5 维度进度条 + tier 徽章 + 行动建议三段式），
**仅供查看历史报告**，新 run 不会再产出该格式。

---

## 视觉规范（主格式：硬性条件筛选）

### 排版结构（4 段式）

1. **Header** — 渐变色（`#1a1a2e → #16213e → #0f3460`），标题 + subtitle + meta-grid（岗位名称/薪资/地点/候选人总数）
2. **筛选总览** — stat-card（蓝/绿/红/灰），分别显示总数/通过/未通过/失败
3. **候选人详情** — 每候选人一张卡片：基础信息 meta 行 + 通过/未通过徽章 + 判断依据（通过绿色块 / 未通过红色块）
4. **未通过区** — rejected 卡片弱化展示（灰色边框 + 缩略信息 + 未通过原因）

### 配色规则

| 元素 | 颜色 | 触发条件 |
|------|------|----------|
| 结果徽章 - 通过 | `#059669` 绿底 | `passed: true` |
| 结果徽章 - 未通过 | `#dc2626` 红底 | `passed: false` |
| 判断依据块 - 通过 | 绿色左边框 | passed 且有 reason |
| 判断依据块 - 未通过 | 红色左边框 | 未通过且有 reason |
| rejected 卡片 | 灰色边框 | `passed: false` |

### 容器尺寸

- 最大宽度：1200px
- 卡片 padding：28px
- meta-item padding：12px 16px
- 字体标题：28px / 副标题 14px / 标签 11px（大写）/ 数值 16px

---

## 关键设计决策

### 为什么保留 legacy 渲染路径

- 历史 run 的 screening_results.json 是评分格式，删掉 legacy 路径会导致旧报告无法重新生成
- `_is_hard_condition_format()` 按 candidates[0] 字段自动分流，新格式优先
- 新 run 一律走主格式（硬性条件），不再产出评分数据

### 为什么字段全部 .get() 处理

- screening_results.json 字段可能缺失（不同 JD / 不同 AI 输出风格不同）
- 缺失字段自动跳过对应渲染块，不报错
- 旧版本字段写死会 KeyError 崩溃

### 为什么 templates/report.html 当前不用

- 旧模板用 Jinja2 占位符，**需要额外的模板引擎依赖**
- 新方案直接 Python f-string 拼字符串，**零依赖**
- templates/ 目录保留作为参考，不删除

---

## CLI 调用示例

```bash
python generate_html_report.py \
    --job-name "主播" \
    --encrypt-job-id "9a7759badfd95d350nFz3d-_F1NX" \
    --run-id "2026-09-06_150915"
    # --input/--output 不传则用默认路径（JobOutputManager 自动拼）
```

输出：

```
HTML 报告已生成: .../9a7759badfd95d350nFz3d-_F1NX/runs/2026-09-06_150915/2026-09-06_150915_screening_report.html
文件大小: 25314 字节
候选人: 5 人
```

> **关键变化**：输出文件名包含 `run_id`，**永不会覆盖历史报告**。报告头部展示 `run-badge`（🆔 run_id + 🕐 生成时间），方便区分同一岗位的多次筛选结果。
> 若希望脚本自动从 `JobOutputManager.report_path` 拼路径，run_id 不传则使用默认值。

---

## 注意事项

- 输入文件必须是 `boss-hr automate` 产出的 `screening_results.json`，不要手工构造
- 主格式的 `passed` 必须是布尔值；`reason` 为逐条对照岗位 yaml 硬性要求的判断依据
- 候选人按 rank 升序展示（rank=1 在最前）
- HTML 是自包含单文件，所有 CSS 内联，无外部依赖，可直接邮件发送或打印
