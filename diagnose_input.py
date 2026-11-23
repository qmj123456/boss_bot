#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断 BOSS 推荐页聊天输入框的 DOM 结构。

用途：定位 auto_greet._send_custom_message 找不到聊天输入框的根因。
不改任何业务代码，只读取 BOSS 浏览器当前状态。

前置条件：
1. Edge 已开启并带 --remote-debugging-port=9222（boss-hr 流程正常启动的话已开）
2. BOSS 已登录，推荐页已打开（https://www.zhipin.com/web/chat/recommend）
3. 已手动点过一次"打招呼"，让聊天面板渲染出来（重要！否则扫不到输入框）

使用：
    python -X utf8 diagnose_input.py
"""
from __future__ import annotations
import sys
from patchright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
RECOMMEND_URL_HINT = "zhipin.com/web/chat/recommend"

# 扫描这几类常见聊天输入元素
SCAN_JS = r"""() => {
    const results = [];
    const seen = new Set();

    function collectEls(els, selector) {
        for (const el of els) {
            if (seen.has(el)) continue;
            seen.add(el);
            const rect = el.getBoundingClientRect();
            const visible = el.offsetWidth > 0 && el.offsetHeight > 0
                            && rect.width > 0 && rect.height > 0;
            results.push({
                selector: selector,
                tag: el.tagName,
                type: el.getAttribute('type') || '',
                placeholder: el.getAttribute('placeholder') || '',
                className: (el.className || '').toString().slice(0, 120),
                id: el.id || '',
                name: el.getAttribute('name') || '',
                contentEditable: el.getAttribute('contenteditable') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                dataTestId: el.getAttribute('data-test') || el.getAttribute('data-testid') || '',
                visible: visible,
                rect: {x: rect.x.toFixed(1), y: rect.y.toFixed(1), w: rect.width.toFixed(1), h: rect.height.toFixed(1)},
                value_len: (el.value || el.textContent || '').length,
            });
        }
    }

    collectEls(document.querySelectorAll('textarea'), 'textarea');
    collectEls(document.querySelectorAll('div[contenteditable="true"]'), 'div[contenteditable="true"]');
    collectEls(document.querySelectorAll('div[contenteditable="plaintext-only"]'), 'div[contenteditable="plaintext-only"]');
    collectEls(document.querySelectorAll('input[type="text"]'), 'input[type="text"]');
    collectEls(document.querySelectorAll('input:not([type])'), 'input:not([type])');
    // 兜底：所有 contenteditable
    collectEls(document.querySelectorAll('[contenteditable]'), '[contenteditable]');
    return results;
}"""

SEND_BTN_JS = r"""() => {
    const btns = document.querySelectorAll('button, a, span, div[role="button"], [class*="send"], [class*="Send"]');
    const hits = [];
    for (const b of btns) {
        const t = (b.textContent || '').trim();
        if (t === '发送' || t === 'Send' || t === '发送消息' || t === '发送（Enter）' || t === '发送 (Enter)') {
            const rect = b.getBoundingClientRect();
            hits.push({
                tag: b.tagName,
                text: t,
                className: (b.className || '').toString().slice(0, 100),
                id: b.id || '',
                visible: b.offsetWidth > 0 && b.offsetHeight > 0,
                rect: {x: rect.x.toFixed(1), y: rect.y.toFixed(1), w: rect.width.toFixed(1), h: rect.height.toFixed(1)},
            });
        }
    }
    return hits;
}"""

GREET_BTN_JS = r"""() => {
    // 看当前页面还有几个"打招呼"按钮 vs "继续沟通"按钮，判断页面招呼状态
    let greet = 0, greeted = 0;
    const btns = document.querySelectorAll('button, a, span, div[role="button"]');
    for (const b of btns) {
        const t = (b.textContent || '').trim();
        if (t === '打招呼' && b.offsetWidth > 0) greet++;
        else if (t === '继续沟通' && b.offsetWidth > 0) greeted++;
    }
    return {greet_btn_count: greet, greeted_btn_count: greeted};
}"""

# 扫描右下角聊天窗口的关闭按钮和容器
CLOSE_BTN_JS = r"""() => {
    const hits = [];
    // 1) 常见关闭按钮选择器
    const sels = ['button.close', '.ico-close', '.btn-close', '[class*="close"][role="button"]',
                  'a.close', 'span.close', '.drawer-close', '[aria-label*="close"]',
                  '[class*="drawer"] [class*="close"]', 'i.ic-close', 'i.icon-close',
                  '[class*="ChatHeader"] [class*="close"]', '[class*="chat-close"]',
                  '.bosschat-close', '.boss-chat-close', '[class*="bosschat"] [class*="close"]',
                  '[class*="boss-chat"] [class*="close"]', '.chat-global-close',
                  '[class*="ChatDrawer"] [class*="close"]', '[class*="chat-drawer"] [class*="close"]'];
    for (const s of sels) {
        const els = document.querySelectorAll(s);
        for (const el of els) {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            const visible = rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden' && style.display !== 'none';
            hits.push({
                source: 'css:' + s,
                tag: el.tagName,
                text: (el.textContent || '').trim().slice(0, 30),
                className: (el.className || '').toString().slice(0, 150),
                id: el.id || '',
                visible: visible,
                rect: {x: rect.x.toFixed(1), y: rect.y.toFixed(1), w: rect.width.toFixed(1), h: rect.height.toFixed(1)},
                ariaLabel: el.getAttribute('aria-label') || '',
            });
        }
    }
    // 2) 找含"关闭"/"×"/"x"/"✕"的可见按钮
    const btns = [...document.querySelectorAll('button, a, div[role="button"], i, span')];
    for (const b of btns) {
        const t = (b.textContent || '').trim();
        const cls = String(b.className || '');
        if (t === '关闭' || t === '×' || t === 'x' || t === '✕' || /close|shutdown|Close/i.test(cls)) {
            const rect = b.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                hits.push({
                    source: 'text:' + t,
                    tag: b.tagName,
                    text: t,
                    className: cls.slice(0, 150),
                    id: b.id || '',
                    visible: true,
                    rect: {x: rect.x.toFixed(1), y: rect.y.toFixed(1), w: rect.width.toFixed(1), h: rect.height.toFixed(1)},
                    ariaLabel: b.getAttribute('aria-label') || '',
                });
            }
        }
    }
    // 3) 扫描右下角区域的容器（class 含 chat/drawer/dialog）
    const containers = [];
    const allEls = [...document.querySelectorAll('[class]')];
    for (const el of allEls) {
        const cls = String(el.className || '');
        if (/chat-panel|chat-window|drawer|chat-container|chat-box|im-dialog|chat-room|ChatDrawer|bosschat|conversation|chat-global|chat-main|chat-wrap/i.test(cls)) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 100 && rect.height > 100) {
                containers.push({
                    tag: el.tagName,
                    className: cls.slice(0, 200),
                    id: el.id || '',
                    rect: {x: rect.x.toFixed(1), y: rect.y.toFixed(1), w: rect.width.toFixed(1), h: rect.height.toFixed(1)},
                    childCount: el.children.length,
                });
            }
        }
    }
    // 4) 如果找到 boss-chat-global-input，扫它的祖先链找容器和关闭按钮
    const inputEl = document.querySelector('#boss-chat-global-input');
    const ancestors = [];
    if (inputEl) {
        let el = inputEl;
        for (let i = 0; i < 10 && el; i++) {
            const rect = el.getBoundingClientRect();
            ancestors.push({
                depth: i,
                tag: el.tagName,
                className: (el.className || '').toString().slice(0, 200),
                id: el.id || '',
                rect: {x: rect.x.toFixed(1), y: rect.y.toFixed(1), w: rect.width.toFixed(1), h: rect.height.toFixed(1)},
            });
            el = el.parentElement;
        }
    }
    return {close_buttons: hits, chat_containers: containers, input_ancestors: ancestors};
}"""


def scan_frame(frame, label: str) -> None:
    print(f"\n----- [{label}] URL: {frame.url[:100]} -----")
    try:
        greet_status = frame.evaluate(GREET_BTN_JS)
        print(f"  打招呼按钮: {greet_status['greet_btn_count']} 个 | 继续沟通按钮: {greet_status['greeted_btn_count']} 个")
    except Exception as e:
        print(f"  [扫描招呼按钮失败] {e}")

    try:
        inputs = frame.evaluate(SCAN_JS)
    except Exception as e:
        print(f"  [扫描输入框失败] {e}")
        return

    if not inputs:
        print("  → 该 frame 内未找到任何 textarea / contenteditable / input[type=text]")
        return

    visible_inputs = [i for i in inputs if i['visible']]
    hidden_inputs = [i for i in inputs if not i['visible']]
    print(f"  → 共 {len(inputs)} 个候选元素（可见 {len(visible_inputs)}，隐藏 {len(hidden_inputs)}）")

    for idx, inp in enumerate(visible_inputs):
        print(f"\n  [可见#{idx}] tag={inp['tag']} selector={inp['selector']}")
        print(f"    type={inp['type']!r} contentEditable={inp['contentEditable']!r}")
        print(f"    placeholder={inp['placeholder']!r}")
        print(f"    className={inp['className']!r}")
        print(f"    id={inp['id']!r} name={inp['name']!r}")
        print(f"    aria-label={inp['ariaLabel']!r} data-test={inp['dataTestId']!r}")
        print(f"    rect(x,y,w,h)=({inp['rect']['x']},{inp['rect']['y']},{inp['rect']['w']},{inp['rect']['h']})")
        print(f"    value/textContent length={inp['value_len']}")

    if hidden_inputs:
        print(f"\n  隐藏元素（{len(hidden_inputs)} 个，仅列 selector 与尺寸）：")
        for inp in hidden_inputs[:5]:
            print(f"    - {inp['tag']} {inp['selector']} placeholder={inp.get('placeholder','')!r} "
                  f"rect=({inp['rect']['x']},{inp['rect']['y']},{inp['rect']['w']},{inp['rect']['h']})")

    try:
        send_btns = frame.evaluate(SEND_BTN_JS)
    except Exception as e:
        print(f"  [扫描发送按钮失败] {e}")
        return
    if send_btns:
        print(f"\n  → 找到 {len(send_btns)} 个'发送'按钮：")
        for b in send_btns:
            print(f"    - {b['tag']} text={b['text']!r} class={b['className']!r} id={b['id']!r} "
                  f"visible={b['visible']} rect=({b['rect']['x']},{b['rect']['y']},{b['rect']['w']},{b['rect']['h']})")
    else:
        print("  → 未找到'发送'按钮")

    try:
        close_info = frame.evaluate(CLOSE_BTN_JS)
    except Exception as e:
        print(f"  [扫描关闭按钮失败] {e}")
        return
    close_btns = close_info.get('close_buttons', []) if close_info else []
    containers = close_info.get('chat_containers', []) if close_info else []
    visible_close = [c for c in close_btns if c.get('visible')]
    if visible_close:
        print(f"\n  → 找到 {len(visible_close)} 个可见关闭按钮：")
        for c in visible_close:
            print(f"    - source={c['source']} tag={c['tag']} text={c['text']!r} class={c['className']!r} "
                  f"id={c['id']!r} aria={c['ariaLabel']!r}")
            print(f"      rect=({c['rect']['x']},{c['rect']['y']},{c['rect']['w']},{c['rect']['h']})")
    else:
        print("  → 未找到可见的关闭按钮")
    if containers:
        print(f"\n  → 找到 {len(containers)} 个聊天窗口容器：")
        for c in containers[:5]:
            print(f"    - tag={c['tag']} class={c['className']!r} id={c['id']!r} children={c['childCount']}")
            print(f"      rect=({c['rect']['x']},{c['rect']['y']},{c['rect']['w']},{c['rect']['h']})")
    ancestors = close_info.get('input_ancestors', []) if close_info else []
    if ancestors:
        print(f"\n  → #boss-chat-global-input 的祖先链（前 10 层）：")
        for a in ancestors:
            print(f"    [depth={a['depth']}] tag={a['tag']} class={a['className']!r} id={a['id']!r}")
            print(f"           rect=({a['rect']['x']},{a['rect']['y']},{a['rect']['w']},{a['rect']['h']})")


def main() -> int:
    print(f"连接 CDP: {CDP_URL}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            contexts = browser.contexts
            if not contexts:
                print("✗ 浏览器无 context，可能 Edge 没开 CDP 或 BOSS 没启动")
                return 1
            ctx = contexts[0]
            pages = ctx.pages
            if not pages:
                print("✗ 浏览器无 page")
                return 1

            print(f"\n共 {len(pages)} 个 page：")
            recommend_page = None
            for i, page in enumerate(pages):
                url = page.url
                print(f"  [{i}] {url[:120]}")
                if RECOMMEND_URL_HINT in url:
                    recommend_page = page

            if not recommend_page:
                print(f"\n✗ 未找到推荐页（URL 含 '{RECOMMEND_URL_HINT}'）")
                print("  请先在 Edge 中打开 https://www.zhipin.com/web/chat/recommend 并登录")
                return 1

            print(f"\n使用推荐页: {recommend_page.url[:120]}")
            print(f"  page 共 {len(recommend_page.frames)} 个 frame：")
            for i, f in enumerate(recommend_page.frames):
                print(f"    [{i}] frame URL: {f.url[:100]}  (main={f == recommend_page.main_frame})")

            # 1. 扫描主 frame
            main_frame = recommend_page.main_frame
            scan_frame(main_frame, "main frame (推荐页主文档)")

            # 2. 扫描所有 iframe
            for i, f in enumerate(recommend_page.frames):
                if f is main_frame:
                    continue
                scan_frame(f, f"iframe#{i}")

            print("\n" + "=" * 60)
            print("诊断完成。")
            print("=" * 60)
            print("\n下一步判断：")
            print("1. 如果没有任何可见 textarea/contenteditable/input[type=text]")
            print("   → 聊天面板没打开。请先手动点一次'打招呼'按钮让聊天面板渲染，再重跑此脚本。")
            print("2. 如果输入框可见但 selector 不是 textarea / div[contenteditable='true']")
            print("   → _send_custom_message 的 selector 已失效，需要更新。")
            print("3. 如果输入框可见但 rect.w<=30 或 rect.h<=15")
            print("   → 尺寸过滤太严，输入框是折叠/最小化状态。")
            print("4. 如果输入框存在但没有'发送'按钮")
            print("   → 可能要用 Enter 键发送，或发送按钮选择器要更新。")
            print("5. 如果输入框在某个 iframe 里，请把 iframe URL 告诉我，")
            print("   我会看 _send_custom_message 的 search_targets 是否覆盖到。")
    except Exception as e:
        print(f"\n✗ 异常: {e}")
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
