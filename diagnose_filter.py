#!/usr/bin/env python3
"""诊断 BOSS 推荐页筛选下拉框的 DOM 结构。

使用方法：
1. 用 Edge 打开 BOSS 推荐页 https://www.zhipin.com/web/chat/recommend
2. 手动点击右上角的"筛选"按钮，让下拉面板展开
3. 在另一个终端运行：python -X utf8 diagnose_filter.py

输出：
- 筛选按钮的位置和 class
- 下拉面板内所有可见文本元素（含坐标、class、tag）
- 确定按钮的位置和 class
"""
import json
import sys
import time
from patchright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"


def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if "zhipin.com" in (pg.url or ""):
                page = pg
                break
        if not page:
            print("未找到 BOSS 页面")
            return

        print(f"当前页面: {page.url}")
        time.sleep(2)

        # 找推荐页 iframe
        frames = []
        for f in page.frames:
            frames.append((f.url or "", f))
        print(f"\nFrames ({len(frames)}):")
        for url, _ in frames:
            print(f"  {url[:80]}")

        # 在推荐页 iframe 内扫描
        for url, frame in frames:
            if "frame/recommend" not in (url or ""):
                continue
            print(f"\n=== 推荐 iframe: {url[:60]} ===")

            # 扫描所有可见文本元素
            SCAN_JS = r"""
            () => {
              const els = Array.from(document.querySelectorAll('*'));
              const out = [];
              for (const el of els) {
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) continue;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;
                const text = (el.innerText || el.textContent || '').trim();
                if (!text || text.length > 50) continue;
                // 只取叶子节点（没有子元素有文本的）
                const hasChildText = Array.from(el.children).some(c =>
                  (c.innerText || c.textContent || '').trim().length > 0
                );
                if (hasChildText && el.children.length > 0) continue;
                out.push({
                  tag: el.tagName,
                  cls: String(el.className || '').slice(0, 60),
                  text: text,
                  x: Math.round(r.x),
                  y: Math.round(r.y),
                  w: Math.round(r.width),
                  h: Math.round(r.height),
                });
              }
              return out.slice(0, 100);
            }
            """
            try:
                elements = frame.evaluate(SCAN_JS)
            except Exception as e:
                print(f"  扫描失败: {e}")
                continue

            print(f"\n可见文本元素 ({len(elements)}):")
            for el in elements:
                print(f"  [{el['tag']}] '{el['text']}' "
                      f"class='{el['cls']}' "
                      f"pos=({el['x']},{el['y']}) "
                      f"size={el['w']}x{el['h']}")

            # 特别找筛选/确定/14天/活跃相关元素
            print("\n=== 筛选相关元素 ===")
            FILTER_JS = r"""
            () => {
              const els = Array.from(document.querySelectorAll('*'));
              const out = [];
              for (const el of els) {
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) continue;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                const text = (el.innerText || el.textContent || '').trim();
                const cls = String(el.className || '');
                if (text.includes('筛选') || text.includes('14天') ||
                    text.includes('没有看过') || text.includes('近14') ||
                    text.includes('刚刚活跃') || text.includes('确定') ||
                    text.includes('应用') || text.includes('确认') ||
                    cls.includes('filter') || cls.includes('Filter') ||
                    cls.includes('close') || cls.includes('Close')) {
                  out.push({
                    tag: el.tagName,
                    cls: cls.slice(0, 80),
                    text: text.slice(0, 50),
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                  });
                }
              }
              return out.slice(0, 30);
            }
            """
            try:
                filter_els = frame.evaluate(FILTER_JS)
                for el in filter_els:
                    print(f"  [{el['tag']}] '{el['text']}' "
                          f"class='{el['cls']}' "
                          f"pos=({el['x']},{el['y']}) "
                          f"size={el['w']}x{el['h']}")
            except Exception as e:
                print(f"  筛选扫描失败: {e}")


if __name__ == "__main__":
    main()
