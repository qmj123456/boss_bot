#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推荐牛人在线简历截图脚本（patchright UI 版本）

特性：
- 启动前读 download_state.json，跳过 status=success / limit_hit 的候选人
- 卡片预筛通过后点击卡片进入在线简历，逐屏截图全部信息
- 不调用 BOSS 简历详情接口，不解析或落盘在线简历正文
- 成功截图：调 store.save_resume()，写入截图路径与 _meta
- 本次新增简历落 runs/<run_id>/process/new_resumes.json
- 失败时区分：临时失败 → mark_failed；触发"上限" → mark_limit_hit 并立即停止
- "没有新候选人要下"时直接退出（不浪费调用）
"""
import sys
import os
import errno
import json
import re
import time
import random
import argparse
import atexit
import functools
import inspect
from io import BytesIO
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageStat

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
import fix_encoding  # noqa: E402
from output_manager import JobOutputManager
from job_resume_store import JobResumeStore, candidate_key
from analysis_result import (analysis_record, analyze_with_retries, error_detail,
                             is_completed_analysis, is_passed_analysis)
from boss_job_switcher import get_or_create_recommend_page, ensure_recommend_job_selected

from patchright.sync_api import sync_playwright
from human_interaction import human_move, human_scroll

try:
    from anti_detection import exit_if_risk_page
except Exception:  # 防风控模块不可用时保持原流程
    exit_if_risk_page = None

# 复用现有 DeepSeek 判断与固定话术发送实现，保持新旧流程判断口径一致。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'boss-hr-greet', 'scripts'))
from shared.analysis_artifact_lock import RunArtifactLock
from ai.resume_analyzer import ResumeAnalyzer  # noqa: E402
from boss_hr.config_manager import get_config  # noqa: E402
from auto_greet import _send_custom_message  # noqa: E402


def _performance_value(key, default):
    """Read one documented performance setting with a safe fallback."""
    return get_config().get(f'performance.{key}', default)


def _performance_float(key, default, *, minimum=0.0):
    try:
        return max(minimum, float(_performance_value(key, default)))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def _performance_int(key, default, *, minimum=1):
    try:
        return max(minimum, int(_performance_value(key, default)))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _performance_range(key, default, *, minimum=0.0, maximum=None):
    value = _performance_value(key, default)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        value = default
    try:
        low, high = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        low, high = float(default[0]), float(default[1])
    low, high = sorted((max(minimum, low), max(minimum, high)))
    if maximum is not None:
        low, high = min(low, maximum), min(high, maximum)
    return low, high


def _performance_random(key, default, *, minimum=0.0, maximum=None):
    low, high = _performance_range(
        key, default, minimum=minimum, maximum=maximum,
    )
    return random.uniform(low, high)


def _flush_json_file(file_obj):
    """Flush JSON durably where supported; Windows EINVAL is non-fatal."""
    file_obj.flush()
    try:
        os.fsync(file_obj.fileno())
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            raise


# 只通过页面上真实候选人卡片进入在线简历。encryptGeekId 是唯一身份；
# 姓名仅在页面没有 ID 属性时兜底，避免同名候选人点错。
OPEN_CARD_JS = r"""(params) => {
    const cards = Array.from(document.querySelectorAll('li.card-item'));
    const nameMatches = [];
    for (const card of cards) {
        let gid = card.getAttribute('data-geek')
            || card.getAttribute('data-geekid')
            || card.getAttribute('data-geek-id')
            || card.getAttribute('data-encrypt-geek-id') || '';
        if (!gid) {
            const tagged = card.querySelector(
                '[data-geek], [data-geekid], [data-geek-id], [data-encrypt-geek-id]');
            if (tagged) gid = tagged.getAttribute('data-geek')
                || tagged.getAttribute('data-geekid')
                || tagged.getAttribute('data-geek-id')
                || tagged.getAttribute('data-encrypt-geek-id') || '';
        }
        if (!gid) {
            for (const a of card.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/[?&](?:geekId|encryptGeekId|geek_id)=([^&]+)/);
                if (m) { gid = decodeURIComponent(m[1]); break; }
            }
        }
        const nameEl = card.querySelector('[class*="name"]') || card.querySelector('h3, h4');
        const cardName = nameEl ? (nameEl.textContent || '').trim() : '';
        const normalizedCardName = cardName.replace(/\s+/g, '')
            .replace(/(刚刚活跃|在线|今日活跃|3日内活跃|本周活跃|2周内活跃|本月活跃)$/, '');
        const normalizedTargetName = (params.name || '').replace(/\s+/g, '');
        if (normalizedTargetName && normalizedCardName === normalizedTargetName) {
            nameMatches.push({card, gid, cardName});
        }
        if (!(params.gid && gid === params.gid)) continue;

        card.scrollIntoView({block: 'center', behavior: 'instant'});
        const target = card.querySelector(
            '.card-inner, [class*="card-content"], a[href*="geek"], [class*="name"]'
        ) || card;
        const r = target.getBoundingClientRect();
        return {
            ok: true, gid: gid, name: cardName, matchBy: 'encrypt_geek_id',
            x: r.left + r.width * .5,
            y: r.top + r.height * .5,
        };
    }
    // BOSS 虚拟列表部分版本的 data-geek 与 geek/list.encryptGeekId 并非
    // 同一种 ID。只在当前 DOM 中姓名精确且唯一时回退，避免同名误点。
    if (nameMatches.length === 1) {
        const {card, gid, cardName} = nameMatches[0];
        card.scrollIntoView({block: 'center', behavior: 'instant'});
        const target = card.querySelector(
            '.card-inner, [class*="card-content"], a[href*="geek"], [class*="name"]'
        ) || card;
        const r = target.getBoundingClientRect();
        return {
            ok: true, gid: gid, name: cardName, matchBy: 'unique_exact_name',
            x: r.left + r.width * .5,
            y: r.top + r.height * .5,
        };
    }
    return {ok: false, error: '当前 DOM 未找到目标候选人卡片'};
}"""


# 逐卡事务由 recommend_list.py 在扫描当前 DOM 时给原卡片打瞬时 token。
# 这里仅点击该 token 指向的同一个 Element；若虚拟列表已把它卸载就立即失败，
# 由上层重扫当前 DOM 重试，绝不按 API 队列回顶寻找另一张卡片。
OPEN_LIVE_CARD_JS = r"""(token) => {
    const card = Array.from(document.querySelectorAll('li.card-item')).find(
        item => item.dataset.bossLiveCardToken === token
    );
    if (!card || !card.isConnected) {
        return {ok: false, error: '当前逐卡事务的原始 DOM 卡片已卸载'};
    }
    const target = card.querySelector(
        '.card-inner, [class*="card-content"], a[href*="geek"], [class*="name"]'
    ) || card;
    let rect = target.getBoundingClientRect();
    // 实时逐卡扫描传入的卡片本来就在当前可见区。不要每次都
    // scrollIntoView(center)，否则关闭简历后同卡重试会反复把列表向上拉。
    if (rect.top < 0 || rect.bottom > window.innerHeight) {
        card.scrollIntoView({block: 'nearest', behavior: 'instant'});
        rect = target.getBoundingClientRect();
    }
    if (rect.width <= 0 || rect.height <= 0) {
        return {ok: false, error: '当前逐卡事务的原始 DOM 卡片不可见'};
    }
    return {
        ok: true,
        matchBy: 'live_dom_token',
        x: rect.left + rect.width * .5,
        y: rect.top + rect.height * .5,
    };
}"""


INIT_SCROLL_CAPTURE_JS = r"""() => {
    // BOSS 推荐页把在线简历放在 iframe 内的 .resume-detail-wrap。
    // iframe 的 HTML/BODY 仍保留虚拟候选列表的巨大 scrollHeight，但 BODY
    // 已被 boss-layer__lock-scroll 锁住，不能把它误判为简历滚动器。
    const preferredSelectors = [
        '.resume-detail-wrap',
        '[class*="resume-detail-wrap"]',
        '[class*="resume-detail"] [class*="scroll"]',
        '[role="dialog"] [class*="scroll"]',
    ];
    let best = null;
    let staticBest = null;
    let staticBestScore = -1;
    for (const selector of preferredSelectors) {
        for (const el of document.querySelectorAll(selector)) {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            const range = Math.max(0, el.scrollHeight - el.clientHeight);
            const visible = rect.width >= 300 && rect.height >= 300
                && rect.bottom > 0 && rect.top < window.innerHeight
                && style.display !== 'none' && style.visibility !== 'hidden';
            if (!visible) continue;
            // Prefer a real scrolling node for long resumes.  Keep the largest
            // visible known resume container as a valid single-screen fallback:
            // short resumes legitimately have scrollHeight === clientHeight.
            if (range > 0) {
                best = el;
                break;
            }
            // A non-scrolling node is safe only when the whole node is inside
            // the viewport; otherwise a clipped long resume could be mistaken
            // for a complete one-screen resume.
            const fullyInViewport = rect.top >= -2
                && rect.bottom <= window.innerHeight + 2;
            const staticScore = rect.width * rect.height;
            if (fullyInViewport && staticScore > staticBestScore) {
                staticBest = el;
                staticBestScore = staticScore;
            }
        }
        if (best) break;
    }

    // 兼容 BOSS 后续改类名：仅在没有命中已知简历容器时，从可见的大型
    // overflow 容器中选择；明确排除 HTML/BODY 和被锁定的元素。
    if (!best) {
        const all = Array.from(document.querySelectorAll('main, section, div'));
        let bestScore = -1;
        for (const el of all) {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            const range = Math.max(0, el.scrollHeight - el.clientHeight);
            const locked = style.overflowY === 'hidden'
                || el.classList.contains('boss-layer__lock-scroll');
            const scrollable = ['auto', 'scroll', 'overlay'].includes(style.overflowY);
            if (locked || !scrollable || range <= 0
                    || rect.width < 300 || rect.height < 300) continue;
            const score = rect.width * rect.height + range;
            if (score > bestScore) { best = el; bestScore = score; }
        }
    }
    if (!best) best = staticBest;
    if (!best) {
        return {ok: false, error: '未找到可见的在线简历主内容区域'};
    }
    // 只记录并滚动简历自己的容器。原始位置必须保存，截图无论成功还是
    // 失败都由 CLEANUP_SCROLL_CAPTURE_JS 恢复，避免影响外层推荐页。
    window.__bossResumeScrollState = {
        element: best,
        originalTop: best.scrollTop,
    };
    window.__bossResumeScrollEl = best;
    best.scrollTo({top: 0, behavior: 'instant'});
    return {
        ok: true,
        height: best.scrollHeight,
        viewport: best.clientHeight,
        top: best.scrollTop,
        className: String(best.className || ''),
        singleScreen: best.scrollHeight <= best.clientHeight + 2,
    };
}"""

CLEANUP_SCROLL_CAPTURE_JS = r"""() => {
    const state = window.__bossResumeScrollState || {};
    const el = state.element || window.__bossResumeScrollEl;
    let restored = false;
    if (el && el.isConnected) {
        el.scrollTo({top: Number(state.originalTop || 0), behavior: 'instant'});
        restored = true;
    }
    delete window.__bossResumeScrollEl;
    delete window.__bossResumeScrollState;
    return {ok: true, restored};
}"""

CAPTURE_OUTER_PAGE_STATE_JS = r"""() => {
    const iframe = document.querySelector('iframe[src*="frame/recommend"]')
        || document.querySelector('iframe');
    const rect = iframe ? iframe.getBoundingClientRect() : null;
    return {
        url: location.href,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        iframe: rect ? {
            left: rect.left, top: rect.top, width: rect.width, height: rect.height
        } : null,
    };
}"""

RESTORE_OUTER_PAGE_STATE_JS = r"""(state) => {
    if (!state) return {ok: false};
    window.scrollTo({
        left: Number(state.scrollX || 0),
        top: Number(state.scrollY || 0),
        behavior: 'instant'
    });
    // BOSS 的推荐页和简历 iframe 都依赖 resize 重新计算宽度。截图后主动
    // 发事件，避免 iframe 重建期间把旧宽度保留下来形成右侧大块空白。
    window.dispatchEvent(new Event('resize'));
    return {
        ok: true,
        url: location.href,
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
    };
}"""

SCROLL_CAPTURE_JS = r"""(top) => {
    const el = window.__bossResumeScrollEl || document.scrollingElement;
    el.scrollTo({top: top, behavior: 'smooth'});
    return {height: el.scrollHeight, viewport: el.clientHeight, top: el.scrollTop};
}"""

READ_CAPTURE_METRICS_JS = r"""() => {
    const el = window.__bossResumeScrollEl || document.scrollingElement;
    return {height: el.scrollHeight, viewport: el.clientHeight, top: el.scrollTop};
}"""

GET_RESUME_CAPTURE_BOUNDS_JS = r"""() => {
    const scrollEl = window.__bossResumeScrollEl;
    if (!scrollEl) return {ok: false, error: '在线简历滚动器尚未初始化'};
    // resume-middle-wrap 正好覆盖“简历正文 + 右侧操作/经历概览”，不包含
    // 推荐页导航、顶部菜单与弹层外灰色遮罩，对应用户要求的第二种截图。
    const root = scrollEl.closest('.resume-middle-wrap')
        || scrollEl.closest('.resume-layout-wrap')
        || scrollEl.closest('.lib-standard-resume')
        || scrollEl;
    const r = root.getBoundingClientRect();
    if (r.width < 300 || r.height < 300) {
        return {ok: false, error: '在线简历弹层裁剪区域不可见'};
    }
    return {
        ok: true,
        x: Math.max(0, r.left),
        y: Math.max(0, r.top),
        width: Math.min(r.width, window.innerWidth - Math.max(0, r.left)),
        height: Math.min(r.height, window.innerHeight - Math.max(0, r.top)),
        contentWidth: Math.min(
            scrollEl.getBoundingClientRect().width,
            r.width
        ),
        className: String(root.className || ''),
    };
}"""


READ_VISIBLE_RESUME_JS = r"""() => {
    const selectors = [
        '.resume-detail-wrap', '.resume-middle-wrap', '.resume-layout-wrap',
        '.lib-standard-resume', '[role="dialog"][class*="resume"]',
        '[class*="resume-detail"]'
    ];
    const roots = Array.from(document.querySelectorAll(selectors.join(',')))
        .filter((root, index, all) => all.indexOf(root) === index)
        .filter(root => {
            const r = root.getBoundingClientRect();
            const style = getComputedStyle(root);
            return r.width >= 300 && r.height >= 300
                && r.bottom > 0 && r.top < window.innerHeight
                && style.display !== 'none' && style.visibility !== 'hidden';
        })
        .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
        });
    const root = roots[0];
    if (!root) return {open: false, name: ''};
    const rr = root.getBoundingClientRect();
    const preferred = root.querySelectorAll(
        '.resume-name, [class*="resume-name"], [class*="geek-name"], '
        + '[class*="user-name"], h1, h2, h3'
    );
    const ignored = new Set([
        '在线简历', '经历概览', '工作经历', '教育经历', '期望职位',
        '个人优势', '资格证书', '专业技能'
    ]);
    for (const node of preferred) {
        const r = node.getBoundingClientRect();
        const text = String(node.textContent || '').replace(/\s+/g, '').trim();
        if (r.width <= 0 || r.height <= 0 || r.top > rr.top + 220) continue;
        if (text.length >= 1 && text.length <= 20 && !ignored.has(text)) {
            return {open: true, name: text, className: String(root.className || '')};
        }
    }
    const lines = String(root.innerText || '').split(/\n+/)
        .map(value => value.replace(/\s+/g, '').trim()).filter(Boolean);
    const name = lines.find(text => text.length >= 1 && text.length <= 20
        && !ignored.has(text) && !/^(收藏|转发|举报|不合适|打招呼|继续沟通)$/.test(text)) || '';
    return {open: true, name, className: String(root.className || '')};
}"""


VERIFY_RESUME_IDENTITY_JS = r"""(expected) => {
    const normalize = value => String(value || '').replace(/\s+/g, '')
        .replace(/(刚刚活跃|在线|今日活跃|3日内活跃|本周活跃|2周内活跃|本月活跃)$/, '');
    const selectors = [
        '.resume-detail-wrap', '.resume-middle-wrap', '.resume-layout-wrap',
        '.lib-standard-resume', '[role="dialog"][class*="resume"]',
        '[class*="resume-detail"]'
    ];
    const roots = Array.from(document.querySelectorAll(selectors.join(',')))
        .filter((root, index, all) => all.indexOf(root) === index)
        .filter(root => {
            const r = root.getBoundingClientRect();
            const style = getComputedStyle(root);
            return r.width >= 300 && r.height >= 300
                && r.bottom > 0 && r.top < window.innerHeight
                && style.display !== 'none' && style.visibility !== 'hidden';
        })
        .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
        });
    if (!roots.length) {
        return {open: false, matched: false, actual: '', evidence: []};
    }
    const expectedName = normalize(expected && expected.name);
    const expectedGid = String(expected && expected.gid || '');
    const evidence = [];

    for (const root of roots) {
        if (expectedGid) {
            const gidNodes = root.querySelectorAll(
                '[data-geek], [data-geekid], [data-geek-id], '
                + '[data-encrypt-geek-id], a[href]'
            );
            for (const node of gidNodes) {
                const values = [
                    node.getAttribute('data-geek'), node.getAttribute('data-geekid'),
                    node.getAttribute('data-geek-id'),
                    node.getAttribute('data-encrypt-geek-id'), node.getAttribute('href')
                ].filter(Boolean);
                if (values.some(value => String(value).includes(expectedGid))) {
                    return {
                        open: true, matched: true, actual: expectedName,
                        matchBy: 'geek_id'
                    };
                }
            }
        }

        // 姓名在不同版本的 BOSS 详情中会位于不同容器/坐标。
        // 只搜索已知的准确姓名，不猜测“第一个短文本”，
        // 因此“帮我联系”等操作按钮绝不会被当成候选人。
        const rawText = String(root.innerText || '');
        const normalizedText = normalize(rawText);
        const lines = rawText.split(/\n+/).map(normalize).filter(Boolean);
        for (const line of lines) {
            if (evidence.length >= 12) break;
            if (line.length <= 80 && !evidence.includes(line)) evidence.push(line);
        }
        if (expectedName && normalizedText.includes(expectedName)) {
            return {
                open: true, matched: true, actual: expectedName,
                matchBy: 'resume_text'
            };
        }
    }
    return {open: true, matched: false, actual: '', evidence};
}"""


# BOSS v225 把完整简历渲染到 c-resume iframe 的 canvas 中，canvas 无姓名
# DOM 可供读取。此时用扫描阶段绑定在原卡片 Element 上的唯一 token
# 验证点击来源，并同时要求新简历弹层和 canvas iframe 真实可见。
VERIFY_LIVE_CARD_RESUME_JS = r"""(expected) => {
    const normalize = value => String(value || '').replace(/\s+/g, '')
        .replace(/(刚刚活跃|在线|今日活跃|3日内活跃|本周活跃|2周内活跃|本月活跃)$/, '');
    const visible = node => {
        if (!node) return false;
        const r = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
            && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const token = String(expected && expected.token || '');
    const expectedName = normalize(expected && expected.name);
    const card = Array.from(document.querySelectorAll('li.card-item')).find(
        item => item.dataset.bossLiveCardToken === token
    );
    const nameNode = card && (card.querySelector('.name')
        || card.querySelector('[class*="name"]') || card.querySelector('h3, h4'));
    const cardName = normalize(nameNode && nameNode.textContent);
    const detail = Array.from(document.querySelectorAll(
        '.resume-detail-wrap, .resume-middle-wrap, .resume-layout-wrap, '
        + '.lib-standard-resume, [role="dialog"][class*="resume"]'
    )).find(visible);
    const canvasFrame = Array.from(document.querySelectorAll(
        'iframe[src*="/frame/c-resume"], iframe[src*="c-resume"]'
    )).find(visible);
    const open = Boolean(detail && canvasFrame);
    return {
        open,
        matched: Boolean(open && card && card.isConnected
            && expectedName && cardName === expectedName),
        actual: cardName,
        matchBy: 'live_dom_token_canvas',
        evidence: [cardName].filter(Boolean),
    };
}"""


CLOSE_VISIBLE_RESUME_JS = r"""() => {
    const selectors = [
        '.resume-detail-wrap', '.resume-middle-wrap', '.resume-layout-wrap',
        '.lib-standard-resume', '[role="dialog"][class*="resume"]',
        '[class*="resume-detail"]'
    ];
    const roots = Array.from(document.querySelectorAll(selectors.join(',')))
        .filter((root, index, all) => all.indexOf(root) === index)
        .filter(root => {
            const r = root.getBoundingClientRect();
            const style = getComputedStyle(root);
            return r.width >= 300 && r.height >= 300
                && r.bottom > 0 && r.top < window.innerHeight
                && style.display !== 'none' && style.visibility !== 'hidden';
        });
    if (!roots.length) return {open: false, clicked: false};
    const root = roots[0];
    const rr = root.getBoundingClientRect();
    const candidates = Array.from(root.querySelectorAll(
        'button, [role="button"], [aria-label*="关闭"], [title*="关闭"], '
        + '[class*="close"], i, svg'
    )).filter(node => {
        const r = node.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0 || r.width > 100 || r.height > 100) return false;
        const label = `${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''} `
            + `${node.className && String(node.className) || ''} ${node.textContent || ''}`;
        const nearTopRight = r.top <= rr.top + 100 && r.right >= rr.right - 120;
        return nearTopRight || /关闭|close|×|✕/i.test(label);
    }).sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        const as = Math.abs(ar.right - rr.right) + Math.abs(ar.top - rr.top);
        const bs = Math.abs(br.right - rr.right) + Math.abs(br.top - rr.top);
        return as - bs;
    });
    if (candidates.length) {
        (candidates[0].closest('button, [role="button"], [class*="close"]')
            || candidates[0]).click();
        return {open: true, clicked: true, method: 'control'};
    }
    const x = Math.max(rr.left + 1, rr.right - 24);
    const y = Math.min(rr.bottom - 1, rr.top + 24);
    const target = document.elementFromPoint(x, y);
    if (target && root.contains(target)) {
        target.click();
        return {open: true, clicked: true, method: 'top_right'};
    }
    return {open: true, clicked: false};
}"""


def _visible_resume_info(context):
    try:
        value = context.evaluate(READ_VISIBLE_RESUME_JS) or {}
        return value if isinstance(value, dict) else {'open': False, 'name': ''}
    except Exception:
        return {'open': False, 'name': ''}


def _normalize_resume_name(value):
    text = ''.join(str(value or '').split())
    for suffix in ('刚刚活跃', '在线', '今日活跃', '3日内活跃',
                   '本周活跃', '2周内活跃', '本月活跃'):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return text


def _resume_names_match(actual, expected):
    actual = _normalize_resume_name(actual)
    expected = _normalize_resume_name(expected)
    if not actual or not expected:
        return False
    if actual == expected:
        return True
    # BOSS may mask one or more characters in a candidate name.
    if '*' in actual or '＊' in actual:
        pattern = ''.join(
            '.+' if char in ('*', '＊') else re.escape(char)
            for char in actual
        )
        return re.fullmatch(pattern, expected) is not None
    return False


def _verify_open_resume_identity(detail_context, expected_name, expected_geek_id='',
                                 live_card_token=''):
    deadline = time.monotonic() + 8.0
    last = {'open': False, 'matched': False, 'actual': '', 'evidence': []}
    while time.monotonic() < deadline:
        try:
            last = detail_context.evaluate(VERIFY_RESUME_IDENTITY_JS, {
                'name': expected_name,
                'gid': expected_geek_id,
            }) or last
        except Exception:
            last = {'open': False, 'matched': False, 'actual': '', 'evidence': []}
        if last.get('open') and last.get('matched'):
            break
        if live_card_token:
            try:
                provenance = detail_context.evaluate(VERIFY_LIVE_CARD_RESUME_JS, {
                    'name': expected_name,
                    'gid': expected_geek_id,
                    'token': live_card_token,
                }) or {}
            except Exception:
                provenance = {}
            if provenance.get('open') and provenance.get('matched'):
                return str(provenance.get('actual') or expected_name)
        time.sleep(0.25)
    if not last.get('open'):
        raise RuntimeError('点击卡片后未检测到在线简历详情，禁止继续')
    if not last.get('matched'):
        evidence = ', '.join(str(value) for value in (last.get('evidence') or [])[:8])
        raise RuntimeError(
            f'在线简历身份不一致：卡片={expected_name or "未知"}，'
            f'简历头部未找到同名文本；候选文本={evidence or "无"}；'
            '已禁止截图和打招呼'
        )
    return str(last.get('actual') or expected_name)


def _find_and_open_resume(frame, page, ctx, geek_id, name, iframe_box=None,
                          max_scroll_steps=36, reset_to_top=True,
                          live_card_token=None):
    """渐进滚动定位卡片并点击，返回（详情页, 是否新窗口, 原 URL）。"""
    original_url = page.url
    existing_pages = set(ctx.pages)
    click_result = None
    if reset_to_top:
        try:
            frame.evaluate('() => window.scrollTo({top: 0, behavior: "instant"})')
        except Exception:
            pass

    for _ in range(max_scroll_steps + 1):
        if live_card_token:
            click_result = frame.evaluate(OPEN_LIVE_CARD_JS, live_card_token)
        else:
            click_result = frame.evaluate(OPEN_CARD_JS, {'gid': geek_id, 'name': name})
        if click_result and click_result.get('ok'):
            # 用真实鼠标轨迹点击卡片，点击前留出阅读/确认停顿。
            offset_x = (iframe_box or {}).get('x', 0)
            offset_y = (iframe_box or {}).get('y', 0)
            human_move(page, (
                offset_x + float(click_result.get('x') or 0),
                offset_y + float(click_result.get('y') or 0),
            ))
            time.sleep(_performance_random(
                'browser.card_click_wait_seconds', (0.2, 0.5),
            ))
            if live_card_token:
                # 拟人移动期间页面可能发生微小布局变化，点击前重新从
                # 同一 token Element 取坐标，防止用旧坐标点到相邻卡片。
                click_result = frame.evaluate(OPEN_LIVE_CARD_JS, live_card_token)
                if not click_result or not click_result.get('ok'):
                    raise RuntimeError(
                        (click_result or {}).get(
                            'error', '点击前原始 DOM 卡片已失效'))
            page.mouse.click(
                offset_x + float(click_result.get('x') or 0),
                offset_y + float(click_result.get('y') or 0),
            )
            break
        if live_card_token:
            # token 已经唯一锁定原 Element，失效时禁止再滚动或按 ID/姓名搜索。
            break
        metrics = frame.evaluate("""() => ({
            top: window.scrollY,
            height: document.documentElement.scrollHeight,
            viewport: window.innerHeight
        })""")
        # 卡片定位也采用真实小幅滚轮，每次滚动后停留浏览。
        next_top = min(
            int(metrics.get('top', 0)) + max(int(metrics.get('viewport', 0) * .45), 320),
            int(metrics.get('height', 0)),
        )
        if next_top <= int(metrics.get('top', 0)):
            break
        human_scroll(page, iframe_box, min_delta=280, max_delta=520)
        time.sleep(_performance_random(
            'browser.card_search_scroll_wait_seconds', (0.8, 1.5),
        ))

    if not click_result or not click_result.get('ok'):
        raise RuntimeError((click_result or {}).get('error', '未找到候选人卡片'))

    # BOSS 可能新开 tab，也可能在当前页/iframe 内展示详情。
    deadline = time.time() + 6
    while time.time() < deadline:
        new_pages = [p for p in ctx.pages if p not in existing_pages]
        if new_pages:
            detail_page = new_pages[-1]
            try:
                detail_page.wait_for_load_state('domcontentloaded', timeout=15000)
            except Exception:
                pass
            return detail_page, True, original_url
        if page.url != original_url:
            return page, False, original_url

        # 在线简历正文通常渲染在点击卡片所在的 iframe 内；
        # 父页同时可能出现一个有尺寸、但不包含简历文本的弹层外壳。
        # 必须先选原 iframe，否则身份校验只能得到“候选文本=无”。
        frame_info = _visible_resume_info(frame)
        if frame_info.get('open'):
            return frame, False, original_url

        try:
            parent_identity = page.evaluate(VERIFY_RESUME_IDENTITY_JS, {
                'name': name, 'gid': geek_id,
            }) or {}
            # 父页只有直接找到当前候选人时才能被选中；
            # 单纯存在空外壳或“帮我联系”按钮都不算详情就绪。
            if parent_identity.get('open') and parent_identity.get('matched'):
                return page, False, original_url
        except Exception:
            pass
        time.sleep(0.2)
    # 同页抽屉/弹层：滚动上下文仍在推荐 iframe 中。
    return frame, False, original_url


def _validate_resume_screenshot_files(paths):
    """Return resolved paths after verifying every image has real content."""
    resolved = [str(Path(path).resolve()) for path in paths]
    if not resolved or any(
        not Path(path).is_file() or Path(path).stat().st_size <= 0
        for path in resolved
    ):
        raise RuntimeError('在线简历截图落盘校验失败')
    for path in resolved:
        with Image.open(path) as captured:
            original_width, original_height = captured.size
            gray = captured.convert('L')
            gray.thumbnail((900, 900))
            stat = ImageStat.Stat(gray)
            pixels = list(gray.getdata())
            non_white_ratio = sum(1 for value in pixels if value < 245) / max(len(pixels), 1)
            variance = float(stat.var[0]) if stat.var else 0.0
            # 缩略图只用来降低像素统计成本。超长简历缩放到 900px 高后
            # 宽度可能小于 300px，但原图仍是完整高清长图，不能因此
            # 误判为空白并在 DeepSeek 调用前中止。
            if (original_width < 300 or original_height < 300
                    or non_white_ratio < 0.002 or variance < 2.0):
                raise RuntimeError(
                    '在线简历截图疑似空白：'
                    f'{Path(path).name} size={original_width}x{original_height} '
                    f'content_ratio={non_white_ratio:.4f} variance={variance:.2f}'
                )
    return resolved


def _reusable_resume_screenshots(target_dir):
    """Reuse only captures whose manifest proves the resume reached its bottom."""
    target = Path(target_dir)
    manifest = target / 'capture_complete.json'
    candidates = []
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding='utf-8'))
            if value.get('capture_complete') is True and value.get('reached_bottom') is True:
                candidates = [target / str(name) for name in value.get('files', [])]
        except (OSError, json.JSONDecodeError, AttributeError):
            candidates = []
    # 旧版文件没有“已到达底部”的证明，宁可重新截图，也不能把可能不完整的
    # 长图直接发送给 AI。
    if not candidates:
        return []
    try:
        return _validate_resume_screenshot_files(candidates)
    except (OSError, ValueError, RuntimeError):
        return []


def _take_resume_screenshot(screenshot_page, clip, *, quality=85):
    """Capture with hardened Playwright options, retaining old-driver support."""
    try:
        return screenshot_page.screenshot(
            clip=clip,
            type='jpeg',
            quality=quality,
            timeout=30000,
            animations='disabled',
            caret='hide',
        )
    except TypeError as exc:
        # Some older Patchright/Playwright builds (and legacy adapters) do not
        # expose the newer screenshot options. Only signature incompatibility
        # falls back; a real screenshot TypeError must still fail loudly.
        if 'unexpected keyword argument' not in str(exc):
            raise
        return screenshot_page.screenshot(
            clip=clip, type='jpeg', quality=quality,
        )


def _is_detached_frame_error(error):
    text = str(error or '').lower()
    return any(marker in text for marker in (
        'frame was detached',
        'frame has been detached',
        'execution context was destroyed',
    ))


def _wait_capture_metrics_stable(scroll_context, *, minimum_delay,
                                 max_polls=None, poll_interval=None):
    """Wait for scroll geometry to settle instead of using a long blind sleep."""
    if max_polls is None:
        max_polls = _performance_int(
            'screenshot.settle_max_polls', 8, minimum=1,
        )
    if poll_interval is None:
        poll_interval = _performance_float(
            'screenshot.settle_poll_interval_seconds', 0.1,
        )
    time.sleep(max(0.0, float(minimum_delay)))
    previous = None
    stable_polls = 0
    latest = {}
    for _ in range(max(1, int(max_polls))):
        latest = scroll_context.evaluate(READ_CAPTURE_METRICS_JS, True) or {}
        signature = (
            int(latest.get('top') or 0),
            int(latest.get('height') or 0),
            int(latest.get('viewport') or 0),
        )
        if signature == previous:
            stable_polls += 1
            if stable_polls >= 2:
                return latest
        else:
            previous = signature
            stable_polls = 0
        time.sleep(max(0.0, float(poll_interval)))
    return latest


def _capture_all_resume_screens_impl(scroll_context, screenshot_page, target_dir,
                                     max_screenshots=None, iframe_box=None):
    """滚动在线简历并把弹层区域拼成长图，不截取外部推荐页。"""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    reusable = _reusable_resume_screenshots(target)
    if reusable:
        return reusable
    if max_screenshots is None:
        max_screenshots = _performance_int(
            'screenshot.max_screenshots', 40, minimum=1,
        )
    screenshot_attempts = _performance_int(
        'screenshot.total_attempts', 2, minimum=1,
    )
    retry_delay = _performance_float(
        'screenshot.retry_delay_seconds', 0.6,
    )
    jpeg_quality = _performance_int(
        'screenshot.jpeg_quality', 85, minimum=50,
    )
    jpeg_quality = min(jpeg_quality, 95)
    scroll_ratio_min, scroll_ratio_max = _performance_range(
        'screenshot.scroll_ratio', (0.68, 0.78),
        minimum=0.1, maximum=0.9,
    )
    # 保留最小阅读停留，同时用滚动几何稳定性替代较长的固定等待。
    time.sleep(_performance_random(
        'screenshot.initial_wait_seconds', (0.6, 1.0),
    ))
    metrics = scroll_context.evaluate(INIT_SCROLL_CAPTURE_JS) or {}
    if not metrics.get('ok'):
        raise RuntimeError(metrics.get('error') or '未找到在线简历滚动区域')
    bounds = scroll_context.evaluate(GET_RESUME_CAPTURE_BOUNDS_JS, 'bounds') or {}
    if not bounds.get('ok'):
        raise RuntimeError(bounds.get('error') or '未找到在线简历弹层裁剪区域')
    offset_x = float((iframe_box or {}).get('x', 0))
    offset_y = float((iframe_box or {}).get('y', 0))
    clip = {
        'x': offset_x + float(bounds.get('x') or 0),
        'y': offset_y + float(bounds.get('y') or 0),
        'width': float(bounds.get('width') or 0),
        'height': float(bounds.get('height') or 0),
    }
    if clip['width'] < 300 or clip['height'] < 300:
        raise RuntimeError(f'在线简历弹层裁剪尺寸异常: {clip}')
    viewport = max(int(metrics.get('viewport') or 0), 500)
    # scrollHeight/clientHeight/scrollTop may be rounded differently by the
    # browser (zoom and devicePixelRatio commonly leave a few CSS pixels).
    # Treat only a tiny edge strip as rounding tolerance; larger gaps still fail.
    coverage_tolerance = max(4, min(20, int(viewport * 0.02)))
    # 每次只滚动约半屏并保留较大重叠，模拟逐段阅读且避免信息落在截图边缘。
    segments = []
    last_top = -1
    covered_until = 0
    final_height = int(metrics.get('height') or viewport)

    reached_end = False
    for index in range(max_screenshots):
        requested_top = 0 if index == 0 else (
            last_top + max(
                int(viewport * random.uniform(scroll_ratio_min, scroll_ratio_max)),
                260,
            )
        )
        scroll_context.evaluate(SCROLL_CAPTURE_JS, requested_top)
        # 保留拟人阅读停留，并在位置/高度稳定后立即继续，不盲等上限。
        metrics = _wait_capture_metrics_stable(
            scroll_context,
            minimum_delay=_performance_random(
                'screenshot.segment_wait_seconds', (0.4, 0.8),
            ),
        )
        top = int(metrics.get('top') or 0)
        height = int(metrics.get('height') or viewport)
        current_viewport = max(int(metrics.get('viewport') or viewport), 1)
        if segments and requested_top > last_top + 2 and top <= last_top + 2:
            # 再等一次排除浏览器 smooth 动画尚未结束；仍不动则说明容器失效。
            time.sleep(0.8)
            metrics = scroll_context.evaluate(READ_CAPTURE_METRICS_JS, True) or {}
            top = int(metrics.get('top') or 0)
            height = int(metrics.get('height') or viewport)
            current_viewport = max(int(metrics.get('viewport') or viewport), 1)
            if top <= last_top + 2:
                if (
                    top + current_viewport >= height - coverage_tolerance
                    and covered_until >= height - coverage_tolerance
                ):
                    final_height = height
                    reached_end = True
                    break
                raise RuntimeError(
                    f'在线简历滚动失败：scrollTop 未变化（{last_top} -> {top}）'
                )
        if reached_end:
            break
        if top == last_top and segments:
            break
        # 相邻视口必须连续覆盖。即使未来把滚动比例配错，也会在写入完整标记
        # 前失败，绝不会生成一张中间有缺口的“完整简历”。
        if segments and top > covered_until + 4:
            raise RuntimeError(
                '在线简历截图覆盖存在缺口：'
                f'covered_until={covered_until}, next_top={top}'
            )
        image_bytes = None
        screenshot_error = None
        for screenshot_attempt in range(1, screenshot_attempts + 1):
            try:
                image_bytes = _take_resume_screenshot(
                    screenshot_page, clip, quality=jpeg_quality,
                )
                if image_bytes:
                    break
            except Exception as exc:
                screenshot_error = exc
                if _is_detached_frame_error(exc):
                    raise RuntimeError(
                        '在线简历 iframe 已重建，立即结束本次截图等待'
                    ) from exc
                if screenshot_attempt < screenshot_attempts:
                    time.sleep(retry_delay)
        if not image_bytes and screenshot_error is not None:
            raise RuntimeError(
                f'在线简历第 {index + 1} 段截图连续 '
                f'{screenshot_attempts} 次失败：{screenshot_error}'
            ) from screenshot_error
        if not image_bytes:
            raise RuntimeError(f'在线简历第 {index + 1} 段截图为空')
        with Image.open(BytesIO(image_bytes)) as image:
            segments.append((image.convert('RGB').copy(), top))
        last_top = top
        covered_until = max(covered_until, top + current_viewport)
        final_height = height
        if top + current_viewport >= height - 4:
            # BOSS 可能在滚到末屏后才懒加载后续经历。再次等到高度稳定；
            # 只有稳定后的最新高度也被覆盖，才允许声明“完整截图”。
            bottom_metrics = _wait_capture_metrics_stable(
                scroll_context,
                minimum_delay=_performance_float(
                    'screenshot.bottom_settle_seconds', 0.35,
                ),
            )
            final_height = int(bottom_metrics.get('height') or height)
            # bottom_metrics 只是测量，不能当作已截图覆盖范围；必须由刚才
            # 真正落盘的截图范围 covered_until 覆盖最新高度。
            if covered_until >= final_height - coverage_tolerance:
                reached_end = True
                break

    if not segments:
        raise RuntimeError('在线简历截图未生成')
    if not reached_end:
        raise RuntimeError(
            f'在线简历超过 {max_screenshots} 屏，为避免遗漏信息已放弃本候选人'
        )
    if (
        segments[0][1] > coverage_tolerance
        or covered_until < final_height - coverage_tolerance
    ):
        raise RuntimeError(
            '在线简历覆盖校验失败，禁止发送不完整截图：'
            f'first_top={segments[0][1]}, covered_until={covered_until}, '
            f'content_height={final_height}'
        )
    # 按真实 scrollTop 差值去掉重复区域并拼成长图；每段采集时已有大重叠，
    # 因而不会漏掉位于滚动边缘的文字。通常一份简历只生成一张长图。
    first_image = segments[0][0]
    long_width = max(image.width for image, _ in segments)
    scale_y = first_image.height / max(clip['height'], 1)
    content_width_ratio = min(
        1.0,
        float(bounds.get('contentWidth') or bounds.get('width') or 0)
        / max(float(bounds.get('width') or 1), 1),
    )
    content_width_px = max(1, round(first_image.width * content_width_ratio))
    append_heights = [first_image.height]
    for (_, current_top), (_, previous_top) in zip(segments[1:], segments[:-1]):
        delta_px = round(max(0, current_top - previous_top) * scale_y)
        append_heights.append(max(1, min(first_image.height, delta_px)))
    long_height = sum(append_heights)
    long_image = Image.new('RGB', (long_width, long_height), 'white')
    long_image.paste(first_image, (0, 0))
    paste_y = first_image.height
    for (image, _), append_height in zip(segments[1:], append_heights[1:]):
        # 后续截图与上一张有大幅重叠；仅追加左侧正文底部新增区域。
        # 右侧经历概览是固定栏，首屏已经完整保留，后续保持白底避免重复。
        unique_content = image.crop((
            0,
            image.height - append_height,
            content_width_px,
            image.height,
        ))
        long_image.paste(unique_content, (0, paste_y))
        unique_content.close()
        paste_y += append_height

    # DeepSeek 单图最长边限制为 8192px；极长简历才拆成多张长图，仍按顺序
    # 全部发送，且每张都只包含在线简历弹层。
    max_long_height = 7800
    paths = []
    part_count = (long_height + max_long_height - 1) // max_long_height
    for part_index in range(part_count):
        start_y = part_index * max_long_height
        end_y = min(long_height, start_y + max_long_height)
        part = long_image.crop((0, start_y, long_width, end_y))
        suffix = '' if part_count == 1 else f'_{part_index + 1:02d}'
        image_path = target / f'online_resume_long{suffix}.jpg'
        part.save(image_path, format='JPEG', quality=jpeg_quality, optimize=True)
        paths.append(str(image_path.resolve()))
    long_image.close()
    for image, _ in segments:
        image.close()

    # 关闭窗口前做一次全量落盘 + 像素内容确认。文件非零并不代表截图有效；
    # BOSS 弹层尚未渲染时也可能得到一张纯白 JPEG。
    paths = _validate_resume_screenshot_files(paths)
    manifest_path = target / 'capture_complete.json'
    manifest_tmp = target / f'.capture_complete.{os.getpid()}.tmp'
    with open(manifest_tmp, 'w', encoding='utf-8') as manifest_file:
        json.dump({
            'capture_complete': True,
            'reached_bottom': True,
            'files': [Path(path).name for path in paths],
            'segments': len(segments),
            'content_height': final_height,
            'covered_until': covered_until,
            'scroll_ratio': [scroll_ratio_min, scroll_ratio_max],
        }, manifest_file, ensure_ascii=False, indent=2)
        _flush_json_file(manifest_file)
    os.replace(manifest_tmp, manifest_path)
    return paths


def _capture_all_resume_screens(scroll_context, screenshot_page, target_dir,
                                max_screenshots=None, iframe_box=None):
    """Capture a resume without leaking scroll/layout state to the Edge page.

    ``scroll_context`` is normally the resume iframe while ``screenshot_page``
    is the outer BOSS page.  Keep their state separate: only the iframe is
    scrolled, and the outer page position/layout is restored in ``finally``.
    The cleanup deliberately runs for successful captures, validation errors,
    screenshot timeouts, and detached-frame failures alike.
    """
    outer_state = None
    try:
        outer_state = screenshot_page.evaluate(CAPTURE_OUTER_PAGE_STATE_JS)
    except Exception:
        # Test doubles and older drivers may expose screenshot() without
        # evaluate().  Capturing remains supported; iframe cleanup still runs.
        outer_state = None

    try:
        return _capture_all_resume_screens_impl(
            scroll_context,
            screenshot_page,
            target_dir,
            max_screenshots=max_screenshots,
            iframe_box=iframe_box,
        )
    finally:
        try:
            scroll_context.evaluate(CLEANUP_SCROLL_CAPTURE_JS)
        except Exception:
            # A detached iframe cannot be repaired through its stale Frame
            # object.  The outer-page restore below still runs independently.
            pass
        if outer_state is not None:
            try:
                screenshot_page.evaluate(
                    RESTORE_OUTER_PAGE_STATE_JS,
                    outer_state,
                )
            except Exception:
                pass


FIND_RESUME_ACTION_JS = r"""(labels) => {
    document.querySelectorAll('[data-boss-resume-action]').forEach(
        el => el.removeAttribute('data-boss-resume-action'));
    const roots = [
        ...document.querySelectorAll('.resume-middle-wrap, .resume-layout-wrap, .lib-standard-resume'),
        ...document.querySelectorAll('[role="dialog"], [class*="resume-detail"]')
    ];
    const scope = roots.find(root => {
        const r = root.getBoundingClientRect();
        return r.width > 300 && r.height > 300;
    }) || document;
    const nodes = scope.querySelectorAll('button, a, [role="button"], div, span');
    for (const el of nodes) {
        const text = (el.textContent || '').replace(/\s+/g, '');
        if (!labels.includes(text)) continue;
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (r.width < 50 || r.height < 20 || style.display === 'none'
                || style.visibility === 'hidden') continue;
        el.setAttribute('data-boss-resume-action', '1');
        return {selector: '[data-boss-resume-action="1"]', text};
    }
    return null;
}"""


def _greet_current_resume(page, detail_context, *, name, geek_id, message,
                          dry_run=False, iframe_box=None):
    """在仍打开的当前简历中完成招呼和固定话术，不回列表二次找人。"""
    if dry_run:
        return {
            'name': name, 'encrypt_geek_id': geek_id, 'found': True,
            'clicked': False, 'verified': True, 'status': 'dry_run',
            'message_sent': False, 'message': message,
        }
    if not message:
        raise RuntimeError('固定话术为空，禁止只打招呼不发后续消息')

    def click_resume_action(labels, *, wait_seconds=0):
        deadline = time.monotonic() + max(float(wait_seconds), 0.0)
        while True:
            action = detail_context.evaluate(FIND_RESUME_ACTION_JS, labels)
            if action and action.get('selector'):
                button = detail_context.query_selector(action['selector'])
                if button:
                    button.click(timeout=8000)
                    return action.get('text') or labels[0]
            if time.monotonic() >= deadline:
                return ''
            time.sleep(0.4)

    # 在线简历右上角只有两种有效状态：首次为“打招呼”，建立沟通后为
    # “继续沟通”。后者仍需点击，右下角聊天窗口才会真正挂载输入框。
    action_text = click_resume_action(['继续沟通', '继续聊'])
    greeted_now = False
    if not action_text:
        action_text = click_resume_action(['打招呼'])
        if not action_text:
            raise RuntimeError('当前在线简历内未找到“打招呼”按钮')
        greeted_now = True
        time.sleep(_performance_random(
            'browser.greeting_transition_wait_seconds', (0.8, 1.4),
        ))
        action_text = click_resume_action(['继续沟通', '继续聊'], wait_seconds=12)
        if not action_text:
            raise RuntimeError('已点击打招呼，但按钮未变为“继续沟通”')

    # “继续沟通”已点击；留出时间让右下角聊天框动画和编辑器完成挂载。
    time.sleep(_performance_random(
        'browser.chat_mount_wait_seconds', (0.5, 1.0),
    ))

    send_result = _send_custom_message(
        page, detail_context, message,
        name=name, encrypt_geek_id=geek_id, iframe_box=iframe_box,
        open_chat_if_needed=False,
    )
    if not send_result.get('sent'):
        raise RuntimeError(
            '已点击打招呼但固定话术发送失败：'
            f"{send_result.get('reason') or 'unknown'}"
        )
    return {
        'name': name, 'encrypt_geek_id': geek_id, 'found': True,
        'clicked': greeted_now, 'verified': True, 'status': 'greeted',
        'message_sent': True, 'message': message,
        'resume_action': action_text,
        'message_result': send_result,
    }


def _resume_context_for_greeting(page, detail_context, *, opened_new_page,
                                 name, geek_id, live_card_token=''):
    """Verify the open resume and recover one iframe rebuild without reopening it."""
    try:
        _verify_open_resume_identity(
            detail_context,
            name,
            expected_geek_id=geek_id,
            live_card_token=live_card_token,
        )
        return detail_context, None
    except Exception as original_error:
        if opened_new_page:
            raise RuntimeError(
                f'打招呼前在线简历已失效：{original_error}'
            ) from original_error

        iframe = (
            page.query_selector('iframe[src*="frame/recommend"]')
            or page.query_selector('iframe')
        )
        refreshed = iframe.content_frame() if iframe else None
        if not refreshed or refreshed is detail_context:
            raise RuntimeError(
                f'打招呼前在线简历 iframe 已失效：{original_error}'
            ) from original_error
        _verify_open_resume_identity(
            refreshed,
            name,
            expected_geek_id=geek_id,
            live_card_token=live_card_token,
        )
        return refreshed, iframe.bounding_box()


def _transaction_payloads(job_name, candidates, analysis_failures, greet_results,
                          run_id, dry_run):
    for rank, item in enumerate(candidates, 1):
        item['rank'] = rank
    screening = {
        'job': job_name,
        'transactional_flow': True,
        'summary': {
            'total': len(candidates),
            'passed': sum(1 for item in candidates if item.get('passed')),
            'rejected': sum(1 for item in candidates if not item.get('passed')),
            'failed': len(analysis_failures),
        },
        'candidates': candidates,
        'failures': analysis_failures,
    }
    greet = {
        'job': job_name,
        'run_id': run_id,
        'transactional_flow': True,
        'dry_run': dry_run,
        'status': 'complete',
        'summary': {
            'total': len(greet_results),
            'greeted': sum(1 for item in greet_results if item.get('status') == 'greeted'),
            'already_contacted': sum(1 for item in greet_results if item.get('status') == 'already_contacted'),
            'dry_run': sum(1 for item in greet_results if item.get('status') == 'dry_run'),
            'failed': sum(1 for item in greet_results if item.get('status') == 'failed'),
            'message_sent': sum(1 for item in greet_results if item.get('message_sent')),
        },
        'results': greet_results,
    }
    if greet['summary']['failed']:
        greet['status'] = 'partial_failed'
    return screening, greet


def _close_resume_legacy(detail_context, page, opened_new_page, original_url):
    """关闭当前在线简历，确保下一位仍从推荐页继续。"""
    if opened_new_page:
        detail_context.close()
        return
    if page.url != original_url:
        page.go_back(wait_until='domcontentloaded', timeout=15000)
        return
    # 同页弹层/iframe：只在详情/对话框容器内找关闭控件，避免误点页面其他按钮。
    try:
        detail_context.evaluate(r"""() => {
            const roots = document.querySelectorAll(
                '[role="dialog"], [class*="resume-detail"], [class*="resumeDialog"], [class*="geek-detail"]'
            );
            for (const root of roots) {
                const rr = root.getBoundingClientRect();
                if (rr.width <= 300 || rr.height <= 300) continue;
                const buttons = root.querySelectorAll(
                    'button[aria-label*="关闭"], button[title*="关闭"], [class*="close"]'
                );
                for (const button of buttons) {
                    const r = button.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) { button.click(); return true; }
                }
            }
            return false;
        }""")
    except Exception:
        pass
    try:
        page.keyboard.press('Escape')
    except Exception:
        pass


def _close_resume(detail_context, page, opened_new_page, original_url):
    """Close a resume and verify that its visible detail DOM disappeared."""
    if opened_new_page:
        detail_context.close()
        return True
    if page.url != original_url:
        page.go_back(wait_until='domcontentloaded', timeout=15000)
        return True
    for attempt in range(3):
        info = _visible_resume_info(detail_context)
        if not info.get('open'):
            return True
        try:
            detail_context.evaluate(CLOSE_VISIBLE_RESUME_JS)
        except Exception:
            pass
        time.sleep(0.35)
        if not _visible_resume_info(detail_context).get('open'):
            return True
        try:
            page.keyboard.press('Escape')
        except Exception:
            pass
        deadline = time.monotonic() + 2.0 + attempt
        while time.monotonic() < deadline:
            if not _visible_resume_info(detail_context).get('open'):
                return True
            time.sleep(0.2)
    remaining = _visible_resume_info(detail_context)
    raise RuntimeError(
        '在线简历详情关闭失败，禁止处理下一张卡片：'
        f'name={remaining.get("name") or "无法识别"}'
    )


def _ensure_no_resume_open(page, contexts):
    """Clear a leftover detail before another rendered card can be clicked."""
    for context in contexts:
        if context is None:
            continue
        info = _visible_resume_info(context)
        if info.get('open'):
            _close_resume(context, page, False, page.url)
    leftovers = []
    for context in contexts:
        if context is None:
            continue
        info = _visible_resume_info(context)
        if info.get('open'):
            leftovers.append(str(info.get('name') or '无法识别'))
    if leftovers:
        raise RuntimeError(
            f'上一份在线简历仍未关闭（{", ".join(leftovers)}），禁止点击下一张卡片'
        )
    return True


def _resume_record_from_card(g, screenshot_paths):
    """保留后续落库/报表所需卡片字段，正文信息只存在截图中。"""
    card = g.get('geekCard') or {}
    works = card.get('geekWorks') or []
    return {
        'ok': True,
        'source': 'online_resume_screenshot',
        'name': card.get('geekName') or '',
        'age': card.get('ageDesc') or '',
        'degree': card.get('degreeName') or card.get('degreeCategory') or card.get('geekDegree') or '',
        'work_years': card.get('workYearDesc') or card.get('geekWorkYear') or '',
        'city': card.get('expectLocationName') or '',
        'expect_city': card.get('expectLocationName') or '',
        'expect_position': card.get('expectPositionName') or '',
        'current_role': card.get('expectPositionName') or '',
        'expected_salary': _format_card_salary(card),
        'work_experience': [
            {
                'company': w.get('company') or '',
                'position': w.get('positionName') or '',
                'start': w.get('startDate') or '',
                'end': w.get('endDate') or '',
            }
            for w in works if isinstance(w, dict)
        ],
        'resume_screenshots': screenshot_paths,
    }


def _format_card_salary(card):
    low, high = card.get('lowSalary'), card.get('highSalary')
    if low is None and high is None:
        return ''
    if low is None:
        return str(high)
    if high is None:
        return str(low)
    return f'{low}-{high}'


def _read_json_array(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            value = json.load(f)
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _atomic_write_json(path, value):
    """逐人提交 run 产物；进程被终止时，已完成截图仍可恢复并送 AI。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        _flush_json_file(f)
    os.replace(temp_path, path)


