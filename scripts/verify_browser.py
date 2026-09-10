"""Opt-in Chromium smoke check against an already running LEVI service.

Runs public demo reads; screenshots and results stay inside the workspace.
"""

import argparse
import json
from playwright.sync_api import sync_playwright
from levi.paths import ROOT, configure, inside


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:7860")
    parser.add_argument("--repo", default="samanthalhy/eval_so100_smol_strawberry_2")
    parser.add_argument("--local-repo")
    args = parser.parse_args()
    configure()
    output = inside(ROOT / "outputs/LEVI/validation")
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(60000)
        page.goto(args.base_url)
        page.wait_for_function("document.documentElement.lang === 'zh-CN'")
        page.locator(
            ".levi-demo-card video, .levi-dataset-card video, video"
        ).first.wait_for()
        page.wait_for_function(
            "document.querySelectorAll('video').length === 2 && [...document.querySelectorAll('video')].every(v => v.readyState >= 2)",
            timeout=120000,
        )
        page.screenshot(path=str(output / "home-zh.png"), full_page=True)
        page.get_by_role("button", name="Switch language / 切换语言").click()
        page.reload()
        page.wait_for_function("document.documentElement.lang === 'en'")
        results["english_persists"] = True
        page.screenshot(path=str(output / "home-en.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        results["mobile_no_horizontal_overflow"] = True
        page.screenshot(path=str(output / "home-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(f"{args.base_url}/{args.repo}/episode_0")
        page.wait_for_function(
            "document.querySelectorAll('video').length >= 1 && [...document.querySelectorAll('video')].every(v => v.readyState >= 2 && v.videoWidth > 0)",
            timeout=120000,
        )
        results["demo_videos"] = page.locator("video").evaluate_all(
            "nodes => nodes.map(v => ({width:v.videoWidth,height:v.videoHeight,readyState:v.readyState}))"
        )
        page.screenshot(path=str(output / "episodes-en.png"), full_page=True)
        for tab, file in [
            ("Statistics", "statistics"),
            ("Filtering", "filtering"),
            ("Frame gallery", "frames"),
            ("Action Insights", "insights"),
            ("Annotations", "annotations"),
            ("Doctor", "doctor"),
        ]:
            page.get_by_role("button", name=tab, exact=True).click()
            if tab == "Statistics":
                page.get_by_text("Episode Length Distribution", exact=False).wait_for()
                results["episode_length_distribution"] = True
            if tab == "Action Insights":
                page.get_by_text("Suggested chunk length:", exact=False).first.wait_for(
                    timeout=120000
                )
                results["analysis_text"] = page.locator("body").inner_text()
            page.get_by_role("button", name="Switch language / 切换语言").click()
            page.wait_for_timeout(600)
            page.screenshot(path=str(output / f"{file}-zh.png"), full_page=True)
            page.get_by_role("button", name="Switch language / 切换语言").click()
        if args.local_repo:
            page.goto(f"{args.base_url}/{args.local_repo}/episode_0")
            page.get_by_role("button", name="Episodes", exact=True).click()
            page.wait_for_function(
                "document.querySelectorAll('video').length >= 1 && [...document.querySelectorAll('video')].every(v => v.readyState >= 2 && v.videoWidth > 0)"
            )
            results["local_videos"] = page.locator("video").evaluate_all(
                "nodes => nodes.map(v => ({width:v.videoWidth,height:v.videoHeight,readyState:v.readyState}))"
            )
        results["page_errors"] = errors
        (output / "browser-smoke.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        assert not errors, errors
        browser.close()
    print(
        json.dumps({k: v for k, v in results.items() if k != "analysis_text"}, indent=2)
    )


if __name__ == "__main__":
    main()
