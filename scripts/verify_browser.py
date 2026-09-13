"""Opt-in Chromium smoke check against an already running LEVI service.

Runs public demo reads; screenshots and results stay inside the workspace.
The browser context is intentionally standalone so iframe-only assumptions and
native browser shortcut regressions are exercised too.
"""

import argparse
import json

from playwright.sync_api import sync_playwright

from levi.paths import ROOT, configure, inside


def wait_for_language(page, language):
    page.wait_for_function(
        "(expected) => document.documentElement.lang === expected",
        arg=language,
    )


def click_language_switch(page, target):
    if target == "zh":
        page.get_by_role("button", name="Switch to Chinese", exact=True).click()
        wait_for_language(page, "zh-CN")
    else:
        page.get_by_role("button", name="切换到英文", exact=True).click()
        wait_for_language(page, "en")


def dispatch_shortcut(page, key, ctrl=False, meta=False):
    return page.evaluate(
        """({key, ctrl, meta}) => {
          const event = new KeyboardEvent("keydown", {
            key,
            code: key === "s" ? "KeyS" : key === "z" ? "KeyZ" : "KeyY",
            ctrlKey: ctrl,
            metaKey: meta,
            bubbles: true,
            cancelable: true,
          });
          const dispatched = window.dispatchEvent(event);
          return event.defaultPrevented && !dispatched;
        }""",
        {"key": key, "ctrl": ctrl, "meta": meta},
    )


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
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(60000)
        page.goto(args.base_url)
        wait_for_language(page, "en")
        results["standalone_browser"] = page.evaluate("window.parent === window")
        assert results["standalone_browser"] is True
        page.wait_for_function("document.body.innerText.includes('Every motion.')")
        page.locator(
            ".levi-demo-card video, .levi-dataset-card video, video"
        ).first.wait_for()
        page.wait_for_function(
            "document.querySelectorAll('video').length === 2 && [...document.querySelectorAll('video')].every(v => v.readyState >= 2)",
            timeout=120000,
        )
        page.screenshot(path=str(output / "home-en.png"), full_page=True)

        click_language_switch(page, "zh")
        page.wait_for_function("document.body.innerText.includes('每一次动作，')")
        page.screenshot(path=str(output / "home-zh.png"), full_page=True)
        click_language_switch(page, "en")
        page.reload()
        wait_for_language(page, "en")
        results["english_persists"] = True

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
        page.get_by_role("button", name="Annotations", exact=True).click()
        page.get_by_text("Language annotations", exact=True).wait_for()

        # The subtask track accepts a real pointer drag and opens the shared
        # label popup. Verify the screenshot-requested center + drag behavior
        # before canceling the draft.
        track = page.locator(".tl .track").nth(1)
        track_box = track.bounding_box()
        assert track_box
        drag_y = track_box["y"] + track_box["height"] / 2
        drag_x1 = track_box["x"] + track_box["width"] * 0.18
        drag_x2 = track_box["x"] + track_box["width"] * 0.72
        page.mouse.move(drag_x1, drag_y)
        page.mouse.down()
        page.mouse.move(drag_x2, drag_y, steps=4)
        page.mouse.up()
        popup = page.locator(".quick-popup")
        popup.wait_for()
        initial_popup = popup.bounding_box()
        assert initial_popup
        initial_center = (
            initial_popup["x"] + initial_popup["width"] / 2,
            initial_popup["y"] + initial_popup["height"] / 2,
        )
        results["popup_centered"] = (
            abs(initial_center[0] - 720) <= 3 and abs(initial_center[1] - 500) <= 3
        )
        assert results["popup_centered"]
        handle = popup.locator(".quick-popup-drag-handle")
        handle_box = handle.bounding_box()
        assert handle_box
        handle_x = handle_box["x"] + handle_box["width"] / 2
        handle_y = handle_box["y"] + handle_box["height"] / 2
        page.mouse.move(handle_x, handle_y)
        page.mouse.down()
        page.mouse.move(handle_x + 120, handle_y + 80, steps=5)
        page.mouse.up()
        page.wait_for_timeout(150)
        moved_popup = popup.bounding_box()
        assert moved_popup
        results["popup_draggable"] = (
            moved_popup["x"] > initial_popup["x"] + 80
            and moved_popup["y"] > initial_popup["y"] + 40
        )
        assert results["popup_draggable"]
        popup.locator(".quick-popup-actions button").first.click()
        assert popup.count() == 0

        results["ctrl_s_prevented"] = dispatch_shortcut(page, "s", ctrl=True)
        results["cmd_s_prevented"] = dispatch_shortcut(page, "s", meta=True)
        results["ctrl_z_prevented"] = dispatch_shortcut(page, "z", ctrl=True)
        results["ctrl_y_prevented"] = dispatch_shortcut(page, "y", ctrl=True)
        assert results["ctrl_s_prevented"] is True
        assert results["cmd_s_prevented"] is True
        assert results["ctrl_z_prevented"] is True
        assert results["ctrl_y_prevented"] is True
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
            click_language_switch(page, "zh")
            page.screenshot(path=str(output / f"{file}-zh.png"), full_page=True)
            click_language_switch(page, "en")

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
        context.close()
        browser.close()
    print(
        json.dumps({k: v for k, v in results.items() if k != "analysis_text"}, indent=2)
    )


if __name__ == "__main__":
    main()
