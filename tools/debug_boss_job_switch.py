#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump visible BOSS recommend-page controls for job-switch debugging."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "shared"
for path in (ROOT, SHARED):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from boss_job_switcher import _find_job_dropdown_point, _recommend_content_root  # noqa: E402
from patchright.sync_api import sync_playwright  # noqa: E402


_DUMP_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 20 && r.height > 10 &&
           r.top >= 0 && r.top <= 180 &&
           s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const out = [];
  for (const el of Array.from(document.querySelectorAll('button,[role=button],a,div,span,input'))) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    const text = (el.innerText || el.textContent || el.getAttribute('placeholder') || '')
      .trim().replace(/\s+/g, ' ');
    const cls = String(el.className || '');
    if (!text && !cls) continue;
    out.push({
      tag: el.tagName,
      role: el.getAttribute('role') || '',
      cls: cls.slice(0, 120),
      text: text.slice(0, 160),
      x: Math.round(r.left),
      y: Math.round(r.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
    });
  }
  return out
    .filter(x => x.text || /select|dropdown|job|position|filter/i.test(x.cls))
    .slice(0, 120);
}
"""


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        pages = [pg for ctx in browser.contexts for pg in ctx.pages]
        print(json.dumps(
            [{"index": i, "url": pg.url, "title": pg.title()} for i, pg in enumerate(pages)],
            ensure_ascii=False,
            indent=2,
        ))
        page = next((pg for pg in pages if "zhipin.com/web/chat/recommend" in (pg.url or "")), None)
        if page is None:
            print("未找到推荐牛人页")
            return 1
        print("selected:", page.url)
        root, offset = _recommend_content_root(page)
        print("content_offset:", json.dumps(offset, ensure_ascii=False))
        print("dropdown_point:", json.dumps(_find_job_dropdown_point(root), ensure_ascii=False))
        print(json.dumps(root.evaluate(_DUMP_JS), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
