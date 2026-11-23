# -*- coding: utf-8 -*-
"""Switch the BOSS recommend page to a target recruiter job."""
from __future__ import annotations

import time
from typing import Any, Callable


_DISPLAY_NAME_KEYS = (
    "boss_display_name",
    "boss_job_name",
    "boss_title",
    "boss_position_name",
    "display_name",
    "job_name",
)

RECOMMEND_URL = "https://www.zhipin.com/web/chat/recommend"


def _install_browser_guards(context: Any, page: Any) -> None:
    """Install stealth / request blocking / risk fuse for the shared recommend tab."""
    try:
        from .anti_detection import install_guards
    except Exception:
        try:
            from anti_detection import install_guards
        except Exception:
            return
    try:
        install_guards(context, page)
    except Exception:
        pass


def get_or_create_recommend_page(context: Any, *, log: Callable[[str], None] | None = None) -> Any:
    """Return a BOSS recommend tab, reusing existing BOSS tabs when possible.

    优先复用已有 BOSS 标签页（由调用方按需导航到推荐牛人页面），避免新开
    标签触发 BOSS 单账号会话互踢导致登录态失效。只有在没有任何 BOSS 标签
    页时才新开标签。
    """
    pages = list(getattr(context, "pages", []) or [])
    # 1. 优先复用已在推荐牛人页面的标签
    for page in pages:
        if "zhipin.com/web/chat/recommend" in (getattr(page, "url", "") or ""):
            _install_browser_guards(context, page)
            return page
    # 2. 其次复用任意 BOSS 标签页，避免新开标签触发登录态互踢；
    #    调用方（recommend_list / recommend_download / auto_greet）会按需
    #    把该标签导航到推荐牛人页面。
    for page in pages:
        if "zhipin.com" in (getattr(page, "url", "") or ""):
            _log(log, "复用已有 BOSS 标签页，避免新开标签触发登录态互踢")
            _install_browser_guards(context, page)
            return page
    # 3. 没有任何 BOSS 标签页时才新开标签
    page = context.new_page()
    _install_browser_guards(context, page)
    _log(log, "打开新的推荐牛人标签页供自动化使用")
    page.goto(RECOMMEND_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(0.8)
    return page


def _local_job_display_name(profile_name: str | None = None) -> str:
    """Read a BOSS page display-name fallback from config/jobs."""
    try:
        from boss_hr.config_manager import get_config

        config = get_config()
        names = []
        if profile_name:
            names.append(profile_name)
        default_name = config.default_job_profile_name()
        if default_name:
            names.append(default_name)

        seen: set[str] = set()
        for name in names:
            if not name or name in seen:
                continue
            seen.add(name)
            profile = config.load_job_profile(name)
            for key in _DISPLAY_NAME_KEYS:
                value = profile.get(key) if isinstance(profile, dict) else None
                if value:
                    return str(value).strip()
            if name:
                return str(name).strip()
    except Exception:
        pass
    return ""


def resolve_job_display_name(encrypt_job_id: str, fallback: str | None = None) -> str:
    """Resolve an encryptJobId to the BOSS job name, falling back to local name."""
    if encrypt_job_id:
        try:
            from recruiter_job_catalog import resolve_recruiter_job

            job = resolve_recruiter_job(encrypt_job_id)
            if isinstance(job, dict):
                name = job.get("jobName") or job.get("job_name")
                if name:
                    return str(name)
        except Exception:
            pass
    local_name = _local_job_display_name(fallback)
    return str(local_name or fallback or encrypt_job_id or "").strip()


def ensure_recommend_job_selected(
    page: Any,
    *,
    encrypt_job_id: str,
    job_name: str | None = None,
    log: Callable[[str], None] | None = None,
    timeout_ms: int = 15000,
    require_exact_job_id: bool = True,
) -> dict[str, Any]:
    """Select the target job in the BOSS recommend page dropdown.

    BOSS' public recommend page keeps the active job in the visible top
    dropdown. Candidate APIs then follow that page state, so fetch/greet must
    switch the page before scanning or clicking candidates.
    """
    target_name = resolve_job_display_name(encrypt_job_id, job_name)
    # target_name 可能为空（resolve 失败），但只要 encrypt_job_id 有值就能用 jobid 比对

    if "zhipin.com/web/chat/recommend" not in (page.url or ""):
        _log(log, "打开推荐牛人页面以切换岗位")
        page.goto(
            RECOMMEND_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        time.sleep(0.8)

    # 1. 优先用 encryptJobId 比对推荐页 iframe URL 的 jobid 参数：
    #    BOSS 推荐页 iframe URL 形如 .../frame/recommend/?jobid=<encryptJobId>，
    #    jobid 与招聘者 API 返回的 encryptJobId 同体系，可直接精确比对，绕开
    #    "API 岗位名 vs 下拉框显示名"不一致问题。已是目标岗位则直接返回。
    current_jobid = _current_recommend_jobid(page)
    if encrypt_job_id and current_jobid == encrypt_job_id:
        _log(log, f"推荐页当前已是目标岗位（jobid={current_jobid}）")
        return {"ok": True, "changed": False, "target_name": target_name,
                "current_jobid": current_jobid}

    root, offset = _recommend_content_root(page)
    # 2. iframe root 重新取到后 jobid 可能刚加载完，再确认一次
    current_jobid = _current_recommend_jobid(page)
    if encrypt_job_id and current_jobid == encrypt_job_id:
        _log(log, f"推荐页当前已是目标岗位（jobid={current_jobid}）")
        return {"ok": True, "changed": False, "target_name": target_name,
                "current_jobid": current_jobid}

    _log(log, f"准备切换推荐页岗位：target={target_name or encrypt_job_id} "
              f"(current jobid={current_jobid or '未知'})")
    clicked = _click_job_dropdown(root, page, offset)
    if not clicked:
        return {
            "ok": False,
            "changed": False,
            "target_name": target_name,
            "reason": "job dropdown not found",
            "current_jobid": current_jobid,
            "expected_jobid": encrypt_job_id,
        }
    time.sleep(1)

    option_clicked = _click_job_option(
        root, page, offset, target_name or "",
        encrypt_job_id=encrypt_job_id, timeout_ms=timeout_ms,
    )
    if not option_clicked:
        # 下拉框可能在切换后重新挂载到主页面，重开一次并在所有 frame 里找
        _click_job_dropdown(root, page, offset)
        time.sleep(1)
        option_clicked = _click_job_option(
            root, page, offset, target_name or "",
            encrypt_job_id=encrypt_job_id, timeout_ms=timeout_ms,
        )
    if not option_clicked:
        visible_options = _visible_job_option_texts(root, page)
        return {
            "ok": False,
            "changed": False,
            "target_name": target_name,
            "reason": "target job option not found",
            "visible_options": visible_options[:10],
            "current_jobid": current_jobid,
            "expected_jobid": encrypt_job_id,
        }

    # 3. 点击选项后等 iframe URL 的 jobid 变成目标值（最多 15 秒）
    new_jobid = ""
    for _ in range(15):
        new_jobid = _current_recommend_jobid(page)
        if encrypt_job_id and new_jobid == encrypt_job_id:
            break
        time.sleep(1)

    if encrypt_job_id and new_jobid == encrypt_job_id:
        _log(log, f"推荐页岗位已切换为：{target_name or encrypt_job_id} (jobid={new_jobid})")
        return {"ok": True, "changed": True, "target_name": target_name,
                "current_jobid": new_jobid}

    # 4. 只有完全读不到 jobid 时才允许用岗位名兜底。若 BOSS 已明确返回了
    # 另一个 jobid，名字相似也必须 fail-closed，避免把旧岗位候选人写入当前 run。
    if (
        not require_exact_job_id
        and not new_jobid
        and target_name
        and _top_bar_contains(root, target_name)
    ):
        _log(log, f"jobid 比对未命中但顶部栏含目标岗位名：{target_name} "
                  f"(jobid={new_jobid or '未知'}, expected={encrypt_job_id})")
        return {"ok": True, "changed": True, "target_name": target_name,
                "current_jobid": new_jobid}

    return {
        "ok": False,
        "changed": True,
        "target_name": target_name,
        "reason": (
            "target jobid unavailable after switch"
            if not new_jobid
            else "target job was clicked but jobid mismatch after switch"
        ),
        "current_jobid": new_jobid,
        "expected_jobid": encrypt_job_id,
    }


def _recommend_content_root(page: Any) -> tuple[Any, dict[str, float]]:
    """返回推荐内容所在的 frame 及其在主页面视口中的偏移。

    BOSS 推荐页通常含多个 iframe：about:srcdoc（空框架）和
    zhipin.com/web/frame/recommend（承载候选人列表与岗位下拉菜单）。
    必须选中后者，否则岗位切换/候选人扫描会在空 iframe 中查找而失败。

    注意：iframe 注册到 page.frames 时其 URL 可能还是 about:srcdoc，
    因此不能仅靠 page.frames 快照判断，必须显式等待 iframe 元素出现，
    再确认其 content_frame 已导航到 frame/recommend 并渲染出下拉菜单。
    """
    # 1. 显式等待承载推荐内容的 iframe 元素出现（解决 iframe 注册时序）
    iframe_el = None
    for _ in range(20):  # 最多等 20 秒
        try:
            iframe_el = page.query_selector('iframe[src*="frame/recommend"]')
            if iframe_el:
                break
        except Exception:
            pass
        time.sleep(1)

    if iframe_el:
        frame = iframe_el.content_frame()
        if frame:
            # 2. 等待岗位下拉菜单容器出现，确认 iframe 内容已渲染
            try:
                frame.wait_for_selector(
                    '.job-selecter-wrap, [class*="job-select"]',
                    timeout=10000,
                )
            except Exception:
                pass
            box = iframe_el.bounding_box()
            if box:
                return frame, {"x": float(box.get("x") or 0),
                               "y": float(box.get("y") or 0)}
            return frame, {"x": 0.0, "y": 0.0}

    # 3. 回退：按 page.frames 快照匹配 URL（iframe 已稳定时可用）
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        if "frame/recommend" in (frame.url or ""):
            return frame, {"x": 0.0, "y": 0.0}

    # 4. 最终回退：第一个 iframe（兼容旧逻辑）
    try:
        iframe = page.wait_for_selector("iframe", timeout=10000)
        frame = iframe.content_frame() if iframe else None
        box = iframe.bounding_box() if iframe else None
        if frame and box:
            return frame, {"x": float(box.get("x") or 0), "y": float(box.get("y") or 0)}
    except Exception:
        pass
    return page, {"x": 0.0, "y": 0.0}


def _click_job_option(root: Any, page: Any, offset: dict[str, float],
                      target_name: str, *, encrypt_job_id: str = "",
                      timeout_ms: int = 15000) -> bool:
    for label, target, target_offset in _job_option_search_targets(root, page, offset):
        # BOSS 的真实岗位选项是 li.job-item[value=encryptJobId]。使用 locator
        # 让浏览器先滚动到选项，再点击；坐标点击容易命中滚动容器中的旧位置。
        if encrypt_job_id:
            try:
                direct = target.locator(f'li.job-item[value="{encrypt_job_id}"]').first
                direct.click(timeout=min(timeout_ms, 3000))
                return True
            except Exception:
                pass
            point = _find_job_option_by_jobid(target, encrypt_job_id)
            if point:
                try:
                    page.mouse.click(point["x"] + target_offset["x"], point["y"] + target_offset["y"])
                    return True
                except Exception:
                    pass
        # 有 jobid 时不按名字猜测：同名/近名岗位可能对应不同的筛选要求。
        if target_name and not encrypt_job_id:
            point = _find_job_option_point(target, target_name)
            if point:
                try:
                    page.mouse.click(point["x"] + target_offset["x"], point["y"] + target_offset["y"])
                    return True
                except Exception:
                    pass
            # 只在岗位列表内回退按名称，避免点中页面标题或候选人卡片。
            try:
                locator = target.locator('li.job-item, [role="option"]').filter(
                    has_text=target_name
                ).first
                locator.click(timeout=min(timeout_ms, 3000))
                return True
            except Exception:
                pass
    return False


def _job_option_search_targets(root: Any, page: Any,
                               offset: dict[str, float]) -> list[tuple[str, Any, dict[str, float]]]:
    targets: list[tuple[str, Any, dict[str, float]]] = [("root", root, offset)]
    if page is not root:
        targets.append(("page", page, {"x": 0.0, "y": 0.0}))
    try:
        for idx, frame in enumerate(getattr(page, "frames", []) or []):
            if frame is root or frame is getattr(page, "main_frame", None):
                continue
            targets.append((f"frame:{idx}", frame, {"x": 0.0, "y": 0.0}))
    except Exception:
        pass
    return targets


def _click_job_dropdown(root: Any, page: Any, offset: dict[str, float]) -> bool:
    point = _find_job_dropdown_point(root)
    if point:
        try:
            page.mouse.click(point["x"] + offset["x"], point["y"] + offset["y"])
            return True
        except Exception:
            pass
    script = r"""
    () => {
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 120 && r.height > 24 && r.top >= 30 && r.top <= 120 &&
               s.visibility !== 'hidden' && s.display !== 'none';
      };
      const clickable = (el) => {
        let cur = el;
        for (let i = 0; cur && i < 4; i++, cur = cur.parentElement) {
          const role = cur.getAttribute('role') || '';
          const cls = cur.className || '';
          if (cur.tagName === 'BUTTON' || role === 'button' ||
              /select|dropdown|job|position/i.test(String(cls))) {
            return cur;
          }
        }
        return el;
      };
      const bad = /(筛选|招聘规范|我的客服|面试|招聘数据|账号权益)/;
      const els = Array.from(document.querySelectorAll(
        'button,[role=button],[class*=select],[class*=dropdown],[class*=job],[class*=position],div'
      ));
      const candidates = els
        .filter(visible)
        .map(el => ({el, r: el.getBoundingClientRect(), text: (el.innerText || el.textContent || '').trim()}))
        .filter(x => x.text.length >= 4 && x.text.length <= 100 && !bad.test(x.text))
        .filter(x => /主播|运营|剪辑|岗位|职位|K|薪|深圳|广州|上海|北京|杭州|成都/.test(x.text))
        .sort((a, b) => Math.abs(a.r.top - 58) - Math.abs(b.r.top - 58));
      if (!candidates.length) return false;
      clickable(candidates[0].el).click();
      return true;
    }
    """
    try:
        return bool(root.evaluate(script))
    except Exception:
        return False


def _find_job_dropdown_point(root: Any) -> dict[str, float] | None:
    script = r"""
    () => {
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 140 && r.height > 28 && r.top >= 0 && r.top <= 90 &&
               r.left >= 500 && r.left <= window.innerWidth - 120 &&
               s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
      };
      const bad = /(筛选|推荐牛人|招聘规范|我的客服|面试|招聘数据|账号权益|最新|精选)/;
      const score = (x) => {
        let s = 0;
        if (/job-selecter|job-select|position-select|ui-dropmenu/i.test(String(x.cls))) s += 60;
        if (/主播|运营|剪辑|岗位|职位/.test(x.text)) s += 20;
        if (/深圳|广州|上海|北京|杭州|成都/.test(x.text)) s += 6;
        if (/[0-9]+[ -]*[0-9]*K|\/天|薪/.test(x.text)) s += 6;
        if (/[∨⌄⌃︿﹀⌵⌄⌃]|收起|展开/.test(x.text)) s += 3;
        s -= Math.abs(x.r.top - 20) / 10;
        s -= Math.abs((x.r.left + x.r.right) / 2 - window.innerWidth * 0.70) / 200;
        return s;
      };
      const els = Array.from(document.querySelectorAll(
        'button,[role=button],[class*=select],[class*=dropdown],[class*=job],[class*=position],div,span'
      ));
      const candidates = els
        .filter(visible)
        .map(el => ({el, r: el.getBoundingClientRect(), text: (el.innerText || el.textContent || '').trim()}))
        .filter(x => x.text.length >= 4 && x.text.length <= 120 && !bad.test(x.text))
        .filter(x => /主播|运营|剪辑|岗位|职位|深圳|广州|上海|北京|杭州|成都|K|\/天|薪/.test(x.text))
        .sort((a, b) => score(b) - score(a));
      if (!candidates.length) return null;
      const c = candidates[0];
      return {x: Math.round(c.r.left + c.r.width / 2), y: Math.round(c.r.top + c.r.height / 2), text: c.text};
    }
    """
    try:
        point = root.evaluate(script)
        return point if isinstance(point, dict) else None
    except Exception:
        return None


def _current_recommend_jobid(page: Any) -> str:
    """返回推荐页 iframe URL 中的 jobid 参数（即当前选中岗位的 encryptJobId）。

    BOSS 推荐页 iframe URL 形如：
      https://www.zhipin.com/web/frame/recommend/?jobid=<encryptJobId>&status=0&...
    jobid 与招聘者 API 返回的 encryptJobId 同体系，可直接精确比对。
    """
    # page.frames 在 iframe 重挂载时可能同时保留旧 frame。以当前 DOM 中
    # 可见的推荐 iframe 为准，避免把旧岗位的 URL 当作切换结果。
    try:
        iframes = page.query_selector_all('iframe[src*="frame/recommend"]')
    except AttributeError:
        try:
            iframe = page.query_selector('iframe[src*="frame/recommend"]')
            iframes = [iframe] if iframe else []
        except Exception:
            iframes = []
    except Exception:
        iframes = []
    for iframe in reversed(iframes):
        try:
            if iframe and iframe.bounding_box():
                frame = iframe.content_frame()
                if frame and "frame/recommend" in (frame.url or ""):
                    return _extract_jobid_from_url(frame.url)
        except Exception:
            continue
    for frame in reversed(getattr(page, "frames", []) or []):
        if frame is getattr(page, "main_frame", None):
            continue
        url = frame.url or ""
        if "frame/recommend" in url:
            return _extract_jobid_from_url(url)
    return ""


def _extract_jobid_from_url(url: str) -> str:
    """从 .../frame/recommend/?jobid=XXX&... 提取 jobid 查询参数。"""
    from urllib.parse import urlparse, parse_qs
    try:
        parsed = urlparse(url or "")
        values = parse_qs(parsed.query).get("jobid") or [""]
        return (values[0] or "").strip()
    except Exception:
        return ""


def _find_job_option_by_jobid(root: Any, encrypt_job_id: str) -> dict[str, float] | None:
    """在下拉框选项元素的属性中匹配 encryptJobId，命中返回中心坐标供 mouse.click 使用。

    BOSS 推荐页岗位下拉框选项为 LI.job-item，岗位 ID 存放在 **value 属性**里
    （已通过 DOM 诊断确认：value='ddcab67aa485e5f10nN43dm9FlJT'）。
    也兼容 data-* / href / onclick 等其他可能的属性。
    """
    if not encrypt_job_id:
        return None
    script = r"""
    (target) => {
      const norm = (s) => String(s || '').replace(/[\s\-_]+/g, '');
      const targetNorm = norm(target);
      if (!targetNorm) return null;
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 80 && r.height > 20 && r.top >= 30 &&
               s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
      };
      // 优先匹配 BOSS 下拉框的 LI.job-item（value 属性存岗位 ID）
      const els = Array.from(document.querySelectorAll('li.job-item,[role=option]'));
      for (const el of els) {
        if (!visible(el)) continue;
        for (const attr of (el.attributes || [])) {
          const name = attr.name || '';
          const value = attr.value || '';
          // value 属性（LI.job-item）、data-*、href、onclick 都可能携带岗位 ID
          if (name === 'value' || name.startsWith('data-') ||
              name === 'href' || name === 'onclick') {
            if (norm(value).includes(targetNorm)) {
              const r = el.getBoundingClientRect();
              return {x: Math.round(r.left + r.width / 2),
                      y: Math.round(r.top + r.height / 2),
                      text: (el.innerText || el.textContent || '').trim().slice(0, 80)};
            }
          }
        }
      }
      return null;
    }
    """
    try:
        point = root.evaluate(script, encrypt_job_id)
        return point if isinstance(point, dict) else None
    except Exception:
        return None


def _find_job_option_point(root: Any, target_name: str) -> dict[str, float] | None:
    script = r"""
    (target) => {
      const norm = (s) => String(s || '')
        .replace(/\s+/g, '')
        .replace(/[()（）【】\[\]_\-—–,，、]/g, '')
        .toLowerCase();
      const targetNorm = norm(target);
      if (!targetNorm) return null;
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 120 && r.height >= 20 && r.top >= 45 &&
               r.left >= 250 && r.right <= window.innerWidth + 20 &&
               s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
      };
      const bad = /(请输入|筛选|调整顺序|推荐牛人|招聘规范|我的客服|账号权益)/;
      const els = Array.from(document.querySelectorAll('li.job-item,[role=option]'));
      const candidates = els
        .filter(visible)
        .map(el => ({el, r: el.getBoundingClientRect(), text: (el.innerText || el.textContent || '').trim()}))
        .filter(x => x.text.length >= 3 && x.text.length <= 140 && !bad.test(x.text))
        .map(x => ({...x, n: norm(x.text)}))
        .filter(x => x.n.includes(targetNorm))
        .sort((a, b) => a.text.length - b.text.length || a.r.top - b.r.top);
      if (!candidates.length) return null;
      const c = candidates[0];
      return {x: Math.round(c.r.left + c.r.width / 2), y: Math.round(c.r.top + c.r.height / 2), text: c.text};
    }
    """
    try:
        point = root.evaluate(script, target_name)
        return point if isinstance(point, dict) else None
    except Exception:
        return None


def _visible_job_option_texts(root: Any, page: Any | None = None) -> list[str]:
    script = r"""
    () => {
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 120 && r.height >= 20 && r.top >= 45 &&
               s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
      };
      const bad = /(请输入|筛选|调整顺序|推荐牛人|招聘规范|我的客服|账号权益)/;
      const seen = new Set();
      const out = [];
      for (const el of Array.from(document.querySelectorAll('li,button,[role=button],a,div,span'))) {
        if (!visible(el)) continue;
        const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
        if (text.length < 3 || text.length > 140 || bad.test(text)) continue;
        if (!/(主播|运营|剪辑|深圳|广州|上海|北京|杭州|成都|K|\/天)/.test(text)) continue;
        if (seen.has(text)) continue;
        seen.add(text);
        out.push(text);
      }
      return out.slice(0, 20);
    }
    """
    out: list[str] = []
    targets = [(root, {"x": 0.0, "y": 0.0})]
    if page is not None:
        targets = [(target, offset) for _, target, offset in _job_option_search_targets(root, page, {"x": 0.0, "y": 0.0})]
    for target, _offset in targets:
        try:
            values = target.evaluate(script)
        except Exception:
            continue
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value)
            if text and text not in out:
                out.append(text)
    return out[:20]


def _top_bar_contains(root: Any, text: str) -> bool:
    script = r"""
    (target) => {
      const els = Array.from(document.querySelectorAll('body *'));
      return els.some(el => {
        const r = el.getBoundingClientRect();
        if (r.top < 0 || r.top > 90 || r.width < 120) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none') return false;
        return (el.innerText || el.textContent || '').includes(target);
      });
    }
    """
    try:
        return bool(root.evaluate(script, text))
    except Exception:
        return False


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)