def _job_requirement(job_name, override=None):
    config = get_config()
    section = dict(config.section('recruitment'))
    profile = config.load_job_profile(job_name) or {}
    section.update(profile)
    requirement = {
        'job_name': job_name or section.get('job_name', ''),
        'experience_required': section.get('experience_required', ''),
        'degree_required': section.get('degree_required', ''),
        'skill_requirements': section.get('skill_requirements', []),
        'additional_requirements': section.get('additional_requirements', ''),
        'card_prescreen': section.get('card_prescreen', {}),
        'resume_analysis': section.get('resume_analysis', {}),
        'stem_major_categories': section.get('stem_major_categories', {}),
    }
    if isinstance(override, dict):
        # 调用方覆盖岗位要求时仍保留外置岗位配置里的求职状态名单，避免
        # 逐卡链路因为一个不完整 override 静默绕过白名单/黑名单。
        for key, value in override.items():
            if key in ('card_prescreen', 'resume_analysis') and isinstance(value, dict):
                nested = dict(requirement.get(key) or {})
                nested.update(value)
                requirement[key] = nested
            else:
                requirement[key] = value
    return requirement


def _check_card_before_open(candidate, requirement):
    from boss_hr.domain.card_prescreen import evaluate_card_prescreen
    result = evaluate_card_prescreen(candidate, requirement)
    candidate['_card_prescreen'] = result.to_dict()
    return result


