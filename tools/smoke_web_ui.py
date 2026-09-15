"""Browser acceptance checks against an explicitly started local web console.

Run: uv run --extra web --with playwright python tools/smoke_web_ui.py
Uses synthetic video + recording backend only; never selects a hardware backend.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8090")
    parser.add_argument("--output", type=Path, default=Path("artifacts/webui-smoke"))
    parser.add_argument("--channel", default="msedge")
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Browser smoke must target a local test server")
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel, headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""
            window.__pad = null;
            navigator.getGamepads = () => window.__pad ? [window.__pad] : [];
        """)
        page.goto(args.url)
        expect(page.locator("#connection")).to_have_text("服务已连接")
        expect(page.locator("#start")).to_be_disabled()
        page.locator("#claim").click()
        expect(page.locator("#settings")).to_be_enabled()
        page.locator("#source").fill("synthetic")
        page.locator("#backend").select_option("recording")
        page.locator("#obstacle").uncheck()
        page.locator("#start").click()
        expect(page.locator("#session-badge")).to_have_text("运行中")
        expect(page.locator("#video")).to_be_visible()
        page.wait_for_function("() => document.querySelector('#video').naturalWidth > 0")
        expect(page.locator("#arm")).to_have_text("武装自治")
        expect(page.locator("#target")).to_have_text("已确认")
        page.screenshot(path=str(args.output / "desktop.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
        for tab in ("connection", "tuning", "capture", "status"):
            page.locator(f"#tab-{tab}").click()
            assert page.evaluate("""() => {
                const panel = document.querySelector('.inspector-body');
                return panel.scrollHeight <= panel.clientHeight + 1;
            }"""), f"{tab} requires sidebar scrolling at 1366x768"
        for selector in ("#estop", "#start", "#arm", "#stop", "#hold", ".forward"):
            assert page.locator(selector).evaluate("""element => {
                const r = element.getBoundingClientRect();
                return r.top >= 0 && r.bottom <= innerHeight;
            }"""), f"{selector} is outside the viewport"

        # A second browser sees the same stream but cannot take the live lease.
        other_context = browser.new_context()
        observer = other_context.new_page()
        observer.goto(args.url)
        expect(observer.locator("#owner-status")).to_have_text("其他页面")
        expect(observer.locator("#claim")).to_be_disabled()
        expect(observer.locator("#start")).to_be_disabled()
        expect(observer.locator("#estop")).to_be_enabled()

        forward = page.locator(".forward")
        forward.hover()
        page.mouse.down()
        expect(page.locator("#output")).to_contain_text("v 0.35")
        page.mouse.up()
        expect(page.locator("#output")).to_contain_text("v 0.00")

        page.locator("#tab-tuning").click()
        page.locator("#tune-minimum_contour_area").fill("65")
        page.locator("#apply-tuning").click()
        expect(page.locator("#notice")).to_have_text("参数已应用到当前会话")
        page.locator("#tab-capture").click()
        page.locator("#capture").click()
        expect(page.locator("#capture")).to_have_text("停止采集")
        page.locator("#capture").click()
        expect(page.locator("#capture")).to_have_text("开始采集")

        # Standard browser Gamepad API input travels through the real WebSocket.
        page.evaluate("""window.__pad = {id:'Test standard pad', connected:true,
            mapping:'standard', axes:[0, -0.6, 0, 0],
            buttons:Array.from({length:17}, () => ({pressed:false}))};""")
        page.locator("#browser-gamepad").check()
        expect(page.locator("#output")).to_contain_text("v 0.50")
        page.evaluate("window.__pad.axes[1] = 0")
        expect(page.locator("#output")).to_contain_text("v 0.00")
        page.evaluate("window.__pad = null")
        page.locator("#browser-gamepad").uncheck()

        # Responsive layout must not introduce horizontal page scrolling.
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(args.output / "mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
        page.locator("button[data-mobile-view=manual]").click()
        page.screenshot(path=str(args.output / "mobile-controls.png"), full_page=True)
        expect(page.locator(".forward")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
        page.locator("button[data-mobile-view=settings]").click()
        expect(page.locator("#tab-tuning")).to_be_visible()
        page.locator("#tab-tuning").click()
        page.screenshot(path=str(args.output / "mobile-tuning.png"), full_page=True)
        assert page.locator("#apply-tuning").evaluate(
            "el => el.getBoundingClientRect().bottom <= innerHeight"
        )
        page.locator("button[data-mobile-view=video]").click()

        observer.locator("#estop").click()
        expect(page.locator("#session-badge")).to_have_text("已停止")
        expect(page.locator("#video")).to_be_hidden()
        expect(page.locator("#reset")).to_be_enabled()
        page.locator("#reset").click()
        expect(page.locator("#start")).to_be_enabled()
        page.locator("#start").click()
        expect(page.locator("#session-badge")).to_have_text("运行中")
        page.close()
        expect(observer.locator("#session-badge")).to_have_text("已停止")
        expect(observer.locator("#owner-status")).to_have_text("未分配")
        assert not errors, errors
        other_context.close()
        context.close()
        browser.close()
    print(json.dumps({"result": "passed", "screenshots": str(args.output.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
