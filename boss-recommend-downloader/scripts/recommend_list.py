#!/usr/bin/env python3
"""
推荐牛人候选人列表获取脚本（支持分批）
Step 1: 通过浏览器滚动拦截 API 获取候选人 ID 列表

分批模式：
  --batch-size 25 --batch 1  收集前25人，保存batch_1_ids.json
  --batch-size 25 --batch 2  继续滚动，收集下25人，保存batch_2_ids.json
  ...
  不刷新页面，顺序固定。batch 1 会打开页面，batch 2+ 连接已有页面继续滚动。

使用统一输出路径：data/runs/<encryptJobId>/runs/<run_id>/process/
"""

import sys
import os

# 添加 shared 目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
import fix_encoding  # noqa: E402  # 强制 Windows UTF-8 stdout
from output_manager import JobOutputManager
from run_orchestrator import RunOrchestrator
from boss_job_switcher import ensure_recommend_job_selected, get_or_create_recommend_page, apply_job_filters
from boss_hr.config_manager import get_config
from boss_hr.domain.card_prescreen import evaluate_card_prescreen
from reject_cache import cleanup_reject_cache, load_rejected_ids, record_rejected

from patchright.sync_api import sync_playwright
from human_interaction import human_scroll

try:
    from anti_detection import exit_if_risk_page
except Exception:  # 防风控模块不可用时保持原流程
    exit_if_risk_page = None
import time
import json
import random
import argparse
from urllib.parse import parse_qs, urlsplit


def _performance_value(config_manager, key, default):
    return config_manager.get(f'performance.{key}', default)


def _performance_float(config_manager, key, default, *, minimum=0.0):
    try:
        return max(minimum, float(_performance_value(config_manager, key, default)))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def _performance_range(config_manager, key, default, *, minimum=0.0):
    value = _performance_value(config_manager, key, default)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        value = default
    try:
        low, high = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        low, high = float(default[0]), float(default[1])
    return tuple(sorted((max(minimum, low), max(minimum, high))))


# 实时事务模式必须由“当前真实渲染的卡片”驱动，而不是由 geek/list API
# 返回的整批数据驱动。API 只补充卡片预筛需要的结构化字段；只有下列扫描
# 实际看见的 DOM 卡片才允许进入预筛和后续简历闭环。
SCAN_RENDERED_CARDS_JS = r"""() => {
    window.__bossLiveCardTokenSeq = (window.__bossLiveCardTokenSeq || 0);
    const normalizeName = value => String(value || '').replace(/\s+/g, '')
        .replace(/(刚刚活跃|在线|今日活跃|3日内活跃|本周活跃|2周内活跃|本月活跃)$/, '');
    const isNonStandardRecommendation = card => {
        const cardText = String(card.innerText || card.textContent || '')
            .replace(/\s+/g, '');
        // “热搜牛人推荐”里的展示项外形接近普通 card-item，但点击会进入
        // 独立推荐界面，不是当前岗位的标准候选卡。
        if (cardText.includes('首项推荐') && cardText.includes('热搜')) return true;
        if (cardText.includes('热搜牛人推荐')) return true;

        // 标题通常位于卡片的父级模块，卡片自身不一定包含标题文字。
        // 限制模块内 card-item 数量，避免误把包含整个列表的页面根节点排除。
        let node = card.parentElement;
        for (let depth = 0; node && node !== document.body && depth < 7;
             depth += 1, node = node.parentElement) {
            const text = String(node.innerText || node.textContent || '')
                .replace(/\s+/g, '');
            if (text.includes('热搜牛人推荐') && text.includes('查看更多')) {
                const moduleCards = node.querySelectorAll('li.card-item');
                if (moduleCards.length <= 3) return true;
            }
        }
        return false;
    };
    const cards = Array.from(document.querySelectorAll('li.card-item'))
        .filter(card => !isNonStandardRecommendation(card));
    return cards.map((card, index) => {
        if (!card.dataset.bossLiveCardToken) {
            window.__bossLiveCardTokenSeq += 1;
            card.dataset.bossLiveCardToken = `boss-live-${Date.now()}-${window.__bossLiveCardTokenSeq}`;
        }
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
            for (const anchor of card.querySelectorAll('a[href]')) {
                const href = anchor.getAttribute('href') || '';
                const match = href.match(/[?&](?:geekId|encryptGeekId|geek_id)=([^&]+)/);
                if (match) { gid = decodeURIComponent(match[1]); break; }
            }
        }
        const nameEl = card.querySelector('[class*="name"]') || card.querySelector('h3, h4');
        const name = nameEl ? normalizeName(nameEl.textContent) : '';
        const rect = card.getBoundingClientRect();
        return {
            dom_gid: gid,
            name,
            token: card.dataset.bossLiveCardToken,
            no_work_experience: /未填写工作经[历验]|暂无工作经历|无工作经[历验]/.test(card.innerText || card.textContent || ''),
            index,
            top: rect.top,
            bottom: rect.bottom,
            rendered: rect.width > 0 && rect.height > 0,
            visible: rect.bottom > 0 && rect.top < window.innerHeight,
        };
    }).filter(item => item.rendered && item.visible && (item.dom_gid || item.name))
      .sort((a, b) => a.top - b.top || a.index - b.index);
}"""


def _normalize_candidate_name(value):
    """与页面侧姓名归一化保持一致，仅用于无共同 ID 时的唯一姓名兜底。"""
    text = ''.join(str(value or '').split())
    for suffix in ('刚刚活跃', '在线', '今日活跃', '3日内活跃',
                   '本周活跃', '2周内活跃', '本月活跃'):
        if text.endswith(suffix):
            return text[:-len(suffix)]
    return text