def _analysis_record(result, resume, geek_id, name):
    return analysis_record(result, resume, geek_id, name)


def _analyze_saved_resume(analyzer, requirement, resume, *, attempts=None, log=None):
    """Retry against the same validated screenshots without reopening BOSS."""
    resume['resume_screenshots'] = _validate_resume_screenshot_files(
        resume.get('resume_screenshots') or []
    )
    return analyze_with_retries(
        analyzer, requirement, resume,
        attempts=attempts if attempts is not None else _performance_int('ai.total_attempts', 2),
        retry_delay=_performance_float('ai.retry_delay_seconds', 0.8),
        retry_max_delay=_performance_float('ai.retry_max_delay_seconds', 8.0),
        log=log,
    )


def _candidate_result_id(item):
    if not isinstance(item, dict):
        return ''
    meta = item.get('_meta') or {}
    return str(
        item.get('encrypt_geek_id')
        or item.get('geek_id')
        or (meta.get('encrypt_geek_id') if isinstance(meta, dict) else '')
        or ''
    )


def _upsert_candidate_result(items, item):
    gid = _candidate_result_id(item)
    items[:] = [
        existing for existing in items
        if _candidate_result_id(existing) != gid
    ]
    items.append(item)


def _is_fatal_candidate_error(error):
    """Only page/session failures stop the whole batch; candidate failures do not."""
    error_text = str(error)
    fatal_keywords = (
        '验证码', '登录失效', '请登录', '浏览器已关闭', 'Target closed',
        '推荐牛人页未加载出候选人 iframe',
    )
    return any(keyword in error_text for keyword in fatal_keywords)


