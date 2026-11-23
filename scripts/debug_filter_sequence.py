"""逐步执行筛选，每次点击后检查所有 active 选项，找出哪个被取消。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared"))

from patchright.sync_api import sync_playwright
from boss_hr.config_manager import get_config
from boss_job_switcher import _recommend_content_root

def get_active_options(frame):
    """获取当前所有 active 的选项。"""
    return frame.evaluate("""() => {
        const panel = document.querySelector('.filter-panel');
        if (!panel) return [];
        const options = panel.querySelectorAll('.option');
        return Array.from(options).filter(o => {
            const cls = String(o.className || '');
            return cls.includes('active') && !cls.includes('default');
        }).map(o => (o.innerText || o.textContent || '').trim());
    }""")

def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if 'recommend' not in page.url:
            page.goto('https://www.zhipin.com/web/chat/recommend',
                       wait_until='domcontentloaded', timeout=20000)
            time.sleep(3)

        iframe_el = None
        for _ in range(20):
            try:
                iframe_el = page.query_selector('iframe[src*="frame/recommend"]')
                if iframe_el:
                    break
            except Exception:
                pass
            time.sleep(1)

        frame = iframe_el.content_frame()

        # 关闭弹窗
        frame.evaluate("""() => {
            const el = document.querySelector('.recover-last-change-params .cancel');
            if (el) el.click();
        }""")
        time.sleep(1)

        # 打开筛选面板
        btn = frame.query_selector('.filter-label')
        if btn:
            btn.click(timeout=5000)
            time.sleep(2)

        frame.wait_for_selector('.filter-panel', timeout=5000)
        print("面板已展开")

        # 关闭弹窗
        frame.evaluate("""() => {
            const el = document.querySelector('.recover-last-change-params .cancel');
            if (el) el.click();
        }""")
        time.sleep(1)

        print(f"\n初始 active: {get_active_options(frame)}")

        # Step 1: 近14天没有
        print("\n--- Step 1: 点击'近14天没有' ---")
        frame.evaluate("""() => {
            const options = document.querySelectorAll('.filter-panel .option');
            for (const opt of options) {
                const cls = String(opt.className || '');
                if (cls.includes('default') || cls.includes('options')) continue;
                const text = (opt.innerText || opt.textContent || '').trim();
                if (text === '近14天没有') {
                    opt.click();
                    return text;
                }
            }
            return null;
        }""")
        time.sleep(1.5)
        print(f"  active: {get_active_options(frame)}")

        # Step 2: 刚刚活跃
        print("\n--- Step 2: 点击'刚刚活跃' ---")
        frame.evaluate("""() => {
            const options = document.querySelectorAll('.filter-panel .option');
            for (const opt of options) {
                const cls = String(opt.className || '');
                if (cls.includes('default') || cls.includes('options')) continue;
                const text = (opt.innerText || opt.textContent || '').trim();
                if (text === '刚刚活跃') {
                    opt.click();
                    return text;
                }
            }
            return null;
        }""")
        time.sleep(1.5)
        print(f"  active: {get_active_options(frame)}")

        # 检查弹窗
        recover = frame.evaluate("""() => {
            const el = document.querySelector('.recover-last-change-params');
            if (!el) return null;
            const s = getComputedStyle(el);
            return {visible: s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null};
        }""")
        print(f"  弹窗状态: {recover}")
        if recover and recover.get('visible'):
            frame.evaluate("document.querySelector('.recover-last-change-params .cancel').click()")
            print("  已关闭弹窗")
            time.sleep(1)
            print(f"  关闭弹窗后 active: {get_active_options(frame)}")

        # Step 3: 27年毕业
        print("\n--- Step 3: 点击'27年毕业' ---")
        result = frame.evaluate("""() => {
            const options = document.querySelectorAll('.filter-panel .option');
            for (const opt of options) {
                const cls = String(opt.className || '');
                if (cls.includes('default') || cls.includes('options')) continue;
                const text = (opt.innerText || opt.textContent || '').trim();
                if (text === '27年毕业') {
                    const wasActive = cls.includes('active');
                    opt.click();
                    return {text: text, wasActive: wasActive, cls: cls};
                }
            }
            return null;
        }""")
        print(f"  点击结果: {result}")
        time.sleep(1.5)

        # 检查弹窗是否在点击后出现
        recover = frame.evaluate("""() => {
            const el = document.querySelector('.recover-last-change-params');
            if (!el) return null;
            const s = getComputedStyle(el);
            return {visible: s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null};
        }""")
        print(f"  弹窗状态: {recover}")
        if recover and recover.get('visible'):
            print("  !!! 弹窗在点击后出现了 !!!")
            # 先不关闭，检查弹窗内容
            popup_text = frame.evaluate("""() => {
                const el = document.querySelector('.recover-last-change-params');
                return el ? el.innerText : '';
            }""")
            print(f"  弹窗内容: {popup_text[:200]}")
            frame.evaluate("document.querySelector('.recover-last-change-params .cancel').click()")
            print("  已关闭弹窗")
            time.sleep(1)
            print(f"  关闭弹窗后 active: {get_active_options(frame)}")
        else:
            print(f"  active: {get_active_options(frame)}")

        # Step 4: 本科
        print("\n--- Step 4: 点击'本科' ---")
        result = frame.evaluate("""() => {
            const options = document.querySelectorAll('.filter-panel .option');
            for (const opt of options) {
                const cls = String(opt.className || '');
                if (cls.includes('default') || cls.includes('options')) continue;
                const text = (opt.innerText || opt.textContent || '').trim();
                if (text === '本科') {
                    const wasActive = cls.includes('active');
                    opt.click();
                    return {text: text, wasActive: wasActive, cls: cls};
                }
            }
            return null;
        }""")
        print(f"  点击结果: {result}")
        time.sleep(1.5)

        recover = frame.evaluate("""() => {
            const el = document.querySelector('.recover-last-change-params');
            if (!el) return null;
            const s = getComputedStyle(el);
            return {visible: s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null};
        }""")
        print(f"  弹窗状态: {recover}")
        if recover and recover.get('visible'):
            print("  !!! 弹窗在点击后出现了 !!!")
            popup_text = frame.evaluate("""() => {
                const el = document.querySelector('.recover-last-change-params');
                return el ? el.innerText : '';
            }""")
            print(f"  弹窗内容: {popup_text[:200]}")
            frame.evaluate("document.querySelector('.recover-last-change-params .cancel').click()")
            print("  已关闭弹窗")
            time.sleep(1)
            print(f"  关闭弹窗后 active: {get_active_options(frame)}")
        else:
            print(f"  active: {get_active_options(frame)}")

        # Step 5: 公办本科
        print("\n--- Step 5: 点击'公办本科' ---")
        result = frame.evaluate("""() => {
            const options = document.querySelectorAll('.filter-panel .option');
            for (const opt of options) {
                const cls = String(opt.className || '');
                if (cls.includes('default') || cls.includes('options')) continue;
                const text = (opt.innerText || opt.textContent || '').trim();
                if (text === '公办本科') {
                    const wasActive = cls.includes('active');
                    opt.click();
                    return {text: text, wasActive: wasActive, cls: cls};
                }
            }
            return null;
        }""")
        print(f"  点击结果: {result}")
        time.sleep(1.5)

        recover = frame.evaluate("""() => {
            const el = document.querySelector('.recover-last-change-params');
            if (!el) return null;
            const s = getComputedStyle(el);
            return {visible: s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null};
        }""")
        print(f"  弹窗状态: {recover}")
        if recover and recover.get('visible'):
            print("  !!! 弹窗在点击后出现了 !!!")
            popup_text = frame.evaluate("""() => {
                const el = document.querySelector('.recover-last-change-params');
                return el ? el.innerText : '';
            }""")
            print(f"  弹窗内容: {popup_text[:200]}")
            frame.evaluate("document.querySelector('.recover-last-change-params .cancel').click()")
            print("  已关闭弹窗")
            time.sleep(1)
            print(f"  关闭弹窗后 active: {get_active_options(frame)}")
        else:
            print(f"  active: {get_active_options(frame)}")

        # 最终状态
        print(f"\n=== 最终 active: {get_active_options(frame)} ===")

        # 关闭面板
        page.mouse.click(50, 200)
        time.sleep(1)


if __name__ == '__main__':
    main()
