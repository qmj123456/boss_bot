# 工作经历证据格式热修复（2026-09-23）

## 修复内容

- AI 的 `work_evidence` 标准输出统一为 JSON 数组。
- 兼容旧版 `{"items": [...]}` 包装格式，校验和保存前统一转换成数组。
- `selected_work_index` 同时兼容 JSON 整数和纯数字字符串。
- 覆盖亚马逊运营、TikTok运营、初级剪拍、运营助理、运营助理（中控轮岗）。
- 真正缺少工作经历证据或不满足岗位硬性条件的候选人仍会被拦截。

## 安装方法

1. 停止正在运行的 daemon、计划任务和相关 `boss-hr` 自动化进程。
2. 备份部署目录中的以下文件：
   - `ai/resume_rules.py`
   - `ai/resume_analyzer.py`
3. 将本压缩包解压到现有 `boss_bot` 项目根目录，允许覆盖同名文件。
4. 在项目根目录执行：

   ```bat
   python -m pip install -e .
   python -m py_compile ai\resume_rules.py ai\resume_analyzer.py
   python -m pytest tests\test_requirements_update.py -q -k "configured_evidence_jobs or work_evidence_schema or legacy_wrapped_platform_evidence"
   ```

5. 重新启动 daemon 或计划任务。

## 注意事项

- 本包不包含 `.env`、数据库、候选人数据、日志或简历截图，不会覆盖现有业务数据。
- 修复从下一次 AI 简历分析开始生效。
- 历史上已经被错误判定为不通过的候选人不会自动改写，需要重新进入分析流程。
