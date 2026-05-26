from __future__ import annotations

from pathlib import Path

import pytest

from altdata_brief.publish.gh_pages import _render_index_md

try:
    from playwright import sync_api as playwright_sync_api
except ImportError:  # pragma: no cover - optional browser smoke dependency
    playwright_sync_api = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PREVIEW_STATUS = "本地预览 · 自动刷新关闭"


def _launch_browser(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as exc:  # pragma: no cover - depends on host browser cache
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Chromium is not installed for Playwright on this host")
        raise


def _brief_layout_preview_html() -> str:
    layout = (PROJECT_ROOT / "gh-pages-template" / "_layouts" / "brief.html").read_text(
        encoding="utf-8"
    )
    script = layout.split("<script>", 1)[1].split("</script>", 1)[0]
    return f"""
    <!doctype html>
    <html>
      <body>
        <span data-refresh-status aria-live="polite">自动刷新检查中</span>
        <script>{script}</script>
      </body>
    </html>
    """


def _assert_local_preview_disables_fetch(html: str) -> None:
    if playwright_sync_api is None:
        pytest.skip("Playwright is not installed in this environment")

    with playwright_sync_api.sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        try:
            page = browser.new_page()
            page.evaluate(
                """
                () => {
                  window.__refreshFetchCalls = 0;
                  window.fetch = async function () {
                    window.__refreshFetchCalls += 1;
                    return { ok: true, text: async function () { return document.documentElement.outerHTML; } };
                  };
                }
                """
            )
            page.set_content(html, wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('[data-refresh-status]')?.textContent.includes('本地预览')"
            )
            assert page.locator("[data-refresh-status]").inner_text() == LOCAL_PREVIEW_STATUS
            assert page.evaluate("window.__refreshFetchCalls") == 0
        finally:
            browser.close()


def test_index_page_local_preview_disables_live_refresh_fetch() -> None:
    html = _render_index_md(["2026-05-17"])

    _assert_local_preview_disables_fetch(html)


def test_brief_page_local_preview_disables_live_refresh_fetch() -> None:
    _assert_local_preview_disables_fetch(_brief_layout_preview_html())