def _cache_api_candidate(by_id, by_name, candidate, expected_job_id):
    """缓存 API 元数据；不做预筛、不入处理队列，也不将其视为已扫描卡片。"""
    card = candidate.get('geekCard') or {}
    card_job_id = str(card.get('encryptJobId') or '')
    if expected_job_id and not card_job_id:
        return False
    if card_job_id and card_job_id != expected_job_id:
        return False
    geek_id = str(candidate.get('encryptGeekId') or card.get('encryptGeekId')
                  or card.get('encGeekId') or '')
    name = _normalize_candidate_name(card.get('geekName') or candidate.get('name'))
    if geek_id:
        by_id[geek_id] = candidate
    if name:
        bucket = by_name.setdefault(name, {})
        bucket[geek_id or f'__anonymous_{id(candidate)}'] = candidate
    return bool(geek_id or name)


def _match_rendered_candidate(rendered_card, by_id, by_name):
    """把一张当前 DOM 卡片匹配到 API 元数据；姓名只允许唯一匹配。"""
    dom_gid = str(rendered_card.get('dom_gid') or '')
    if dom_gid and dom_gid in by_id:
        return by_id[dom_gid]
    name = _normalize_candidate_name(rendered_card.get('name'))
    matches = list((by_name.get(name) or {}).values())
    return matches[0] if len(matches) == 1 else None


def _read_recommend_response(resp, expected_job_id):
    """校验接口响应；空列表与 HTTP/业务错误、缺少字段必须分开。"""
    status = getattr(resp, 'status', 200)
    if not 200 <= status < 300:
        raise ValueError(f'geek/list HTTP {status}')
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError('geek/list 响应不是 JSON 对象')
    if data.get('code') not in (None, 0, '0'):
        raise ValueError(f'geek/list 业务错误 code={data.get("code")}')
    payload = data.get('zpData')
    if not isinstance(payload, dict) or not isinstance(payload.get('geekList'), list):
        raise ValueError('geek/list 缺少有效 zpData.geekList')
    candidates = payload['geekList']
    if any(not isinstance(item, dict) or not isinstance(item.get('geekCard'), dict)
           for item in candidates):
        raise ValueError('geek/list 候选卡片结构不完整')
    if any(
        str(item['geekCard'].get('encryptJobId') or '') == str(expected_job_id)
        and not (item.get('encryptGeekId') or item['geekCard'].get('encryptGeekId')
                 or item['geekCard'].get('encGeekId'))
        for item in candidates
    ):
        raise ValueError('geek/list 当前岗位卡片缺少候选人 ID')

    # 空列表不能靠候选字段确认所属岗位，必须同时核对请求中的岗位 ID。
    # 没有明确的当前岗位范围时，不将响应当作该岗位已耗尽的证据。
    query = parse_qs(urlsplit(resp.url).query)
    request = getattr(resp, 'request', None)
    body = getattr(request, 'post_data', None)
    if body:
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            parsed = parse_qs(body)
        if isinstance(parsed, dict):
            query.update(parsed)
    request_job_ids = []
    for key in ('encryptJobId', 'jobId'):
        value = query.get(key)
        request_job_ids.extend(value if isinstance(value, list) else [value])
    current_job = str(expected_job_id) in {
        str(value) for value in request_job_ids if value not in (None, '')
    } or any(
        str(item['geekCard'].get('encryptJobId') or '') == str(expected_job_id)
        for item in candidates
    )
    # 只接受接口明确的布尔结束信号；[]、重复数据、滚动无变化均不足以证明耗尽。
    exhausted = current_job and payload.get('hasMore') is False
    return candidates, current_job, exhausted


def _list_stall_reason(diagnostics):
    """将无新增的停止原因与候选池确实结束区分，供日志和机器读取。"""
    if diagnostics.get('active_api_error'):
        return 'api_error'
    if diagnostics.get('last_unmatched_card_count'):
        return 'metadata_unavailable'
    if diagnostics.get('exhaustion_confirmed'):
        return 'list_exhausted'
    if diagnostics.get('last_dom_state') == 'repeated_cards':
        return 'repeated_cards'
    if diagnostics.get('last_api_state') == 'repeated_cards':
        return 'repeated_cards'
    return 'no_new_cards'


# ── 图形验证码检测（退出码 30 专用）──
CAPTCHA_EXIT_CODE = 30
_CAPTCHA_URL_PATTERNS = ("/safe/", "/security-check", "/verify", "/captcha")
_CAPTCHA_KEYWORDS = ("验证码", "安全验证", "滑块", "拖动", "captcha", "verify")
_CAPTCHA_SELECTORS = ('[class*="captcha"]', '[class*="slider"]', '[class*="verify"]', '#captcha')


def detect_captcha(pg, log=print):
    """检测页面是否出现图形验证码。命中时打印 JSON + SystemExit(30)。"""
    # 先看防风控熔断状态：连续跳验证页 / 同 URL 刷新循环时立即停手
    if exit_if_risk_page:
        exit_if_risk_page(pg)
    url = (pg.url or "").lower()
    # 1. URL 检测
    if any(p in url for p in _CAPTCHA_URL_PATTERNS):
        print(json.dumps({
            "status": "captcha", "exit_code": CAPTCHA_EXIT_CODE,
            "message": "检测到 BOSS 图形验证码（URL 命中），需人工处理",
            "url": pg.url, "reason": "url_pattern",
        }, ensure_ascii=False))
        raise SystemExit(CAPTCHA_EXIT_CODE)
    # 2. 页面文本检测
    try:
        text = pg.inner_text("body", timeout=2000) or ""
    except Exception:
        text = ""
    if any(k in text.lower() for k in _CAPTCHA_KEYWORDS):
        print(json.dumps({
            "status": "captcha", "exit_code": CAPTCHA_EXIT_CODE,
            "message": "检测到 BOSS 图形验证码（页面文本命中），需人工处理",
            "url": pg.url, "reason": "page_text", "text": text[:200],
        }, ensure_ascii=False))
        raise SystemExit(CAPTCHA_EXIT_CODE)
    # 3. DOM 元素检测
    for sel in _CAPTCHA_SELECTORS:
        try:
            if pg.query_selector(sel):
                print(json.dumps({
                    "status": "captcha", "exit_code": CAPTCHA_EXIT_CODE,
                    "message": "检测到 BOSS 图形验证码（DOM 元素命中），需人工处理",
                    "url": pg.url, "reason": "dom_element", "selector": sel,
                }, ensure_ascii=False))
                raise SystemExit(CAPTCHA_EXIT_CODE)
        except SystemExit:
            raise
        except Exception:
            pass


