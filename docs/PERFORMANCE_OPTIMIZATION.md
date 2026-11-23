# 招聘流程性能优化部署说明

## 生效行为

- 单段截图首次失败后只重试 1 次，总计最多 2 次。
- AI 首次失败后复用同一份已保存截图重试 1 次，总计最多 2 次。
- 截图最终失败记录为 `screenshot_failed`，AI 最终失败记录为
  `analysis_unavailable`；两者都会关闭当前简历并继续下一候选人。
- 简历滚动步长提高到视口的 68%～78%，同时保留至少 22% 重叠。
- 每段截图前检查连续覆盖；疑似末屏会等待懒加载稳定，并以最新页面高度
  验证已真实截图到底部。没有完整性标记的旧截图不会直接复用。
- 推荐列表、打开简历、分段截图、关闭简历和旧入口节流等待均已缩短。
- 视觉 AI 单次超时调整为 40 秒。

## 新电脑需要覆盖的运行文件

保持下列相对目录不变，覆盖到新电脑项目根目录：

1. `boss_hr/config_manager.py`
2. `config/performance.yaml`（新增文件）
3. `config/ai.yaml`
4. `ai/client.py`
5. `boss-recommend-downloader/scripts/recommend_download.py`
6. `boss-recommend-downloader/scripts/recommend_list.py`

必须同时覆盖 `boss_hr/config_manager.py` 和新增的 `config/performance.yaml`，
否则性能配置不会被加载。

## 配置说明

所有性能字段及单位都写在 `config/performance.yaml` 的行内注释中。时间单位
统一为秒，形如 `[0.6, 1.0]` 的字段表示运行时在最小值与最大值之间随机取值。

不要把 `screenshot.scroll_ratio` 的最大值提高到 1。程序会额外把它限制在
0.9 以内，并执行连续覆盖检查；配置不安全时会失败并记录，而不会生成不完整
简历长图。

如果新电脑网络较慢，优先调高以下字段，而不要降低截图完整性检查：

- `screenshot.initial_wait_seconds`
- `screenshot.segment_wait_seconds`
- `ai.vision_timeout_seconds`（位于 `config/ai.yaml`）
- `browser.list_scroll_wait_seconds`

## 验证结果

- 完整测试：535 passed，65 skipped，0 failed。
- 语法编译检查通过。
- 已覆盖动态懒加载导致简历高度在末屏继续增长的测试。
- 已覆盖截图和 AI 均只重试一次的测试。
