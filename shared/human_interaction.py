#!/usr/bin/env python3
"""拟人化浏览器交互工具(适配 patchright / playwright 同步 API)。

提供行为层反检测能力,与 patchright 的静态指纹规避互补:
  - generate_human_path    : 生成带贝塞尔曲线 + 抖动的拟人鼠标路径
  - human_move             : 拟人移动鼠标到视口坐标
  - human_move_and_click   : 拟人移动并(可选)点击某个元素
  - human_scroll           : 通过真实滚轮滚动(替代 window.scrollBy)
  - human_browse_context   : 制造一次"列表浏览"UI 上下文(移动+小幅滚动,不点击)
  - human_type_text        : 逐字符输入(38~125ms 随机间隔,换行用 Shift+Enter)
  - human_pause            : 随机短暂停顿

注意:
  - 必须用 page.mouse.click,不能用 element.click()(平台会识别为脚本)。
  - iframe 内元素 bounding_box 是相对 iframe 文档的坐标,需加 iframe 视口偏移。
  - Playwright 不暴露当前鼠标坐标,这里用模块级 _last_pos 手动跟踪。
  - 输入间隔 / 鼠标按下时长可用环境变量覆盖:
      BOSS_HR_TYPING_MIN_MS / BOSS_HR_TYPING_MAX_MS (默认 38 / 125)
      BOSS_HR_CLICK_MIN_MS  / BOSS_HR_CLICK_MAX_MS  (默认 55 / 180)
"""
import math
import os
import random
import time

# 跟踪上次鼠标位置,使下一次移动连贯
_last_pos = None


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _typing_gap_ms():
    low = max(1, _env_int("BOSS_HR_TYPING_MIN_MS", 38))
    high = max(low, _env_int("BOSS_HR_TYPING_MAX_MS", 125))
    return low, high


def _click_press_ms():
    low = max(1, _env_int("BOSS_HR_CLICK_MIN_MS", 55))
    high = max(low, _env_int("BOSS_HR_CLICK_MAX_MS", 180))
    return low, high


def human_pause(min_seconds=0.12, max_seconds=0.35):
    """随机停顿一小段,模拟人类操作之间的节奏。"""
    low = max(0.0, float(min_seconds))
    high = max(low, float(max_seconds))
    time.sleep(random.uniform(low, high))


def human_type_text(element, text, *, min_gap_ms=None, max_gap_ms=None,
                    newline_key="Shift+Enter"):
    """逐字符输入文本,字符间隔默认 38~125ms。

    - 换行用 Shift+Enter,避免在 BOSS 聊天框里触发 Enter 提前发送。
    - 返回 True 表示整段输入完成;异常时返回 False,由调用方决定是否回退到
      JS 赋值。注意:返回 False 时可能已经输入了一部分,调用方回退前应先清空。
    """
    if element is None:
        return False
    low, high = _typing_gap_ms()
    if min_gap_ms is not None:
        low = max(1, int(min_gap_ms))
    if max_gap_ms is not None:
        high = max(low, int(max_gap_ms))

    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    try:
        element.focus()
    except Exception:
        pass

    try:
        for index, char in enumerate(text):
            if char == "\n":
                element.press(newline_key)
            else:
                element.type(char, delay=0)
            if index < len(text) - 1:
                time.sleep(random.randint(low, high) / 1000.0)
        return True
    except Exception:
        return False


def _ensure_last_pos(viewport):
    global _last_pos
    if _last_pos is None:
        w = (viewport or {}).get("width", 1280)
        h = (viewport or {}).get("height", 800)
        _last_pos = (random.randint(200, max(201, w - 200)),
                     random.randint(200, max(201, h - 200)))
    return _last_pos


def generate_human_path(start, end, curvature=2.0):
    """二次贝塞尔 + 抖动,返回拟人路径点列表。"""
    sx, sy = start
    ex, ey = end
    mx = (sx + ex) / 2 + random.uniform(-1, 1) * (ex - sx) * 0.2
    my = (sy + ey) / 2 + random.uniform(-1, 1) * (ey - sy) * 0.2
    n = max(10, int(math.hypot(ex - sx, ey - sy) / 12))
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * mx + t ** 2 * ex
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * my + t ** 2 * ey
        x += random.uniform(-1.5, 1.5)
        y += random.uniform(-1.5, 1.5)
        pts.append((x, y))
    return pts


def _element_viewport_point(page, selector, frame=None, iframe_box=None, timeout=3000):
    """返回元素在视口坐标系下的随机内点;找不到返回 None。"""
    target = frame if frame is not None else page
    try:
        el = target.wait_for_selector(selector, timeout=timeout)
    except Exception:
        return None
    if el is None:
        return None
    box = el.bounding_box()
    if not box:
        return None
    ox = iframe_box["x"] if iframe_box else 0
    oy = iframe_box["y"] if iframe_box else 0
    return (ox + box["x"] + box["width"] * random.uniform(0.3, 0.7),
            oy + box["y"] + box["height"] * random.uniform(0.3, 0.7))


def human_move(page, point):
    """拟人移动鼠标到指定视口坐标。"""
    global _last_pos
    _ensure_last_pos(page.viewport_size or {})
    for (x, y) in generate_human_path(_last_pos, point):
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.005, 0.02))
    _last_pos = point


def human_move_and_click(page, selector, frame=None, iframe_box=None,
                         hover_only=False, timeout=3000):
    """拟人移动并(可选)点击某元素。

    hover_only=True 时只悬停不点击。必须用 page.mouse.click。
    返回 True/False 表示是否成功定位到元素。
    """
    pt = _element_viewport_point(page, selector, frame=frame,
                                 iframe_box=iframe_box, timeout=timeout)
    if pt is None:
        return False
    human_move(page, pt)
    time.sleep(random.uniform(0.08, 0.2))
    if not hover_only:
        low, high = _click_press_ms()
        try:
            page.mouse.click(pt[0], pt[1], delay=random.randint(low, high))
        except TypeError:
            # 旧版 Patchright / Playwright 不支持 delay 参数
            page.mouse.click(pt[0], pt[1])
    return True


def human_scroll(page, iframe_box=None, min_delta=1000, max_delta=1800):
    """通过真实滚轮滚动(替代 window.scrollBy)。"""
    if iframe_box:
        ix = iframe_box["x"] + iframe_box["width"] * random.uniform(0.3, 0.7)
        iy = iframe_box["y"] + iframe_box["height"] * random.uniform(0.3, 0.7)
        human_move(page, (ix, iy))
    delta = random.randint(min_delta, max_delta)
    steps = random.randint(2, 4)
    per = delta // steps
    for _ in range(steps):
        page.mouse.wheel(0, per)
        time.sleep(random.uniform(0.15, 0.4))


def human_browse_context(page, iframe_box=None, do_scroll=True):
    """制造一次"正在浏览列表"的 UI 上下文:移动鼠标 + (可选)小幅滚动。

    不点击任何元素,因此不会触发额外 quota,也不会因定位不到卡片而失败。
    用于在每个 fetch 前补齐行为上下文(方案 A)。
    """
    if iframe_box:
        tx = iframe_box["x"] + iframe_box["width"] * random.uniform(0.2, 0.8)
        ty = iframe_box["y"] + iframe_box["height"] * random.uniform(0.2, 0.8)
    else:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        tx = random.randint(200, max(201, vp["width"] - 200))
        ty = random.randint(200, max(201, vp["height"] - 200))
    human_move(page, (tx, ty))
    if do_scroll:
        page.mouse.wheel(0, random.randint(300, 700))
        time.sleep(random.uniform(0.2, 0.5))