class LiveCandidateProcessor:
    """卡片预筛回调：在同一推荐页会话中立即完成一位候选人的完整流程。"""

    def __init__(self, *, job_name, encrypt_job_id, run_id, max_greetings=10,
                 dry_run=False, greet_message='', job_requirement=None):
        self.job_name = job_name
        self.job_id = encrypt_job_id
        self.run_id = run_id
        self.max_greetings = max_greetings
        self.dry_run = dry_run
        self.greet_message = greet_message
        self.output = JobOutputManager(
            job_name, encrypt_job_id=encrypt_job_id, run_id=run_id,
        )
        self.output.ensure_run_dir()
        self.store = JobResumeStore(job_name, encrypt_job_id=encrypt_job_id)
        self.analyzer = ResumeAnalyzer(config=get_config())
        self.requirement = _job_requirement(job_name, job_requirement)
        self.screening_path = self.output.get_process_path('screening_results.json')
        self.greet_log_path = self.output.get_process_path('greet_log.json')
        self.live_log_path = self.output.get_process_path('transaction_live.log')
        self._artifact_lock = RunArtifactLock(Path(self.output.run_dir) / 'process').acquire()
        atexit.register(self._artifact_lock.close)
        try:
            self.new_resumes = _read_json_array(self.output.new_resumes_path)
            self.failed = _read_json_array(self.output.failed_resumes_path)
            self.analysis_candidates = []
            self.analysis_failures = []
            self.greet_results = []
            self.processed_ids = set()
            for path, target, key in (
                (self.screening_path, self.analysis_candidates, 'candidates'),
                (self.screening_path, self.analysis_failures, 'failures'),
                (self.greet_log_path, self.greet_results, 'results'),
            ):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        value = json.load(f)
                    target.extend(value.get(key) or [])
                except (OSError, json.JSONDecodeError, AttributeError):
                    pass
            for item in self.analysis_candidates:
                gid = str(item.get('encrypt_geek_id') or item.get('geek_id') or '')
                if gid and is_completed_analysis(item) and (item['passed'] is False or any(
                    str(g.get('encrypt_geek_id') or '') == gid
                    and g.get('status') in ('greeted', 'already_contacted', 'dry_run')
                    for g in self.greet_results
                )):
                    self.processed_ids.add(gid)
            self._commit()
        except BaseException:
            self._artifact_lock.close()
            raise

    def close(self):
        guard = getattr(self, '_artifact_lock', None)
        if guard is not None:
            guard.close()

    def __del__(self):
        self.close()

    def _log(self, message):
        line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {message}'
        print(line, flush=True)
        with open(self.live_log_path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')

    @property
    def greeted_count(self):
        return sum(
            1 for item in self.greet_results
            if item.get('status') in ('greeted', 'dry_run')
        )

    def _ensure_artifact_ownership(self):
        guard = getattr(self, '_artifact_lock', None)
        if guard is not None and not guard.locked:
            raise RuntimeError('本轮处理器已经关闭，禁止覆盖分析产物；请重新创建处理器')

    def _commit(self):
        self._ensure_artifact_ownership()
        screening, greet = _transaction_payloads(
            self.job_name, self.analysis_candidates, self.analysis_failures,
            self.greet_results, self.run_id, self.dry_run,
        )
        _atomic_write_json(self.output.new_resumes_path, self.new_resumes)
        _atomic_write_json(self.output.failed_resumes_path, self.failed)
        _atomic_write_json(self.screening_path, screening)
        _atomic_write_json(self.greet_log_path, greet)

    def process(self, candidate, page, ctx):
        """处理一张通过预筛的卡片。

        `_live_dom_card` 表示调用方刚从当前 DOM 看见并锁定了这张卡片。
        此时只允许在当前 DOM 立即点击，禁止回顶或滚动二次搜索。
        """
        self._ensure_artifact_ownership()
        geek_id = str(candidate.get('encryptGeekId') or '')
        card = candidate.get('geekCard') or {}
        name = str(card.get('geekName') or '')
        live_dom_card = candidate.get('_live_dom_card') or {}
        if not geek_id or geek_id in self.processed_ids:
            return {'ok': True, 'skipped': True, 'stop': False}
        prescreen = _check_card_before_open(candidate, getattr(self, 'requirement', {}))
        if not prescreen.allow_full_resume:
            self.processed_ids.add(geek_id)
            self.failed.append({'name': name, 'encrypt_geek_id': geek_id,
                                'stage': 'card_prescreen', 'reason': '；'.join(prescreen.reasons)})
            self._commit()
            return {'ok': True, 'skipped': True, 'stop': False,
                    'terminal_status': 'card_prescreen_rejected'}
        if self.greeted_count >= self.max_greetings:
            return {'ok': True, 'stop': True, 'reason': 'greeting_quota_reached'}
        cached_analysis = next((item for item in self.analysis_candidates
                                if _candidate_result_id(item) == geek_id and is_completed_analysis(item)), None)
        status = self.store.get_status(self.job_id, geek_id)
        if status == 'limit_hit' or (status == 'success' and cached_analysis is None):
            self.processed_ids.add(geek_id)
            return {'ok': True, 'skipped': True, 'stop': False}

        detail_context = None
        opened_new_page = False
        original_url = page.url
        analysis_item = None
        try:
            iframe = page.query_selector('iframe[src*="frame/recommend"]') or page.query_selector('iframe')
            if not iframe or not iframe.content_frame():
                raise RuntimeError('推荐牛人页未加载出候选人 iframe')
            frame = iframe.content_frame()
            iframe_box = iframe.bounding_box()
            _ensure_no_resume_open(page, (frame, page))
            self._log(f'{name} 卡片预筛通过，立即打开在线简历')
            detail_context, opened_new_page, original_url = _find_and_open_resume(
                frame, page, ctx, geek_id, name,
                iframe_box=iframe_box,
                max_scroll_steps=0 if live_dom_card else 36,
                reset_to_top=False if live_dom_card else True,
                live_card_token=live_dom_card.get('token') or None,
            )
            actual_resume_name = _verify_open_resume_identity(
                detail_context, name, expected_geek_id=geek_id,
                live_card_token=live_dom_card.get('token') or '',
            )
            self._log(f'{name} 在线简历身份校验通过：detail={actual_resume_name}')
            screenshot_page = detail_context if opened_new_page else page
            if cached_analysis is not None:
                analysis_item = cached_analysis
                self._log(f'{name} 复用本轮已完成分析，仅恢复后续招呼')
            else:
                screenshot_dir = os.path.join(
                    self.output.run_dir, 'process', 'resume_screenshots',
                    candidate_key(self.job_id, geek_id).replace(':', '_'),
                )
                try:
                    screenshot_paths = _capture_all_resume_screens(
                        detail_context, screenshot_page, screenshot_dir,
                        iframe_box=iframe_box if not opened_new_page else None,
                    )
                except Exception as capture_error:
                    attempts = _performance_int(
                        'screenshot.total_attempts', 2, minimum=1,
                    )
                    failure = {
                        'name': name,
                        'encrypt_geek_id': geek_id,
                        'reason': str(capture_error)[:300],
                        'error': str(capture_error)[:300],
                        'stage': 'resume_capture',
                        'screenshot_attempts': attempts,
                        'terminal_status': 'screenshot_failed',
                        'failed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    }
                    _upsert_candidate_result(self.failed, failure)
                    self.store.mark_failed(
                        self.job_id, geek_id,
                        f'截图连续 {attempts} 次失败：{capture_error}',
                        self.run_id,
                    )
                    self.processed_ids.add(geek_id)
                    self._commit()
                    self._log(
                        f'{name} 截图连续 {attempts} 次失败；已记录终态，'
                        '不重开简历并继续下一张卡片：'
                        f'{str(capture_error)[:180]}'
                    )
                    return {
                        'ok': True,
                        'stop': False,
                        'terminal_status': 'screenshot_failed',
                    }
                resume = _resume_record_from_card(candidate, screenshot_paths)
                resume['_meta'] = {
                    'candidate_key': candidate_key(self.job_id, geek_id),
                    'encrypt_job_id': self.job_id,
                    'encrypt_geek_id': geek_id,
                    'captured_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'first_run_id': self.run_id,
                    'capture_method': 'online_resume_screenshot_live',
                }
                self.store.save_resume(resume, self.job_id, geek_id, self.run_id)
                _upsert_candidate_result(self.new_resumes, resume)
                self._commit()

                self._log(f'{name} 完整截图已校验（{len(screenshot_paths)} 张），立即发送 DeepSeek')
                analysis, last_error, analysis_attempts = _analyze_saved_resume(
                    self.analyzer,
                    self.requirement,
                    resume,
                    attempts=_performance_int('ai.total_attempts', 2, minimum=1),
                    log=lambda message: self._log(f'{name} {message}'),
                )
                if analysis is None:
                    resume['_meta'].update({
                        'deepseek_attempts': analysis_attempts,
                        'deepseek_error': str(last_error)[:300],
                        'deepseek_retry_mode': 'saved_screenshot_only',
                    })
                    _upsert_candidate_result(self.new_resumes, resume)
                    _upsert_candidate_result(self.analysis_failures, {
                        'geek_id': geek_id, 'name': name,
                        **error_detail(last_error),
                        'attempts': analysis_attempts,
                        'stage': 'deepseek_analysis',
                        'terminal_status': 'analysis_unavailable',
                        'failed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'retry_mode': 'saved_screenshot_only',
                    })
                    _upsert_candidate_result(self.failed, {
                        'name': name,
                        'encrypt_geek_id': geek_id,
                        'reason': f'DeepSeek 连续 {analysis_attempts} 次分析失败：{last_error}'[:300],
                        'error': str(last_error)[:300],
                        'stage': 'deepseek_analysis',
                        'analysis_attempts': analysis_attempts,
                        'terminal_status': 'analysis_unavailable',
                        'failed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    })
                    self.store.mark_failed(
                        self.job_id, geek_id,
                        f'DeepSeek 连续 {analysis_attempts} 次分析失败：{last_error}',
                        self.run_id,
                    )
                    self.processed_ids.add(geek_id)
                    self._commit()
                    self._log(
                        f'{name} DeepSeek 连续 {analysis_attempts} 次不可用；'
                        '已复用同一份截图且不再重开简历，关闭后继续下一张卡片'
                    )
                    return {
                        'ok': True,
                        'stop': False,
                        'terminal_status': 'analysis_unavailable',
                    }

                resume['_meta'].update({
                    'deepseek_sent_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'deepseek_screenshot_count': len(screenshot_paths),
                    'deepseek_passed': analysis['passed'],
                    'deepseek_attempts': analysis_attempts,
                    'deepseek_retry_mode': 'saved_screenshot_only',
                })
                _upsert_candidate_result(self.new_resumes, resume)
                analysis_item = _analysis_record(analysis, resume, geek_id, name)
                _upsert_candidate_result(self.analysis_candidates, analysis_item)
                self.analysis_failures[:] = [
                    item for item in self.analysis_failures
                    if str(item.get('geek_id') or '') != geek_id
                ]
                self._commit()

            if analysis_item['passed']:
                self._log(f'{name} DeepSeek 通过，立即打招呼并发送固定话术')
                detail_context, refreshed_box = _resume_context_for_greeting(
                    page,
                    detail_context,
                    opened_new_page=opened_new_page,
                    name=name,
                    geek_id=geek_id,
                    live_card_token=live_dom_card.get('token') or '',
                )
                if refreshed_box is not None:
                    iframe_box = refreshed_box
                greet_item = _greet_current_resume(
                    screenshot_page, detail_context,
                    name=name, geek_id=geek_id, message=self.greet_message,
                    dry_run=self.dry_run,
                    iframe_box=iframe_box if not opened_new_page else None,
                )
                _upsert_candidate_result(self.greet_results, greet_item)
                self._commit()
                self._log(
                    f'{name} 单人闭环完成：status={greet_item.get("status")} '
                    f'message_sent={bool(greet_item.get("message_sent"))}'
                )
            else:
                self._log(f'{name} DeepSeek 未通过，关闭简历后继续下一张卡片')

            if analysis_item['passed'] and greet_item.get('status') not in ('greeted', 'already_contacted', 'dry_run'):
                raise RuntimeError(greet_item.get('reason') or '招呼未完成')
            self.store.mark_success(self.job_id, geek_id, self.run_id)
            self.processed_ids.add(geek_id)
            self.failed[:] = [
                item for item in self.failed
                if str(item.get('encrypt_geek_id') or '') != geek_id
            ]
            self._commit()
            return {'ok': True, 'stop': self.greeted_count >= self.max_greetings}
        except Exception as exc:
            if (analysis_item and analysis_item.get('passed')
                    and not any(_candidate_result_id(g) == geek_id for g in self.greet_results)):
                _upsert_candidate_result(self.greet_results, {
                    'name': name, 'encrypt_geek_id': geek_id,
                    'found': True, 'clicked': False, 'verified': False,
                    'status': 'failed', 'message_sent': False,
                    'message': self.greet_message, 'reason': str(exc)[:300],
                })
            _upsert_candidate_result(self.failed, {
                'name': name, 'encrypt_geek_id': geek_id,
                'reason': str(exc)[:300],
            })
            self.store.mark_failed(self.job_id, geek_id, str(exc), self.run_id)
            self._commit()
            error_text = str(exc)
            if analysis_item and analysis_item.get('passed'):
                # Greeting may already have clicked "打招呼" before a chat-box
                # failure.  Reopening the whole card risks duplicate external
                # actions, so persist the failure and continue exactly once.
                self.processed_ids.add(geek_id)
                self._log(
                    f'{name} 在线简历内打招呼闭环失败；已记录且不重开该简历：{exc}'
                )
                return {
                    'ok': True,
                    'stop': False,
                    'terminal_status': 'greeting_failed',
                }
            must_stop = _is_fatal_candidate_error(error_text)
            if must_stop:
                action = '停止本轮'
            elif live_dom_card:
                action = '交由逐卡事务对原卡重试'
            else:
                action = '记录失败并继续下一张卡片'
            self._log(f'{name} 未完成，{action}：{exc}')
            return {'ok': False, 'stop': must_stop, 'reason': error_text}
        finally:
            if detail_context is not None:
                try:
                    _close_resume(detail_context, page, opened_new_page, original_url)
                    self._log(f'{name} 在线简历已关闭并确认详情DOM消失')
                    time.sleep(_performance_random(
                        'browser.close_wait_seconds', (0.2, 0.5),
                    ))
                except Exception as exc:
                    self._log(f'关闭 {name} 简历失败；下一张卡片前将强制重试：{exc}')

    def finalize(self):
        if hasattr(self, '_finalized_stats'):
            return self._finalized_stats
        try:
            self._commit()
            stats = {
                'downloaded': len(self.new_resumes),
                'failed': len(self.failed),
                'analyzed': len(self.analysis_candidates),
                'passed': sum(1 for item in self.analysis_candidates if item.get('passed')),
                'greeted': sum(1 for item in self.greet_results if item.get('status') == 'greeted'),
                'message_sent': sum(1 for item in self.greet_results if item.get('message_sent')),
            }
            self._finalized_stats = stats
            return stats
        finally:
            self.close()


def _guard_download_artifacts(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        bound = inspect.signature(function).bind(*args, **kwargs)
        run_id = bound.arguments.get('run_id')
        from output_manager import resolve_encrypt_job_id
        job_id = resolve_encrypt_job_id(bound.arguments.get('encrypt_job_id'))
        # Let the original entry retain its required-run/required-job errors.
        if not run_id or not job_id:
            return function(*args, **kwargs)
        output = JobOutputManager(bound.arguments.get('job_name'),
                                  encrypt_job_id=job_id, run_id=run_id, lazy=True)
        if not Path(output.run_dir).is_dir():
            raise FileNotFoundError(f'run 目录不存在：{output.run_dir}')
        with RunArtifactLock(Path(output.run_dir) / 'process'):
            return function(*args, **kwargs)
    return guarded


@_guard_download_artifacts
def download_resumes(job_name, batch_number=None, max_count=None,
                     pause_every=None, pause_min=None, pause_max=None,
                     run_id=None, from_pool=False, encrypt_job_id=None,
                     transactional=False, max_greetings=10, dry_run=False,
                     greet_message='', job_requirement=None):
    # 默认走 orchestrator，自动跟走上游创建的 run_id
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
    from run_orchestrator import RunOrchestrator
    from output_manager import resolve_encrypt_job_id
    encrypt_job_id = resolve_encrypt_job_id(encrypt_job_id)
    if not encrypt_job_id:
        raise ValueError("缺少 encrypt_job_id。\n  传 --encrypt-job-id，或设置 env BOSS_HR_ENCRYPT_JOB_ID")
    orch = RunOrchestrator(job_name, encrypt_job_id=encrypt_job_id)
    # 2026-07-30 重构：run_id 是数据边界，必须显式传（--run-id required=True）
    run_id = orch.bind_existing_run(run_id)
    if pause_every is None:
        pause_every = _performance_int(
            'legacy_throttling.pause_every', 10, minimum=0,
        )
    configured_pause_min, configured_pause_max = _performance_range(
        'legacy_throttling.pause_seconds', (8.0, 15.0),
    )
    if pause_min is None:
        pause_min = configured_pause_min
    if pause_max is None:
        pause_max = configured_pause_max
    pause_min, pause_max = sorted((float(pause_min), float(pause_max)))
    # 用户确认守卫（2026-07-30）：未确认直接 SystemExit(20)
    if not orch.is_confirmed(run_id):
        print(json.dumps({
            "status": "blocked",
            "exit_code": 20,
            "run_id": run_id,
            "message": (f"用户尚未确认，禁止执行 Step 2。"
                         "Step 1 完成后必须等用户在 BOSS 调整完筛选条件，"
                         "然后调 shared/confirm_run.py --run-id {run_id} "
                         "--encrypt-job-id {encrypt_job_id} --job-name {job_name} "
                         "把 run.json.confirmed 切到 true。"),
        }, ensure_ascii=False))
        raise SystemExit(20)

    output = JobOutputManager(job_name, encrypt_job_id=encrypt_job_id, run_id=run_id)
    output.ensure_run_dir()
    store = JobResumeStore(job_name, encrypt_job_id=encrypt_job_id)
    screening_path = output.get_process_path('screening_results.json')
    greet_log_path = output.get_process_path('greet_log.json')
    analysis_candidates = []
    analysis_failures = []
    greet_results = []
    analyzer = ResumeAnalyzer(config=get_config()) if transactional else None
    requirement = _job_requirement(job_name, job_requirement)
    if transactional:
        screening_existing = {}
        try:
            with open(screening_path, 'r', encoding='utf-8') as f:
                screening_existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        if isinstance(screening_existing, dict):
            analysis_candidates = list(screening_existing.get('candidates') or [])
            analysis_failures = list(screening_existing.get('failures') or [])
        greet_existing = {}
        try:
            with open(greet_log_path, 'r', encoding='utf-8') as f:
                greet_existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        if isinstance(greet_existing, dict):
            greet_results = list(greet_existing.get('results') or [])
        screening_payload, greet_payload = _transaction_payloads(
            job_name, analysis_candidates, analysis_failures, greet_results,
            run_id, dry_run,
        )
        _atomic_write_json(screening_path, screening_payload)
        _atomic_write_json(greet_log_path, greet_payload)

    # 异常护栏：脚本崩溃时若没写出 new_resumes.json，自动清空 run_dir
    import atexit
    _SAVED = False

    def _auto_prune():
        if not _SAVED and output.prune_if_empty():
            print(f'⚠️  本次 run 未产生 new_resumes.json，已清理: {output.run_dir}')

    atexit.register(_auto_prune)

    # 1) 选定输入：
    #    - from_pool=True：从 state/candidate_pool.json 取未尝试的（跨 run 通杀）
    #    - 否则：本次 run 的新增候选人（list 阶段刚写）
    if from_pool:
        pool = store.untried_geeks()
        # 截到 max_count
        if max_count:
            pool = pool[:max_count]
        # 写到系统临时目录（不污染 process/）
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="boss_dl_input_", suffix=".json")
        os.close(fd)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(pool, f, ensure_ascii=False)
        input_path = tmp
    elif batch_number:
        input_path = output.get_process_path(f'batch_{batch_number}_ids.json')
    else:
        input_path = output.recommend_geek_ids_path

    if not os.path.exists(input_path):
        print(f'错误：候选人列表文件不存在：{input_path}')
        _SAVED = True  # 保留 run 目录，方便排查 list 阶段为什么没有产物。
        return [], []

    with open(input_path, 'r', encoding='utf-8') as f:
        geek_list = json.load(f)

    if not geek_list:
        print('本次 run 候选人为空（无需下载）')
        new_resumes = _read_json_array(output.new_resumes_path)
        failed = _read_json_array(output.failed_resumes_path)
        if not os.path.exists(output.new_resumes_path):
            _atomic_write_json(output.new_resumes_path, new_resumes)
        if not os.path.exists(output.failed_resumes_path):
            _atomic_write_json(output.failed_resumes_path, failed)
        _SAVED = True
        return new_resumes, failed

    # 2) 当前 run 的岗位 ID 是唯一可信边界。候选卡片若来自其他岗位立即停止，
    # 禁止把旧岗位截图写进当前岗位目录。
    card_job_ids = {
        str((g.get('geekCard') or {}).get('encryptJobId') or '')
        for g in geek_list if isinstance(g, dict)
    } - {''}
    missing_job_id_count = sum(
        1
        for g in geek_list
        if isinstance(g, dict)
        and not str((g.get('geekCard') or {}).get('encryptJobId') or '')
    )
    if missing_job_id_count:
        raise RuntimeError(
            '候选列表存在缺少 encryptJobId 的记录：'
            f'count={missing_job_id_count} expected={encrypt_job_id}'
        )
    foreign_job_ids = sorted(jid for jid in card_job_ids if jid != encrypt_job_id)
    if foreign_job_ids:
        raise RuntimeError(
            '候选列表岗位与当前 run 不一致：'
            f'expected={encrypt_job_id}, actual={foreign_job_ids}'
        )
    job_id = encrypt_job_id

    # 3) 过滤：跳过已 success / limit_hit 的
    todo = []
    skipped = 0
    for g in geek_list:
        gid = g.get('encryptGeekId', '')
        if not gid:
            continue
        st = store.get_status(job_id, gid)
        cached_analysis = next((item for item in analysis_candidates
                                if _candidate_result_id(item) == gid and is_completed_analysis(item)), None)
        pending_greeting = transactional and cached_analysis is not None and cached_analysis['passed'] is True and not any(
            _candidate_result_id(item) == gid
            and item.get('status') in ('greeted', 'already_contacted', 'dry_run')
            for item in greet_results
        )
        if st == 'limit_hit' or (st == 'success' and not pending_greeting):
            skipped += 1
            continue
        todo.append(g)

    if skipped:
        print(f'⏭ 跳过已成功/触限的候选人 {skipped} 人（来自 state/download_state.json）')

    if not todo:
        print('✓ 本次 run 所有候选人都已成功下载或触限，无新增可下')
        new_resumes = _read_json_array(output.new_resumes_path)
        failed = _read_json_array(output.failed_resumes_path)
        if not os.path.exists(output.new_resumes_path):
            _atomic_write_json(output.new_resumes_path, new_resumes)
        if not os.path.exists(output.failed_resumes_path):
            _atomic_write_json(output.failed_resumes_path, failed)
        _SAVED = True
        return new_resumes, failed

    if batch_number:
        print(f'[分批模式] 第 {batch_number} 批')
    print(f'岗位：{job_name}')
    print(f'岗位 ID：{job_id}')
    print(f'候选人：{len(geek_list)} 人（待下载 {len(todo)}，已跳 {skipped}）')
    if max_count:
        print(f'最大下载：{max_count}')

    # 从本 run 的增量产物恢复。首次运行也立刻创建两个文件，确保任何中断都
    # 不会丢失已经完成的截图或失败原因。
    new_resumes = _read_json_array(output.new_resumes_path)
    failed = _read_json_array(output.failed_resumes_path)
    completed_ids = {
        str((item.get('_meta') or {}).get('encrypt_geek_id') or '')
        for item in new_resumes if isinstance(item, dict)
    } | {
        str(item.get('encrypt_geek_id') or '')
        for item in failed if isinstance(item, dict)
    }
    completed_ids.discard('')
    if completed_ids:
        if transactional:
            greeted_ids = {
                str(item.get('encrypt_geek_id') or '')
                for item in greet_results
                if item.get('status') in ('greeted', 'already_contacted', 'dry_run')
            }
            terminal_ids = {
                str(item.get('encrypt_geek_id') or item.get('geek_id') or '')
                for item in analysis_candidates
                if is_completed_analysis(item) and (
                    item['passed'] is False
                    or _candidate_result_id(item) in greeted_ids
                )
            }
            terminal_ids.discard('')
            todo = [g for g in todo if g.get('encryptGeekId', '') not in terminal_ids]
            print(f'↻ 从本 run 逐人事务恢复，已完整闭环 {len(terminal_ids)} 人')
        else:
            todo = [g for g in todo if g.get('encryptGeekId', '') not in completed_ids]
            print(f'↻ 从本 run 增量产物恢复，已处理 {len(completed_ids)} 人')
    _atomic_write_json(output.new_resumes_path, new_resumes)
    _atomic_write_json(output.failed_resumes_path, failed)
    _SAVED = True
    if not todo:
        return new_resumes, failed
    start_time = datetime.now()
    print(f'\n开始时间：{start_time.strftime("%H:%M:%S")}\n')

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = get_or_create_recommend_page(ctx, log=print)

        if 'zhipin.com' not in page.url:
            print('导航到 BOSS 直聘...')
            page.goto('https://www.zhipin.com/web/chat/recommend',
                       wait_until='domcontentloaded', timeout=20000)
            time.sleep(3)

        switch = ensure_recommend_job_selected(
            page,
            encrypt_job_id=encrypt_job_id,
            job_name=job_name,
            log=print,
        )
        if not switch.get('ok'):
            raise RuntimeError(
                '截图前无法确认 BOSS 推荐页岗位：'
                f"{switch.get('reason')} expected={encrypt_job_id} "
                f"actual={switch.get('current_jobid') or '未知'}"
            )

        iframe = page.query_selector('iframe[src*="frame/recommend"]') or page.query_selector('iframe')
        if not iframe or not iframe.content_frame():
            raise RuntimeError('推荐牛人页未加载出候选人 iframe')
        frame = iframe.content_frame()
        iframe_box = iframe.bounding_box()

        reset_card_search = True
        transaction_blocked = False
        for i, g in enumerate(todo):
            if exit_if_risk_page:
                exit_if_risk_page(page)
            completed_greetings = sum(
                1 for item in greet_results
                if item.get('status') in ('greeted', 'dry_run')
            )
            if transactional and completed_greetings >= max_greetings:
                print(f'✓ 已完成本轮招呼上限 {max_greetings} 人，停止打开更多简历')
                break
            if max_count and not transactional and len(new_resumes) + len(failed) >= max_count:
                break

            geek_id = g.get('encryptGeekId', '')
            gcard = g.get('geekCard', {})
            name = gcard.get('geekName', '')

            if not geek_id:
                failed.append({'name': name or 'Unknown', 'reason': '缺少 ID', 'encrypt_geek_id': ''})
                continue

            prescreen = _check_card_before_open(g, requirement)
            if not prescreen.allow_full_resume:
                failed.append({'name': name, 'encrypt_geek_id': geek_id,
                               'stage': 'card_prescreen', 'reason': '；'.join(prescreen.reasons)})
                _atomic_write_json(output.failed_resumes_path, failed)
                continue

            elapsed = (datetime.now() - start_time).total_seconds() / 60
            print(f'[{elapsed:.1f}分钟] #{i + 1}: {name}...', end=' ', flush=True)

            detail_context = None
            opened_new_page = False
            original_url = page.url
            card_opened = False
            try:
                detail_context, opened_new_page, original_url = _find_and_open_resume(
                    frame, page, ctx, geek_id, name, iframe_box=iframe_box,
                    reset_to_top=reset_card_search,
                )
                card_opened = True
                # 列表顺序与 API 候选顺序一致；下一位从当前位置继续，避免每人
                # 都回到顶部重复执行慢速扫描。
                reset_card_search = False
                # 新 tab 用自身滚动上下文；同页详情在候选 iframe 中滚动。
                screenshot_page = detail_context if opened_new_page else page
                screenshot_dir = os.path.join(
                    output.run_dir, 'process', 'resume_screenshots',
                    candidate_key(job_id, geek_id).replace(':', '_'),
                )
                try:
                    screenshot_paths = _capture_all_resume_screens(
                        detail_context, screenshot_page, screenshot_dir,
                        iframe_box=iframe_box if not opened_new_page else None,
                    )
                except Exception as capture_error:
                    attempts = _performance_int(
                        'screenshot.total_attempts', 2, minimum=1,
                    )
                    failed.append({
                        'name': name,
                        'encrypt_geek_id': geek_id,
                        'reason': str(capture_error)[:300],
                        'error': str(capture_error)[:300],
                        'stage': 'resume_capture',
                        'screenshot_attempts': attempts,
                        'terminal_status': 'screenshot_failed',
                        'failed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    })
                    completed_ids.add(geek_id)
                    _atomic_write_json(output.failed_resumes_path, failed)
                    store.mark_failed(
                        job_id, geek_id,
                        f'截图连续 {attempts} 次失败：{capture_error}',
                        run_id,
                    )
                    print(
                        f'截图连续 {attempts} 次失败，已记录并继续下一人：'
                        f'{str(capture_error)[:120]}'
                    )
                    continue
                print(f'截图已全部落盘并校验（{len(screenshot_paths)} 张），准备关闭在线简历...',
                      end=' ', flush=True)
                result = _resume_record_from_card(g, screenshot_paths)
                result['_meta'] = {
                    'candidate_key': candidate_key(job_id, geek_id),
                    'encrypt_job_id': job_id,
                    'encrypt_geek_id': geek_id,
                    'captured_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'first_run_id': run_id,
                    'capture_method': 'online_resume_screenshot',
                }

                is_new_cumulative = store.save_resume(result, job_id, geek_id, run_id)
                if transactional:
                    _upsert_candidate_result(new_resumes, result)
                    completed_ids.add(geek_id)
                    _atomic_write_json(output.new_resumes_path, new_resumes)
                elif geek_id not in completed_ids:
                    new_resumes.append(result)
                    completed_ids.add(geek_id)
                    _atomic_write_json(output.new_resumes_path, new_resumes)
                if transactional:
                    print('截图有效，立即发送 DeepSeek 分析...', end=' ', flush=True)
                    cached_analysis = next((item for item in analysis_candidates
                                            if _candidate_result_id(item) == geek_id and is_completed_analysis(item)), None)
                    if cached_analysis is not None:
                        analysis, last_analysis_error, analysis_attempts = cached_analysis, None, 0
                    else:
                        analysis, last_analysis_error, analysis_attempts = _analyze_saved_resume(
                            analyzer,
                            requirement,
                            result,
                            attempts=_performance_int('ai.total_attempts', 2, minimum=1),
                            log=lambda message: print(message, end=' ', flush=True),
                        )
                    if analysis is None:
                        result['_meta']['deepseek_attempts'] = analysis_attempts
                        result['_meta']['deepseek_error'] = str(last_analysis_error)[:300]
                        result['_meta']['deepseek_retry_mode'] = 'saved_screenshot_only'
                        _upsert_candidate_result(new_resumes, result)
                        _atomic_write_json(output.new_resumes_path, new_resumes)
                        failure = {
                            'geek_id': geek_id, 'name': name,
                            **error_detail(last_analysis_error),
                            'attempts': analysis_attempts,
                            'stage': 'deepseek_analysis',
                            'terminal_status': 'analysis_unavailable',
                            'failed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'retry_mode': 'saved_screenshot_only',
                        }
                        _upsert_candidate_result(analysis_failures, failure)
                        screening_payload, greet_payload = _transaction_payloads(
                            job_name, analysis_candidates, analysis_failures, greet_results,
                            run_id, dry_run,
                        )
                        _atomic_write_json(screening_path, screening_payload)
                        failed.append({
                            'name': name,
                            'encrypt_geek_id': geek_id,
                            'reason': (
                                f'DeepSeek 连续 {analysis_attempts} 次分析失败：'
                                f'{last_analysis_error}'
                            )[:300],
                            'error': str(last_analysis_error)[:300],
                            'stage': 'deepseek_analysis',
                            'analysis_attempts': analysis_attempts,
                            'terminal_status': 'analysis_unavailable',
                            'failed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        })
                        _atomic_write_json(output.failed_resumes_path, failed)
                        store.mark_failed(
                            job_id, geek_id,
                            f'DeepSeek 连续 {analysis_attempts} 次分析失败：{last_analysis_error}',
                            run_id,
                        )
                        print('AI 最终不可用；不重开简历，关闭后继续下一张卡片')
                        continue

                    result['_meta'].update({
                        'deepseek_sent_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'deepseek_screenshot_count': len(screenshot_paths),
                        'deepseek_passed': analysis['passed'],
                        'deepseek_attempts': analysis_attempts,
                        'deepseek_retry_mode': 'saved_screenshot_only',
                    })
                    _upsert_candidate_result(new_resumes, result)
                    _atomic_write_json(output.new_resumes_path, new_resumes)
                    analysis_item = _analysis_record(analysis, result, geek_id, name)
                    _upsert_candidate_result(analysis_candidates, analysis_item)
                    analysis_failures[:] = [
                        item for item in analysis_failures
                        if str(item.get('geek_id') or '') != geek_id
                    ]
                    screening_payload, greet_payload = _transaction_payloads(
                        job_name, analysis_candidates, analysis_failures, greet_results,
                        run_id, dry_run,
                    )
                    _atomic_write_json(screening_path, screening_payload)
                    if analysis_item['passed']:
                        print('AI 通过，立即打招呼并发送固定话术...', end=' ', flush=True)
                        try:
                            detail_context, refreshed_box = _resume_context_for_greeting(
                                page,
                                detail_context,
                                opened_new_page=opened_new_page,
                                name=name,
                                geek_id=geek_id,
                            )
                            if refreshed_box is not None:
                                iframe_box = refreshed_box
                            greet_item = _greet_current_resume(
                                screenshot_page, detail_context,
                                name=name, geek_id=geek_id, message=greet_message,
                                dry_run=dry_run,
                                iframe_box=iframe_box if not opened_new_page else None,
                            )
                        except Exception as greet_exc:
                            greet_item = {
                                'name': name, 'encrypt_geek_id': geek_id,
                                'found': True, 'clicked': False, 'verified': False,
                                'status': 'failed', 'message_sent': False,
                                'message': greet_message, 'reason': str(greet_exc)[:300],
                            }
                        _upsert_candidate_result(greet_results, greet_item)
                        screening_payload, greet_payload = _transaction_payloads(
                            job_name, analysis_candidates, analysis_failures, greet_results,
                            run_id, dry_run,
                        )
                        _atomic_write_json(greet_log_path, greet_payload)
                        if greet_item.get('status') not in ('greeted', 'already_contacted', 'dry_run'):
                            transaction_blocked = True
                            raise RuntimeError(greet_item.get('reason') or '招呼失败')
                        print(f"{greet_item.get('status')} ", end='', flush=True)
                    else:
                        print('AI 未通过，不打招呼 ', end='', flush=True)

                # 严格单人事务：截图、AI、必要的招呼和固定话术都落盘后，
                # 才把候选人标记成功并由 finally 关闭当前简历。
                store.mark_success(job_id, geek_id, run_id)
                if transactional:
                    failed[:] = [item for item in failed if _candidate_result_id(item) != geek_id]
                if is_new_cumulative:
                    print(f'OK（{len(screenshot_paths)} 张截图）')
                else:
                    print('OK（累计库已存在，本 run 截图已记录）')
            except Exception as e:
                if transactional:
                    transaction_blocked = True
                print(f'异常：{str(e)[:50]}')
                if transactional or geek_id not in completed_ids:
                    _upsert_candidate_result(failed, {
                        'name': name, 'encrypt_geek_id': geek_id, 'reason': str(e)[:80],
                    })
                    completed_ids.add(geek_id)
                    _atomic_write_json(output.failed_resumes_path, failed)
                store.mark_failed(job_id, geek_id, str(e), run_id)
                if not card_opened:
                    reset_card_search = True
            finally:
                if detail_context is not None:
                    try:
                        _close_resume(detail_context, page, opened_new_page, original_url)
                        time.sleep(_performance_random(
                            'browser.close_wait_seconds', (0.2, 0.5),
                        ))
                    except Exception as close_exc:
                        print(f'关闭在线简历失败（继续下一位）：{str(close_exc)[:50]}')
                # BOSS 偶尔会重建推荐 iframe，每位候选人后重新取一次。
                try:
                    iframe = page.query_selector('iframe[src*="frame/recommend"]') or page.query_selector('iframe')
                    if iframe and iframe.content_frame():
                        frame = iframe.content_frame()
                        iframe_box = iframe.bounding_box()
                except Exception:
                    pass

            if transaction_blocked:
                print('⛔ 当前候选人的逐人闭环未完成，停止处理后续候选人，等待下轮重试')
                break

            if i < len(todo) - 1:
                time.sleep(_performance_random(
                    'legacy_throttling.candidate_wait_seconds', (1.0, 2.0),
                ))

            processed = i + 1
            if pause_every and processed % pause_every == 0 and i < len(todo) - 1:
                wait = random.uniform(pause_min, pause_max)
                print(f'  ⏸ 已处理 {processed} 份，节流等待 {wait:.0f} 秒...')
                time.sleep(wait)

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds() / 60

    print(f'\n{"=" * 40}')
    print(f'结束时间：{end_time.strftime("%H:%M:%S")}')
    print(f'总耗时：{duration:.1f} 分钟')
    print(f'本次新增：{len(new_resumes)} 份在线简历截图')
    print(f'本次失败：{len(failed)} 份')
    print(f'累计下载：{store.count_resumes()} 份')
    if transactional:
        screening_payload, greet_payload = _transaction_payloads(
            job_name, analysis_candidates, analysis_failures, greet_results,
            run_id, dry_run,
        )
        _atomic_write_json(screening_path, screening_payload)
        _atomic_write_json(greet_log_path, greet_payload)
        print(
            '逐人闭环：'
            f"AI={screening_payload['summary']['total']}，"
            f"通过={screening_payload['summary']['passed']}，"
            f"招呼={greet_payload['summary']['greeted']}，"
            f"话术发送={greet_payload['summary']['message_sent']}"
        )

    # 保存本次 run 的新增简历
    _atomic_write_json(output.new_resumes_path, new_resumes)
    print(f'本次新增 → {output.new_resumes_path}')

    # 失败列表
    _atomic_write_json(output.failed_resumes_path, failed)
    print(f'失败列表 → {output.failed_resumes_path}')

    _SAVED = True  # 成功落盘，保留 run
    orch.mark_done('download', run_id=run_id)  # 标记 download 步骤完成
    return new_resumes, failed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='推荐牛人在线简历截图（去重 + 三态）')
    parser.add_argument('--job-name', default='线控底盘制动、转向工程师')
    parser.add_argument('--encrypt-job-id', default=None,
                        help='BOSS encryptJobId（推荐；新设计目录名依此定位；亦可走 env BOSS_HR_ENCRYPT_JOB_ID）')
    parser.add_argument('--batch', type=int, default=None)
    parser.add_argument('--max', type=int, default=None)
    parser.add_argument('--pause-every', type=int, default=None,
                        help='覆盖 performance.yaml 的旧入口节流频率')
    parser.add_argument('--pause-min', type=float, default=None,
                        help='覆盖 performance.yaml 的旧入口最短节流秒数')
    parser.add_argument('--pause-max', type=float, default=None,
                        help='覆盖 performance.yaml 的旧入口最长节流秒数')
    parser.add_argument('--run-id', required=True,
                        help='【必填】run_id 是数据边界。新任务先跑 boss_jd.py 创建 run；不传直接报错。')
    parser.add_argument('--from-pool', action='store_true',
                        help='从 state/candidate_pool.json 取未尝试的（跨 run 通杀）')
    parser.add_argument('--transactional', action='store_true',
                        help='逐人执行截图→DeepSeek→招呼→固定话术闭环')
    parser.add_argument('--max-greetings', type=int, default=10)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--greet-message', default='')
    parser.add_argument('--job-requirement-json', default='')

    args = parser.parse_args()
    requirement_override = None
    if args.job_requirement_json:
        requirement_override = json.loads(args.job_requirement_json)
    download_resumes(args.job_name, args.batch, args.max,
                     pause_every=args.pause_every,
                     pause_min=args.pause_min,
                     pause_max=args.pause_max,
                     run_id=args.run_id,
                     from_pool=args.from_pool,
                     encrypt_job_id=args.encrypt_job_id,
                     transactional=args.transactional,
                     max_greetings=args.max_greetings,
                     dry_run=args.dry_run,
                     greet_message=args.greet_message,
                     job_requirement=requirement_override)