def apply_filter_not_viewed_14d(
    page: Any,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """切换岗位后点击右上角筛选下拉，勾选"近14天没有看过"+"刚刚活跃"，然后点确定。

    BOSS 推荐页右上角有一个筛选按钮，点击后弹出下拉面板，包含：
    - 浏览状态：近14天没有看过（DOM 文字可能是"近14天没有"）
    - 活跃度：刚刚活跃
    - 年龄：不动（不选）

    勾选后点击"确定"按钮关闭面板，候选人列表自动刷新。

    失败不阻断流程（返回 ok=False），由调用方决定是否继续。

    注意：此函数为兼容旧调用保留，新调用建议使用 apply_job_filters 传入岗位筛选配置。
    """
    return apply_job_filters(page, job_filter_config=None, log=log)


def _click_first_degree_checkbox(root: Any, log: Callable[[str], None] | None = None) -> bool:
    """勾选「只看第一学历」复选框，已勾选时跳过。

    BOSS 这个控件不是 ``.option`` 选项，而是复选框：
    ``<div class="first-degree-wrap"><span class="check-box"></span>
    <span class="name">只看第一学历（第一学历为全日制本科）</span></div>``，
    勾选后 ``.check-box`` 的 class 变为 ``check-box checked``。
    """
    MARK_JS = r"""
    () => {
      let wrap = document.querySelector('.first-degree-wrap');
      if (!wrap) {
        const panel = document.querySelector('.filter-panel');
        if (panel) {
          for (const el of panel.querySelectorAll('div,span,label')) {
            const t = (el.innerText || el.textContent || '').trim();
            if (t.startsWith('只看第一学历') && t.length < 40) {
              wrap = el.closest('.first-degree-wrap') || el.parentElement || el;
              break;
            }
          }
        }
      }
      if (!wrap) return null;
      wrap.setAttribute('data-boss-checkbox-target', '1');
      const box = wrap.querySelector('.check-box') || wrap;
      const cls = String(box.className || '');
      return {checked: cls.includes('checked') || cls.includes('active'), cls: cls};
    }
    """
    try:
        info = root.evaluate(MARK_JS)
    except Exception as e:
        _log(log, f"定位'只看第一学历'复选框失败: {e}")
        return False
    if not info:
        return False
    if info.get("checked"):
        _log(log, "「只看第一学历」已勾选，跳过")
        return True
    try:
        el = root.query_selector('[data-boss-checkbox-target="1"]')
        if el is None:
            return False
        el.click(timeout=5000)
        time.sleep(0.4)
        state = root.evaluate(
            "() => { const w = document.querySelector('[data-boss-checkbox-target=\"1\"]');"
            " const b = w ? (w.querySelector('.check-box') || w) : null;"
            " return b ? String(b.className || '') : null; }"
        )
        if state and ("checked" in state or "active" in state):
            _log(log, "已勾选「只看第一学历」")
            return True
        _log(log, "点击「只看第一学历」后未检测到勾选状态")
        return False
    except Exception as e:
        _log(log, f"勾选'只看第一学历'失败: {e}")
        return False
    finally:
        try:
            root.evaluate("document.querySelectorAll('[data-boss-checkbox-target]').forEach(e => e.removeAttribute('data-boss-checkbox-target'))")
        except Exception:
            pass


def apply_job_filters(
    page: Any,
    *,
    job_filter_config: dict[str, Any] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """切换岗位后点击右上角筛选下拉，按岗位配置勾选筛选条件，然后点确定。

    job_filter_config 从岗位 YAML 的 recruitment.boss_filter 读取，支持：
    - not_viewed_14d: bool — 勾选“近14天没有看过”
    - gender: str — 性别（如“女”）
    - active_status: list[str] — 勾选活跃度（如["刚刚活跃"]）
    - employment_status: list[str] — 勾选求职状态（如["离职-随时到岗"]）
    - age_max: int — 年龄上限（勾选对应年龄段）
    - graduation_year: int — 应届生毕业年份（如2027）
    - degree: str — 学历要求（如"本科"）
    - major_category: list[str] — 专业类别（如["工学","理学"]）

    如果 job_filter_config 为 None 或空，使用默认：近14天没有看过 + 刚刚活跃。

    失败不阻断流程（返回 ok=False），由调用方决定是否继续。
    """
    # 所有岗位统一勾选“刚刚活跃”和“在线”。复制配置，避免修改调用方对象。
    job_filter_config = dict(job_filter_config or {"not_viewed_14d": True})
    configured_active_status = job_filter_config.get("active_status") or []
    if isinstance(configured_active_status, str):
        configured_active_status = [configured_active_status]
    job_filter_config["active_status"] = list(dict.fromkeys(
        [str(status) for status in configured_active_status if str(status).strip()]
        + ["刚刚活跃", "在线"]
    ))

    root, offset = _recommend_content_root(page)

    def _click_by_selector(selector: str, label: str) -> bool:
        """用 frame.query_selector + click() 真实点击（patchright 自动处理 iframe 偏移）。"""
        try:
            el = root.query_selector(selector)
        except Exception:
            el = None
        if not el:
            _log(log, f"未找到{label}（selector={selector}）")
            return False
        try:
            el.click(timeout=5000)
            _log(log, f"已点击{label}")
            time.sleep(0.8)
            return True
        except Exception as e:
            _log(log, f"点击{label}失败: {e}")
            return False

    def _dismiss_recover_popup():
        """关闭"是否应用上次的筛选条件？"弹窗（每次点击选项前检查，防止弹窗遮挡）。"""
        try:
            dismissed = root.evaluate(r"""() => {
                const el = document.querySelector('.recover-last-change-params');
                if (!el) return false;
                const s = getComputedStyle(el);
                const visible = s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null;
                if (!visible) return false;
                const cancel = el.querySelector('.cancel');
                if (cancel) { cancel.click(); return true; }
                return false;
            }""")
            if dismissed:
                _log(log, "已关闭'恢复上次筛选'弹窗")
                time.sleep(0.5)
        except Exception:
            pass

    def _click_option_by_text(parent_selector: str, text_keywords: list, label: str) -> bool:
        """在下拉面板内按文字找选项并点击。如果已选中（active）则跳过，避免取消选择。"""
        _dismiss_recover_popup()
        MARK_JS = r"""
        ({parentSelector, keywords}) => {
          const parent = document.querySelector(parentSelector);
          if (!parent) return null;
          const options = parent.querySelectorAll('.option');
          // 第一轮：精确匹配（text === kw），避免"本科"误匹配到"公办本科"
          for (const opt of options) {
            const cls = String(opt.className || '');
            if (cls.includes('options') || cls.includes('default')) continue;
            const text = (opt.innerText || opt.textContent || '').trim();
            if (keywords.some(kw => text === kw)) {
              opt.scrollIntoView({block: 'center', behavior: 'instant'});
              const r = opt.getBoundingClientRect();
              const isActive = cls.includes('active');
              opt.setAttribute('data-boss-filter-target', '1');
              return {text: text, x: r.x, y: r.y, w: r.width, h: r.height,
                      cls: cls, isActive: isActive, match: 'exact'};
            }
          }
          // 第二轮：子串匹配（text.includes(kw)），作为 fallback
          for (const opt of options) {
            const cls = String(opt.className || '');
            if (cls.includes('options') || cls.includes('default')) continue;
            const text = (opt.innerText || opt.textContent || '').trim();
            if (keywords.some(kw => text.includes(kw))) {
              opt.scrollIntoView({block: 'center', behavior: 'instant'});
              const r = opt.getBoundingClientRect();
              const isActive = cls.includes('active');
              opt.setAttribute('data-boss-filter-target', '1');
              return {text: text, x: r.x, y: r.y, w: r.width, h: r.height,
                      cls: cls, isActive: isActive, match: 'substring'};
            }
          }
          return null;
        }
        """
        try:
            info = root.evaluate(MARK_JS, {"parentSelector": parent_selector, "keywords": text_keywords})
        except Exception:
            info = None
        if not info:
            _log(log, f"未找到{label}")
            return False
        if info.get('isActive'):
            _log(log, f"{label} 已选中（active），跳过点击避免取消")
        else:
            try:
                el = root.query_selector('[data-boss-filter-target="1"]')
                if el:
                    el.click(timeout=5000)
                    _log(log, f"已点击{label}")
                    time.sleep(0.8)
            except Exception as e:
                _log(log, f"点击{label}失败: {e}")
                return False
        # 清除标记（CSS 选择器必须用引号包裹，否则 JS 会把 [data-...] 当成数组字面量）
        try:
            root.evaluate("document.querySelectorAll('[data-boss-filter-target]').forEach(e => e.removeAttribute('data-boss-filter-target'))")
        except Exception:
            pass
        return True

    # Step 1: 点击筛选按钮，打开下拉面板
    # DOM: div.filter-label text='筛选'
    if not _click_by_selector('.filter-label', "筛选按钮"):
        # 回退：用 filter-label-wrap
        if not _click_by_selector('.filter-label-wrap', "筛选按钮(.filter-label-wrap)"):
            return {"ok": False, "reason": "filter button not found"}

    # 等待筛选面板展开
    try:
        root.wait_for_selector('.filter-panel', timeout=5000)
        _log(log, "筛选面板已展开")
    except Exception:
        _log(log, "筛选面板未展开，可能已展开或选择器不对")

    # 面板展开后关闭"是否应用上次的筛选条件？"弹窗（此弹窗在面板展开后才出现）
    DISMISS_RECOVER_JS = r"""
    () => {
        const el = document.querySelector('.recover-last-change-params');
        if (!el) return false;
        const s = getComputedStyle(el);
        const visible = s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null;
        if (!visible) return false;
        const cancel = el.querySelector('.cancel');
        if (cancel) { cancel.click(); return true; }
        return false;
    }
    """
    try:
        dismissed = root.evaluate(DISMISS_RECOVER_JS)
        if dismissed:
            _log(log, "已关闭'恢复上次筛选'弹窗")
            time.sleep(0.8)
    except Exception:
        pass

    # Step 1.5: 点击"清除"按钮重置所有筛选条件，确保每次从干净状态开始
    # （不依赖之前的选中状态，避免第二次循环时条件残留导致逻辑错乱）
    CLEAR_JS = r"""
    () => {
      const panel = document.querySelector('.filter-panel');
      if (!panel) return false;
      const btns = panel.querySelectorAll('.btn');
      for (const btn of btns) {
        const cls = String(btn.className || '');
        if (!cls.includes('btn-outline')) continue;  // "清除"是 btn-outline
        const text = (btn.innerText || btn.textContent || '').trim();
        if (text === '清除' || text === '清空' || text === '重置') {
          btn.click();
          return true;
        }
      }
      return false;
    }
    """
    try:
        cleared = root.evaluate(CLEAR_JS)
        if cleared:
            _log(log, "已点击'清除'按钮，重置筛选条件")
            time.sleep(1.0)  # 等待 BOSS 清除所有条件并更新 DOM
        else:
            _log(log, "未找到'清除'按钮（可能无已选条件），直接应用筛选")
    except Exception as e:
        _log(log, f"点击'清除'按钮异常: {e}")

    # Step 2: 按岗位配置勾选筛选条件
    # 2a: 近14天没有看过
    if job_filter_config.get("not_viewed_14d", True):
        if not _click_option_by_text('.filter-panel', ['近14天没有', '14天', '没有看过'], "'近14天没有看过'"):
            _log(log, "未找到'近14天没有看过'，继续")

    # 2b: 性别（如“女”）。配置了性别时属于强制前置条件：找不到选项就
    # 禁止继续卡片预筛，避免把未按性别过滤的候选人带入后续流程。
    gender = str(job_filter_config.get("gender") or "").strip()
    if gender:
        if not _click_option_by_text('.filter-panel', [gender], f"性别'{gender}'"):
            return {
                "ok": False,
                "reason": f"required gender filter not found: {gender}",
            }

    # 2c: 活跃度（如"刚刚活跃"）
    active_status = job_filter_config.get("active_status") or []
    for status in active_status:
        if not _click_option_by_text('.filter-panel', [status], f"'{status}'"):
            _log(log, f"未找到活跃度'{status}'，继续")

    # 2d: 求职状态（如"离职-随时到岗"）
    employment_status = job_filter_config.get("employment_status") or []
    for status in employment_status:
        # DOM 文字可能是"离职-随时到岗"或"离职 随时到岗"
        keywords = [status, status.replace('-', ' ')]
        if not _click_option_by_text('.filter-panel', keywords, f"'{status}'"):
            _log(log, f"未找到求职状态'{status}'，继续")

    # 2e: 年龄上限（BOSS 年龄是滑块不是选项，跳过；由 card_prescreen 的 max_age 在卡片阶段筛选）
    age_max = job_filter_config.get("age_max")
    if age_max:
        _log(log, f"年龄<= {age_max}：BOSS筛选面板是滑块，由卡片预筛 max_age={age_max} 处理")

    # 2f: 应届生毕业年份（BOSS 选项文本为"27年毕业"，不是"2027届"）
    graduation_year = job_filter_config.get("graduation_year")
    if graduation_year:
        year_short = str(graduation_year)[-2:]  # 2027 → "27"
        grad_keywords = [f"{year_short}年毕业", f"{graduation_year}年毕业", f"{graduation_year}届"]
        if not _click_option_by_text('.filter-panel', grad_keywords, f"'{graduation_year}届({year_short}年毕业)'"):
            _log(log, f"未找到'{graduation_year}届'筛选选项，继续")

    # 2g: 学历（如"本科"；支持列表，多选如"大专"+"本科"）
    degree = job_filter_config.get("degree")
    degree_values = degree if isinstance(degree, (list, tuple)) else ([degree] if degree else [])
    for degree_value in degree_values:
        text = str(degree_value).strip()
        if not text:
            continue
        if not _click_option_by_text('.filter-panel', [text], f"学历'{text}'"):
            _log(log, f"未找到学历'{text}'筛选选项，继续")

    # 2h: 院校类型（如"公办本科"，BOSS 选项文本就是"公办本科"）
    school_type = job_filter_config.get("school_type")
    if school_type:
        if not _click_option_by_text('.filter-panel', [school_type], f"院校'{school_type}'"):
            _log(log, f"未找到院校'{school_type}'筛选选项，继续")

    # 2h-2: 只看第一学历（复选框 .first-degree-wrap，不是 .option 选项）
    if job_filter_config.get("first_degree_only"):
        if not _click_first_degree_checkbox(root, log):
            _log(log, "未找到'只看第一学历'复选框，继续")

    # 2i: 工作经验（如“1-3年”）。允许字符串或列表，便于组合多个区间。
    experience_years = job_filter_config.get("experience_years") or []
    if isinstance(experience_years, str):
        experience_years = [experience_years]
    for experience in experience_years:
        text = str(experience).strip()
        if not text:
            continue
        keywords = [text, text.replace("-", "至"), text.replace("至", "-")]
        if not _click_option_by_text('.filter-panel', keywords, f"工作经验'{text}'"):
            _log(log, f"未找到工作经验'{text}'筛选选项，继续")

    # Step 3: 点击"确定"按钮关闭筛选面板
    # DOM: div.btn text='确定'（注意不是 div.btn.btn-outline，那是"清除"）
    _dismiss_recover_popup()
    CONFIRM_JS = r"""
    () => {
      const panel = document.querySelector('.filter-panel');
      if (!panel) return null;
      const btns = panel.querySelectorAll('.btn');
      for (const btn of btns) {
        const cls = String(btn.className || '');
        if (cls.includes('btn-outline')) continue;  // 跳过"清除"按钮
        const text = (btn.innerText || btn.textContent || '').trim();
        if (text === '确定' || text === '应用' || text === '确认') {
          btn.setAttribute('data-boss-filter-confirm', '1');
          const r = btn.getBoundingClientRect();
          return {text: text, x: r.x, y: r.y, w: r.width, h: r.height, cls: cls};
        }
      }
      return null;
    }
    """
    try:
        info = root.evaluate(CONFIRM_JS)
    except Exception:
        info = None
    if info:
        _log(log, f"找到确定按钮 text='{info['text']}' class='{info['cls']}'")
        try:
            el = root.query_selector('[data-boss-filter-confirm="1"]')
            if el:
                el.click(timeout=5000)
                root.evaluate("document.querySelectorAll('[data-boss-filter-confirm]').forEach(e => e.removeAttribute('data-boss-filter-confirm'))")
                _log(log, "已点击确定按钮，筛选面板关闭")
                time.sleep(0.8)  # 等待面板关闭 + 候选人列表刷新
        except Exception as e:
            _log(log, f"点击确定按钮失败: {e}")
    else:
        if gender:
            return {
                "ok": False,
                "reason": "required gender filter could not be confirmed",
            }
        _log(log, "未找到确定按钮，尝试点空白处关闭")
        page.mouse.click(50, 200)
        time.sleep(1)

    # Step 4: 验证筛选面板已关闭（最多等 10 秒）
    PANEL_CLOSED_JS = r"""
    () => {
      const panel = document.querySelector('.filter-panel');
      if (!panel) return true;
      const s = getComputedStyle(panel);
      return s.display === 'none' || s.visibility === 'hidden' || panel.offsetParent === null;
    }
    """
    for _ in range(10):
        try:
            closed = root.evaluate(PANEL_CLOSED_JS)
            if closed:
                _log(log, "筛选面板已确认关闭")
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        _log(log, "筛选面板可能未关闭，继续执行（不阻断）")

    applied = [k for k, v in job_filter_config.items() if v]
    _log(log, f"筛选完成：{', '.join(applied)}")
    return {"ok": True, "filter": ", ".join(applied)}


def _build_age_keywords(age_max: int) -> list[str]:
    """根据年龄上限生成 BOSS 筛选面板上对应的年龄段关键词。

    BOSS 筛选面板的年龄段选项通常是：
    18-24岁、25-29岁、30-34岁、35-39岁、40岁以上

    策略：勾选所有"上限 <= age_max"的年龄段。
    例如 age_max=33 → 勾选"18-24岁"+"25-29岁"（不勾"30-34岁"因为上限34>33）
    实际 BOSS 的年龄段是范围，我们匹配包含的段。
    """
    keywords = []
    if age_max >= 24:
        keywords.extend(["18-24", "18-24岁"])
    if age_max >= 29:
        keywords.extend(["25-29", "25-29岁"])
    if age_max >= 34:
        keywords.extend(["30-34", "30-34岁"])
    if age_max >= 39:
        keywords.extend(["35-39", "35-39岁"])
    if age_max >= 40:
        keywords.extend(["40", "40以上", "40岁"])
    return keywords
__all__ = [
    "RECOMMEND_URL",
    "ensure_recommend_job_selected",
    "get_or_create_recommend_page",
    "resolve_job_display_name",
    "apply_filter_not_viewed_14d",
    "apply_job_filters",
]