def get_recommend_candidates(job_name='车架工程师', max_candidates=None,
                             batch_size=None, batch_number=None,
                             run_id=None, encrypt_job_id=None,
                             candidate_handler=None):
    """
    获取推荐牛人候选人列表

    Args:
        job_name:        岗位名称（jobs.json metadata）
        max_candidates:  最大获取数量（None 为全部，与分批互斥）
        batch_size:      每批收集人数（分批模式）
        batch_number:    第几批，从 1 开始（分批模式）
        run_id:          本次 run ID（必填；数据边界）
        encrypt_job_id:  BOSS encryptJobId（推荐；env BOSS_HR_ENCRYPT_JOB_ID 可兑底）
    """
    # 2026-07-28 修复：跟走 RunOrchestrator，让 list 落到跟前面 step 同一 run_id，
    #   避免每次 skill 默认开新 run 目录。
    from output_manager import resolve_encrypt_job_id
    encrypt_job_id = resolve_encrypt_job_id(encrypt_job_id)
    if not encrypt_job_id:
        raise ValueError("缺少 encrypt_job_id。\n  传 --encrypt-job-id，或设置 env BOSS_HR_ENCRYPT_JOB_ID")
    orch = RunOrchestrator(job_name, encrypt_job_id=encrypt_job_id)
    # 2026-07-30 重构：run_id 是数据边界，必须显式传。
    # --run-id 已设为 required=True，argparse 会保证 run_id 非空。
    # bind_existing_run 校验 run_dir 存在 + encrypt_job_id 匹配，不通过报错。
    run_id = orch.bind_existing_run(run_id)
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
    config_manager = get_config()
    recruitment_config = dict(config_manager.section("recruitment"))
    job_profile = config_manager.load_job_profile(job_name)
    if job_profile:
        recruitment_config.update(job_profile)

    # 当天已拒绝过的卡片：抓取时直接跳过，避免每个大轮重复扫同一个人。
    cleanup_reject_cache()
    rejected_cache_ids = load_rejected_ids(job_name)

    # 分批状态文件
    state_path = output.get_process_path('batch_state.json')
    rejected_path = output.get_process_path('card_prescreen_rejected.json')
    summary_path = output.get_process_path('card_prescreen_summary.json')

    # 读取已有状态（batch 2+ 需要）
    # seen_ids 在实时模式中只表示“已经取得明确终态的真实 DOM 卡片”。
    # API 响应里出现过但尚未渲染/处理的候选人绝不能提前放进来。
    seen_ids = set()
    all_geeks = []
    prev_total = 0

    if batch_number and batch_number > 1 and os.path.exists(state_path):
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        seen_ids = set(state.get('seen_ids', []))
        prev_total = state.get('total_collected', 0)
        print(f'[分批模式] 第 {batch_number} 批，前 {prev_total} 人已收集，继续滚动...')

    # 本批收集的新候选人
    batch_geeks = []
    rejected_geeks = []
    handler_state = {'next_index': 0, 'stop': False, 'last_result': None}
    api_candidates_by_id = {}
    api_candidates_by_name = {}
    live_retry_counts = {}
    metadata_wait_counts = {}
    list_diagnostics = {
        'termination_reason': 'running',
        'exhaustion_confirmed': False,
        'api_end_of_list': False,
        'api_response_count': 0,
        'api_success_count': 0,
        'api_error_count': 0,
        'api_candidate_count': 0,
        'api_repeated_candidate_count': 0,
        'api_ignored_candidate_count': 0,
        'remaining_api_candidate_count': 0,
        'last_api_state': 'not_observed',
        'active_api_error': '',
        'last_api_error': '',
        'last_rendered_card_count': 0,
        'last_unmatched_card_count': 0,
        'metadata_wait_count': 0,
        'consecutive_no_new_rounds': 0,
    }
    observed_api_ids = set()

    def _scanned_count():
        """本轮已完成卡片预筛的数量，包含通过和拦截。"""
        return len(batch_geeks) + len(rejected_geeks)

    def _record_candidate(g):
        """硬性条件允许时进入下载队列；缺失求职状态保留待确认审计。"""
        card_job_id = str((g.get('geekCard') or {}).get('encryptJobId') or '')
        if not card_job_id:
            print('[warn] 忽略缺少 encryptJobId 的候选响应', file=sys.stderr)
            return False
        if card_job_id != encrypt_job_id:
            print(
                f'[warn] 忽略其他岗位候选响应：expected={encrypt_job_id} '
                f'actual={card_job_id}',
                file=sys.stderr,
            )
            return False
        geek_id = str(g.get('encryptGeekId') or (g.get('geekCard') or {}).get('encryptGeekId') or '')
        if geek_id and geek_id in rejected_cache_ids:
            return False
        result = evaluate_card_prescreen(g, recruitment_config)
        if result.status == 'rejected' and geek_id:
            rejected_cache_ids.add(geek_id)
            record_rejected(job_name, geek_id, result.reasons)
        slim = _slim_candidate_card(g, result.to_dict())
        if result.allow_full_resume:
            all_geeks.append(slim)
            batch_geeks.append(slim)
            return True
        rejected_geeks.append({
            'encryptGeekId': g.get('encryptGeekId', ''),
            'name': (g.get('geekCard') or {}).get('geekName', '') or g.get('name', ''),
            'status': result.status,
            'reasons': result.reasons,
            'missing_fields': result.missing_fields,
            'extracted': result.extracted,
            'card': slim,
        })
        return False

    def _cache_candidate(g):
        if not _cache_api_candidate(
                api_candidates_by_id, api_candidates_by_name, g, encrypt_job_id):
            card_job_id = str((g.get('geekCard') or {}).get('encryptJobId') or '')
            if card_job_id and card_job_id != encrypt_job_id:
                print(
                    f'[warn] 忽略其他岗位候选响应：expected={encrypt_job_id} '
                    f'actual={card_job_id}',
                    file=sys.stderr,
                )

    def on_response(resp):
        """拦截 geek/list API 响应"""
        if 'geek/list' in resp.url:
            list_diagnostics['api_response_count'] += 1
            try:
                geek_list, current_job, exhausted = _read_recommend_response(resp, encrypt_job_id)
                list_diagnostics['api_success_count'] += 1
                list_diagnostics['api_candidate_count'] += len(geek_list)
                current_ids = {
                    str(g.get('encryptGeekId') or g['geekCard'].get('encryptGeekId')
                        or g['geekCard'].get('encGeekId') or '')
                    for g in geek_list
                    if str(g['geekCard'].get('encryptJobId') or '') == encrypt_job_id
                } - {''}
                list_diagnostics['api_repeated_candidate_count'] += len(current_ids & observed_api_ids)
                list_diagnostics['api_ignored_candidate_count'] += sum(
                    str(g['geekCard'].get('encryptJobId') or '') != encrypt_job_id
                    for g in geek_list
                )
                if current_job:
                    list_diagnostics['active_api_error'] = ''
                    list_diagnostics['api_end_of_list'] = exhausted
                    list_diagnostics['last_api_state'] = (
                        'new_cards' if current_ids - observed_api_ids else
                        'repeated_cards' if current_ids else 'empty_list'
                    )
                observed_api_ids.update(current_ids)
                for g in geek_list:
                    if candidate_handler is not None:
                        # 实时模式禁止把整批 API 数据直接变成待处理队列。这里仅
                        # 建索引，稍后必须由当前 DOM 中真实存在的一张卡片触发。
                        _cache_candidate(g)
                    else:
                        if max_candidates and not batch_number and _scanned_count() >= max_candidates:
                            break
                        if str(g['geekCard'].get('encryptJobId') or '') != encrypt_job_id:
                            continue
                        gid = g.get('encryptGeekId', '')
                        if gid and gid not in seen_ids:
                            seen_ids.add(gid)
                            _record_candidate(g)
            except Exception as exc:
                list_diagnostics['api_error_count'] += 1
                list_diagnostics['active_api_error'] = str(exc)[:500]
                list_diagnostics['last_api_error'] = str(exc)[:500]
                list_diagnostics['exhaustion_confirmed'] = False
                list_diagnostics['api_end_of_list'] = False
                print(f'[warn] geek/list 响应解析失败: {exc}', file=sys.stderr)
                _save_live_artifacts()

    def _drain_passed_candidates(pg, ctx):
        """兼容非实时模式的历史队列消费。"""
        if candidate_handler is None:
            return False
        while handler_state['next_index'] < len(batch_geeks):
            if exit_if_risk_page:
                exit_if_risk_page(pg)
            candidate = batch_geeks[handler_state['next_index']]
            handler_state['next_index'] += 1
            result = candidate_handler(candidate, pg, ctx) or {}
            handler_state['last_result'] = result
            if result.get('stop'):
                handler_state['stop'] = True
                return True
        return False

    def _current_recommend_frame(pg):
        iframe = (pg.query_selector('iframe[src*="frame/recommend"]')
                  or pg.query_selector('iframe'))
        if not iframe or not iframe.content_frame():
            raise RuntimeError('推荐牛人页未加载出候选人 iframe')
        return iframe, iframe.content_frame(), iframe.bounding_box()

    def _refresh_list_diagnostics():
        list_diagnostics['remaining_api_candidate_count'] = len(observed_api_ids - seen_ids)
        list_diagnostics['exhaustion_confirmed'] = bool(
            list_diagnostics['api_end_of_list']
            and not list_diagnostics['remaining_api_candidate_count']
            and not list_diagnostics['last_unmatched_card_count']
            and not list_diagnostics['active_api_error']
        )

    def _save_live_artifacts():
        _refresh_list_diagnostics()
        _save_card_prescreen_artifacts(
            rejected_path, summary_path, rejected_geeks, batch_geeks,
            list_diagnostics=list_diagnostics,
        )

    def _process_one_rendered_card(pg, ctx):
        """处理当前 DOM 中一张尚未终态的卡片。

        每次调用最多选择一张卡片。通过预筛时把当前 DOM 身份作为瞬时提示
        交给处理器，处理器只能点击这张仍在 DOM 中的卡片，禁止回顶滚动搜索。
        返回 True 表示本次做了工作（包括一次可重试失败）。
        """
        _iframe, frame, _iframe_box = _current_recommend_frame(pg)
        rendered_cards = frame.evaluate(SCAN_RENDERED_CARDS_JS) or []
        list_diagnostics['last_rendered_card_count'] = len(rendered_cards)
        list_diagnostics['last_unmatched_card_count'] = 0
        list_diagnostics['last_dom_state'] = 'repeated_cards' if rendered_cards else 'no_visible_cards'
        unmatched_cards = []
        for rendered in rendered_cards:
            candidate = _match_rendered_candidate(
                rendered, api_candidates_by_id, api_candidates_by_name,
            )
            if not candidate:
                unmatched_cards.append(rendered)
                continue
            card = candidate.get('geekCard') or {}
            geek_id = str(candidate.get('encryptGeekId') or card.get('encryptGeekId')
                          or card.get('encGeekId') or '')
            if not geek_id or geek_id in seen_ids:
                continue
            if geek_id in rejected_cache_ids:
                # 今天已经扫过并被拒的人，直接在卡片层跳过，不再重复判断。
                seen_ids.add(geek_id)
                continue
            list_diagnostics['last_dom_state'] = 'new_card'

            if rendered.get('no_work_experience'):
                candidate = dict(candidate)
                candidate['geekCard'] = dict(card, workExperienceDesc='未填写工作经历')
            result = evaluate_card_prescreen(candidate, recruitment_config)
            if result.status == 'rejected' and geek_id:
                rejected_cache_ids.add(geek_id)
                record_rejected(job_name, geek_id, result.reasons)
            slim = _slim_candidate_card(candidate, result.to_dict())
            if not result.allow_full_resume:
                rejected_geeks.append({
                    'encryptGeekId': geek_id,
                    'name': card.get('geekName', '') or candidate.get('name', ''),
                    'status': result.status,
                    'reasons': result.reasons,
                    'missing_fields': result.missing_fields,
                    'extracted': result.extracted,
                    'card': slim,
                })
                seen_ids.add(geek_id)
                _save_live_artifacts()
                print(
                    f'[逐卡事务] #{_scanned_count()} {card.get("geekName", "")} '
                    f'卡片预筛终态={result.status}'
                )
                return True

            handler_candidate = dict(slim)
            handler_candidate['_live_dom_card'] = {
                'dom_gid': rendered.get('dom_gid') or '',
                'name': rendered.get('name') or card.get('geekName') or '',
                'token': rendered.get('token') or '',
            }
            handler_result = candidate_handler(handler_candidate, pg, ctx) or {}
            handler_state['last_result'] = handler_result

            if (handler_result.get('ok') and handler_result.get('stop')
                    and handler_result.get('reason') == 'greeting_quota_reached'):
                # 恢复一个已经达到配额的 run 时，不得把尚未处理的当前卡片
                # 误记为完整闭环。
                handler_state['stop'] = True
                return True

            if handler_result.get('ok'):
                # 只有截图/AI/必要招呼完整落盘，或候选人已有明确历史终态，
                # 才计入本轮 30 张完成数。
                batch_geeks.append(slim)
                all_geeks.append(slim)
                seen_ids.add(geek_id)
                live_retry_counts.pop(geek_id, None)
                _save_live_artifacts()
                if handler_result.get('stop'):
                    handler_state['stop'] = True
                print(
                    f'[逐卡事务] #{_scanned_count()} {card.get("geekName", "")} '
                    '完整闭环已落盘'
                )
                return True

            if handler_result.get('stop'):
                raise RuntimeError(
                    f'候选人 {card.get("geekName", "")} 处理遇到致命错误：'
                    f'{handler_result.get("reason") or "unknown"}'
                )

            attempts = live_retry_counts.get(geek_id, 0) + 1
            live_retry_counts[geek_id] = attempts
            if attempts >= 3:
                # 这张卡已经真实执行了三次完整闭环，失败原因也由处理器逐人
                # 落盘。将它记为本轮明确的失败终态后继续下一张，避免一个人
                # 阻塞剩余 29 张；它没有被静默跳过，后续 run 仍可再次尝试。
                terminal = dict(slim)
                terminal['_transaction'] = {
                    'status': 'failed_after_retries',
                    'attempts': attempts,
                    'reason': str(handler_result.get('reason') or 'unknown')[:300],
                }
                batch_geeks.append(terminal)
                all_geeks.append(terminal)
                seen_ids.add(geek_id)
                live_retry_counts.pop(geek_id, None)
                _save_live_artifacts()
                print(
                    f'[逐卡事务] #{_scanned_count()} {card.get("geekName", "")} '
                    f'闭环连续失败 {attempts} 次，已记录失败终态并继续下一张'
                )
                return True
            print(
                f'[逐卡事务] {card.get("geekName", "")} 闭环失败，'
                f'原卡立即重试 {attempts}/3'
            )
            retry_delay = _performance_float(
                config_manager,
                'browser.candidate_retry_delay_seconds',
                0.5,
            )
            pg.wait_for_timeout(int(retry_delay * attempts * 1000))
            return True

        # 当前屏真实存在但尚无 API 元数据的卡片绝不能被滚过去。先等待响应；
        # 若持续无法匹配则明确中止，保留现场供下一轮处理。
        if unmatched_cards:
            list_diagnostics['last_unmatched_card_count'] = len(unmatched_cards)
            list_diagnostics['metadata_wait_count'] += 1
            card = unmatched_cards[0]
            token = str(card.get('token') or card.get('dom_gid') or card.get('name') or '')
            waits = metadata_wait_counts.get(token, 0) + 1
            metadata_wait_counts[token] = waits
            if waits >= 10:
                list_diagnostics['termination_reason'] = 'metadata_unavailable'
                _save_live_artifacts()
                raise RuntimeError(
                    '当前可见卡片连续等待接口元数据失败，已停止下滚以防漏人：'
                    f'name={card.get("name") or "未知"} '
                    f'dom_gid={card.get("dom_gid") or "无"}'
                )
            print(
                f'[逐卡事务] 当前卡片 {card.get("name") or "未知"} '
                f'等待接口元数据 {waits}/10，禁止跳过'
            )
            metadata_wait = _performance_float(
                config_manager,
                'browser.metadata_wait_seconds',
                0.3,
            )
            pg.wait_for_timeout(int(metadata_wait * 1000))
            return True
        return False

    def _drain_current_dom(pg, ctx):
        """不断重扫当前 DOM；每完成一张就重新取 DOM，绝不消费旧卡片列表。"""
        if candidate_handler is None:
            return _drain_passed_candidates(pg, ctx)
        while not handler_state['stop']:
            if exit_if_risk_page:
                exit_if_risk_page(pg)
            if max_candidates and not batch_number and _scanned_count() >= max_candidates:
                return True
            if not _process_one_rendered_card(pg, ctx):
                return False
        return True

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        pg = get_or_create_recommend_page(ctx, log=print)

        # 岗位切换期间的新岗位首屏响应也可能很快返回，因此监听器要提前挂载；
        # _record_candidate 会按 encryptJobId 严格丢弃旧岗位的在途响应。
        pg.on('response', on_response)

        if batch_number and batch_number > 1:
            # batch 2+：连接已有页面，不重新加载
            print('连接已有推荐牛人页面（不刷新）...')
            time.sleep(1)
        else:
            # batch 1 / 普通模式：先检查当前页面 URL，避免覆盖用户在 BOSS 调好的筛选条件
            current_url = (pg.url or '')
            if 'zhipin.com/web/chat/recommend' in current_url:
                # 当前已在推荐牛人页面 → 复用用户在 BOSS 调整好的筛选条件
                print(f'当前已在推荐牛人页面（{current_url}），不刷新直接复用用户的筛选条件...')
                time.sleep(1)
            else:
                # 不在 → 才 goto（保留"冷启动"能力）
                print('打开推荐牛人页面...')
                pg.goto('https://www.zhipin.com/web/chat/recommend',
                        wait_until='domcontentloaded', timeout=60000)
                time.sleep(2)

        # 验证码检测（页面加载后立即检查）
        detect_captcha(pg)

        switch = ensure_recommend_job_selected(
            pg,
            encrypt_job_id=encrypt_job_id,
            job_name=job_name,
            log=print,
        )
        if not switch.get('ok'):
            raise RuntimeError(
                '无法切换 BOSS 推荐页岗位：'
                f"{switch.get('reason')} target={switch.get('target_name')}"
            )

        # 切换岗位后点击右上角筛选，按岗位 boss_filter 配置勾选
        # 等待 iframe 内容（筛选按钮）渲染完成，避免岗位切换后页面还没准备好
        time.sleep(2)
        boss_filter_cfg = recruitment_config.get('boss_filter') or {}
        print(f"应用筛选：{job_name} → {boss_filter_cfg}")
        filter_result = apply_job_filters(pg, job_filter_config=boss_filter_cfg, log=print)
        if not filter_result.get('ok'):
            print(f"筛选未成功（{filter_result.get('reason','')}），继续执行...")
            # 筛选失败时再等 2 秒重试一次（iframe 可能加载较慢）
            time.sleep(2)
            filter_result = apply_job_filters(pg, job_filter_config=boss_filter_cfg, log=print)
            if filter_result.get('ok'):
                print(f"筛选重试成功")
            else:
                print(f"筛选重试仍失败（{filter_result.get('reason','')}），继续执行...")
                if boss_filter_cfg.get('gender'):
                    raise RuntimeError(
                        '主播岗位性别筛选未成功，已停止卡片预筛：'
                        f"{filter_result.get('reason', '未知原因')}"
                    )

        # 验证码检测（iframe 加载前再检查一次）
        detect_captcha(pg)

        # 等待 iframe 出现（最多 15 秒）
        iframe = None
        for wait in range(15):
            try:
                iframe = pg.query_selector('iframe')
                if iframe:
                    break
            except Exception:
                pass
            time.sleep(1)

        if not iframe:
            # 2026-09-06 修复：以前这里 return（rc=0），fetch 会静默读取上一轮
            # 的旧产物，掩盖"登录态被踢/页面未加载"的真实失败。改为 blocked
            # JSON + 非零退出码，让 fetch_service 透出真实错误。
            print(json.dumps({
                "status": "blocked",
                "exit_code": 21,
                "run_id": run_id,
                "message": ("推荐牛人页未加载出候选人 iframe（当前页面可能已被"
                            "重定向到登录页，BOSS 登录态可能已失效）。"
                            f"当前页面 URL: {pg.url}"),
            }, ensure_ascii=False))
            raise SystemExit(21)

        frame = iframe.content_frame()
        iframe_box = iframe.bounding_box()

        # 实时模式从当前 DOM 第一张卡片开始，完成一张后重新扫描 DOM；
        # 非实时模式继续保留历史的 API 收集行为。
        _drain_current_dom(pg, ctx)

        # 确定本批目标数量
        if batch_size and batch_number:
            target = batch_size
            print(f'本批目标：收集 {target} 位新候选人')
        else:
            target = max_candidates  # 普通模式

        # 滚动
        no_new_count = 0
        while no_new_count < 5:
            if handler_state['stop']:
                list_diagnostics['termination_reason'] = 'handler_stop'
                print('单人闭环要求停止，本轮不再浏览后续卡片')
                break
            # 普通模式：--max 表示最多扫描多少张卡片，避免为了凑满通过人数长时间滚动。
            if max_candidates and not batch_number and _scanned_count() >= max_candidates:
                list_diagnostics['termination_reason'] = 'scan_limit'
                print(f'已扫描 {max_candidates} 张卡片，停止滚动')
                break
            # 分批模式：保持旧语义，按通过预筛的人数作为本批目标。
            if batch_number and target and len(batch_geeks) >= target:
                list_diagnostics['termination_reason'] = 'batch_target'
                print(f'本批已收集 {len(batch_geeks)} 人，停止滚动')
                break

            prev = _scanned_count()

            # 小幅、分段滚轮 + 阅读停留：一次只推进约半张到一张卡片，
            # 避免连续大幅扫页。human_scroll 内部还会分 2~4 次滚轮事件。
            if candidate_handler is not None:
                # 打开/关闭在线简历后 BOSS 可能重建 iframe，滚动前必须刷新坐标。
                iframe, frame, iframe_box = _current_recommend_frame(pg)
            human_scroll(pg, iframe_box, min_delta=320, max_delta=620)
            delay_min, delay_max = _performance_range(
                config_manager,
                'browser.list_scroll_wait_seconds',
                (0.8, 1.5),
            )
            delay = random.uniform(delay_min, delay_max)
            # 使用 Playwright 等待让 geek/list 响应元数据完成索引。实时模式随后
            # 只从新一屏实际渲染的 DOM 卡片中选一张处理，不消费 API 候选队列。
            pg.wait_for_timeout(int(delay * 1000))
            _drain_current_dom(pg, ctx)

            if _scanned_count() == prev:
                no_new_count += 1
            else:
                no_new_count = 0
            list_diagnostics['consecutive_no_new_rounds'] = no_new_count

            total_now = prev_total + _scanned_count()
            print(
                f'已扫描 {total_now} 张卡片 '
                f'(通过：{len(batch_geeks)}，拦截：{len(rejected_geeks)}，'
                f'连续无新增：{no_new_count}/5)'
            )

        # 接收最后一次滚动触发、但在循环退出边界到达后的响应。
        if not handler_state['stop']:
            _drain_current_dom(pg, ctx)

        # 保存结果
        _refresh_list_diagnostics()
        if list_diagnostics['termination_reason'] == 'running':
            list_diagnostics['termination_reason'] = (
                'handler_stop' if handler_state['stop'] else
                'scan_limit' if max_candidates and not batch_number and _scanned_count() >= max_candidates else
                'batch_target' if batch_number and target and len(batch_geeks) >= target else
                _list_stall_reason(list_diagnostics)
            )
        _save_live_artifacts()
        print(f'列表停止原因：{list_diagnostics["termination_reason"]}')
        if list_diagnostics['termination_reason'] == 'api_error':
            raise RuntimeError(
                '推荐接口持续未恢复，本轮不按候选池耗尽或成功结束：'
                f'{list_diagnostics["active_api_error"]}'
            )
        if (candidate_handler is not None and max_candidates and not batch_number
                and not handler_state['stop'] and _scanned_count() < max_candidates):
            raise RuntimeError(
                f'逐卡事务未完成目标：要求 {max_candidates} 张，只有 '
                f'{_scanned_count()} 张取得明确终态；为防止漏人，本轮不按成功结束'
            )
        if batch_size and batch_number:
            # 分批模式：保存 batch_N_ids.json + 更新累计文件 + 更新状态
            batch_path = output.get_process_path(f'batch_{batch_number}_ids.json')
            with open(batch_path, 'w', encoding='utf-8') as f:
                json.dump(batch_geeks, f, ensure_ascii=False, indent=2)
            print(f'\n本批 {len(batch_geeks)} 人已保存到：{batch_path}')

            # 累计所有候选人（去重）
            all_cumulative = all_geeks  # all_geeks 包含了历史 seen 的 + 本批新的
            # 但 all_geeks 只包含本批新加入的（seen_ids 过滤了旧的）
            # 需要重新从累计文件加载旧的
            cumulative_path = output.recommend_geek_ids_path
            existing = []
            if os.path.exists(cumulative_path):
                with open(cumulative_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            existing_ids = {g.get('encryptGeekId', '') for g in existing}
            for g in batch_geeks:
                gid = g.get('encryptGeekId', '')
                if gid not in existing_ids:
                    existing.append(g)
                    existing_ids.add(gid)

            with open(cumulative_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            print(f'累计 {len(existing)} 人已保存到：{cumulative_path}')

            # 更新状态文件
            new_state = {
                'total_collected': len(existing),
                'seen_ids': list(seen_ids),
                'last_batch': batch_number,
            }
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(new_state, f, ensure_ascii=False, indent=2)

            return batch_geeks
        else:
            # 普通模式：保存全部
            output_path = output.recommend_geek_ids_path
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(all_geeks, f, ensure_ascii=False, indent=2)
            print(f'\n总共获取：{len(all_geeks)} 位候选人')
            print(f'已保存到：{output_path}')
            return all_geeks


def _save_card_prescreen_artifacts(rejected_path, summary_path, rejected_geeks, accepted_geeks,
                                 *, list_diagnostics=None):
    """保存卡片预筛审计，并区分待确认候选人是否进入完整简历阶段。"""
    with open(rejected_path, 'w', encoding='utf-8') as f:
        json.dump(rejected_geeks, f, ensure_ascii=False, indent=2)
    pending_proceeded = sum(
        1 for item in accepted_geeks
        if (item.get('_card_prescreen') or {}).get('status') == 'pending_confirmation'
    )
    pending_blocked = sum(
        1 for item in rejected_geeks if item.get('status') == 'pending_confirmation'
    )
    summary = {
        'accepted_count': len(accepted_geeks),
        'rejected_count': len(rejected_geeks),
        'pending_confirmation_count': pending_proceeded + pending_blocked,
        'pending_proceeded_count': pending_proceeded,
        'pending_blocked_count': pending_blocked,
    }
    if list_diagnostics is not None:
        summary['list_diagnostics'] = dict(list_diagnostics)
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def _slim_candidate_card(g, prescreen):
    """只保存下载和审计需要的卡片字段，避免把整张 BOSS 原始卡片落盘。"""
    card = g.get('geekCard') or {}
    view_expect = card.get('viewExpect') or {}
    # 保留工作经历的公司名（用于竞品公司卡片预筛）
    geek_works = card.get('geekWorks') or []
    slim_works = [
        {
            'company': w.get('company') or '',
            'positionName': w.get('positionName') or '',
            'startDate': w.get('startDate') or '',
            'endDate': w.get('endDate') or '',
            'workTime': w.get('workTime') or '',
        }
        for w in geek_works if isinstance(w, dict)
    ]
    # 保留教育经历（用于毕业年份/27届卡片预筛）
    geek_edus = card.get('geekEdus') or []
    slim_edus = [
        {
            'school': e.get('school') or '',
            'major': e.get('major') or '',
            'degreeName': e.get('degreeName') or '',
            'startDate': e.get('startDate') or '',
            'endDate': e.get('endDate') or '',
        }
        for e in geek_edus if isinstance(e, dict)
    ]
    raw_employment_status = _raw_employment_status_fields(g, card)
    return {
        'encryptGeekId': g.get('encryptGeekId') or card.get('encryptGeekId') or card.get('encGeekId') or '',
        'activeTimeDesc': g.get('activeTimeDesc') or card.get('activeTimeDesc') or '',
        'talkTimeDesc': g.get('talkTimeDesc') or '',
        'haveChatted': g.get('haveChatted'),
        'viewed': card.get('viewed'),
        'geekCard': {
            'encryptGeekId': card.get('encryptGeekId') or card.get('encGeekId') or g.get('encryptGeekId') or '',
            'encGeekId': card.get('encGeekId') or '',
            'encryptJobId': card.get('encryptJobId') or '',
            'jobId': card.get('jobId') or '',
            'securityId': card.get('securityId') or '',
            'geekName': card.get('geekName') or '',
            'ageDesc': card.get('ageDesc') or '',
            'geekDegree': card.get('geekDegree') or '',
            'degreeName': card.get('degreeName') or '',
            'degreeCategory': card.get('degreeCategory') or '',
            'workYearDesc': card.get('workYearDesc') or '',
            'geekWorkYear': card.get('geekWorkYear') or '',
            'freshGraduate': card.get('freshGraduate'),
            'expectLocationName': card.get('expectLocationName') or view_expect.get('locationName') or '',
            'expectPositionName': card.get('expectPositionName') or view_expect.get('positionName') or '',
            'expectPositionNameLv2': card.get('expectPositionNameLv2') or view_expect.get('positionNameLv2') or '',
            'lowSalary': card.get('lowSalary') if card.get('lowSalary') is not None else view_expect.get('lowSalary'),
            'highSalary': card.get('highSalary') if card.get('highSalary') is not None else view_expect.get('highSalary'),
            'applyStatusDesc': card.get('applyStatusDesc') or '',
            # 原始枚举码与可能出现的替代字段只做留存，不在确认映射前参与判断。
            'applyStatus': card.get('applyStatus'),
            'employmentStatus': card.get('employmentStatus'),
            'employmentStatusDesc': card.get('employmentStatusDesc') or '',
            'jobSeekingStatus': card.get('jobSeekingStatus'),
            'jobSeekingStatusDesc': card.get('jobSeekingStatusDesc') or '',
            'recOnline': card.get('recOnline'),
            'geekWorks': slim_works,
            'geekEdus': slim_edus,
        },
        '_raw_employment_status': raw_employment_status,
        '_card_prescreen': prescreen,
    }


def _raw_employment_status_fields(candidate, card):
    """保存 BOSS 求职状态原始标量字段，供后续验证状态码映射。"""
    tokens = ('applystatus', 'employmentstatus', 'jobseekingstatus')

    def collect(source):
        if not isinstance(source, dict):
            return {}
        return {
            str(key): value
            for key, value in source.items()
            if any(token in str(key).lower().replace('_', '') for token in tokens)
            and (value is None or isinstance(value, (str, int, float, bool)))
        }

    return {
        'candidate': collect(candidate),
        'geekCard': collect(card),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='获取推荐牛人候选人列表（支持分批）')
    parser.add_argument('--job-name', default='车架工程师', help='岗位名称（jobs.json metadata）')
    parser.add_argument('--encrypt-job-id', default=None,
                        help='BOSS encryptJobId（推荐；新设计目录名依此定位；亦可走 env BOSS_HR_ENCRYPT_JOB_ID）')
    parser.add_argument('--max', type=int, default=None, help='最大获取数量（普通模式）')
    parser.add_argument('--batch-size', type=int, default=None, help='每批收集人数（分批模式）')
    parser.add_argument('--batch', type=int, default=None, help='第几批，从1开始（分批模式）')
    parser.add_argument('--run-id', required=True,
                        help='【必填】run_id 是数据边界。新任务先跑 boss_jd.py 创建 run；不传直接报错。')
    parser.add_argument('--transactional-live', action='store_true',
                        help='卡片通过后在当前页面立即执行截图→DeepSeek→招呼闭环')
    parser.add_argument('--max-greetings', type=int, default=10)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--greet-message', default='')
    parser.add_argument('--job-requirement-json', default='')

    args = parser.parse_args()

    processor = None
    if args.transactional_live:
        from recommend_download import LiveCandidateProcessor
        requirement_override = None
        if args.job_requirement_json:
            requirement_override = json.loads(args.job_requirement_json)
        processor = LiveCandidateProcessor(
            job_name=args.job_name,
            encrypt_job_id=args.encrypt_job_id,
            run_id=args.run_id,
            max_greetings=args.max_greetings,
            dry_run=args.dry_run,
            greet_message=args.greet_message,
            job_requirement=requirement_override,
        )

    if args.batch_size and args.batch:
        get_recommend_candidates(args.job_name,
                                 batch_size=args.batch_size,
                                 batch_number=args.batch,
                                 run_id=args.run_id,
                                 encrypt_job_id=args.encrypt_job_id,
                                 candidate_handler=processor.process if processor else None)
    else:
        get_recommend_candidates(args.job_name,
                                 max_candidates=args.max,
                                 run_id=args.run_id,
                                 encrypt_job_id=args.encrypt_job_id,
                                 candidate_handler=processor.process if processor else None)
    if processor:
        print(json.dumps({
            'status': 'transactional_live_complete',
            'run_id': args.run_id,
            **processor.finalize(),
        }, ensure_ascii=False))
