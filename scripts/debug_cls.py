"""检查 BOSS 面板选项的 className 精确值。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared"))

from patchright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

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

        # 打开面板
        frame.evaluate("""() => {
            const el = document.querySelector('.recover-last-change-params .cancel');
            if (el) el.click();
        }""")
        time.sleep(1)

        btn = frame.query_selector('.filter-label')
        if btn:
            btn.click(timeout=5000)
            time.sleep(2)

        # 打印所有选项的精确 className
        options = frame.evaluate("""() => {
            const panel = document.querySelector('.filter-panel');
            if (!panel) return [];
            const options = panel.querySelectorAll('.option');
            return Array.from(options).map(o => {
                const cls = String(o.className || '');
                const text = (o.innerText || o.textContent || '').trim();
                return {text: text, cls: cls, includesActive: cls.includes('active'), includesDefault: cls.includes('default')};
            });
        }""")

        print("=== 所有选项的 className ===")
        for i, o in enumerate(options):
            marker = ""
            if o['includesActive']: marker += " [ACTIVE]"
            if o['includesDefault']: marker += " [DEFAULT]"
            if marker:
                print(f"[{i}] text='{o['text']}' cls='{o['cls']}'{marker}")

        # 关闭面板
        page.mouse.click(50, 200)
        time.sleep(1)

if __name__ == '__main__':
    main()
