"""Opt-in production UI test. All model/service responses are fixtures.

Build first; run with LEVI_BROWSER_TESTS=1 and LEVI_CHROMIUM_EXECUTABLE set to an
installed Chromium. This never starts the backend, Ollama or an accelerator.
"""

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("LEVI_BROWSER_TESTS") != "1", reason="Opt-in local browser acceptance"
)


def test_local_model_connection_download_and_bilingual_ui(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    project = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = (tmp_path / "frontend.log").open("w")
    server = subprocess.Popen(
        [
            str(project / ".runtime/bun-linux-x64/bun"),
            "node_modules/next/dist/bin/next",
            "start",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=project,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env={**os.environ, "NEXT_TELEMETRY_DISABLED": "1"},
    )
    origin = f"http://127.0.0.1:{port}"
    providers = []
    downloads = []
    calls = []
    try:
        for _ in range(100):
            if server.poll() is not None:
                pytest.fail(
                    "Frontend exited: " + (tmp_path / "frontend.log").read_text()
                )
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=os.environ["LEVI_CHROMIUM_EXECUTABLE"],
                headless=True,
                args=["--disable-gpu"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route(request):
                url = request.request.url
                if not url.startswith(origin + "/"):
                    request.abort()
                    return
                path = urlsplit(url).path
                if not path.startswith("/api/"):
                    request.continue_()
                    return
                calls.append((request.request.method, path))
                payload = {}
                if path.endswith("/providers"):
                    if request.request.method == "POST":
                        data = request.request.post_data_json
                        providers[:] = [
                            {
                                **data,
                                "enabled": True,
                                "credential_ready": True,
                                "credential_source": "not_required",
                            }
                        ]
                        payload = {"ok": True}
                    else:
                        payload = providers
                elif path.endswith("/ollama/download"):
                    downloads[:] = [
                        {
                            "id": "model-fixture",
                            "provider": "ollama-local",
                            "model": "qwen3.5:4b",
                            "status": "running",
                            "cancel_requested": False,
                            "progress": {
                                "status": "pulling",
                                "total": 100,
                                "completed": 50,
                            },
                        }
                    ]
                    payload = downloads[0]
                elif path.endswith("/ollama"):
                    payload = {
                        "version": "fixture",
                        "model": None,
                        "models": [],
                        "capabilities": [],
                        "digest_matches": False,
                    }
                elif path.endswith("/model-downloads"):
                    payload = downloads
                elif path.endswith("/cancel"):
                    downloads[0].update(status="cancelled", cancel_requested=True)
                    payload = downloads[0]
                elif path.endswith("/connections"):
                    payload = {
                        "external": {
                            "configured": False,
                            "enabled": False,
                            "datasets": [],
                        }
                    }
                elif path.endswith(("/runs", "/runtimes", "/grants", "/datasets")):
                    payload = []
                request.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload),
                )

            page.route("**/*", route)
            page.goto(origin, wait_until="networkidle")
            page.evaluate("window.dispatchEvent(new Event('levi-agent-connections'))")
            page.get_by_role("button", name="Model settings", exact=True).click()
            page.get_by_label("Connection type").select_option("ollama")
            expect(page.get_by_label("Model ID", exact=True)).to_have_value(
                "qwen3.5:4b"
            )
            page.get_by_role("button", name="Save model settings", exact=True).click()
            page.get_by_label("Agent Workbench", exact=True).get_by_role(
                "button", name="Accounts & connections", exact=True
            ).click()
            expect(page.get_by_text("No API key required", exact=False)).to_be_visible()
            page.get_by_role("button", name="Inspect local models", exact=True).click()
            expect(page.get_by_text("Model not installed", exact=False)).to_be_visible()
            assert not any(path.endswith("/download") for _, path in calls)
            page.get_by_text("Download model explicitly", exact=True).click()
            expect(
                page.get_by_role("button", name="Start model download", exact=True)
            ).to_be_disabled()
            page.get_by_label(
                "I authorize this model download and have reviewed its license"
            ).check()
            page.get_by_role("button", name="Start model download", exact=True).click()
            expect(page.get_by_role("progressbar")).to_have_attribute("value", "50")
            page.screenshot(path=str(tmp_path / "ollama-en.png"), full_page=True)
            page.get_by_role("button", name="Cancel download", exact=True).click()
            expect(
                page.get_by_text("Cancellation requested", exact=False)
            ).to_be_visible()
            page.evaluate("localStorage.setItem('levi-language','zh')")
            page.reload(wait_until="networkidle")
            page.evaluate("window.dispatchEvent(new Event('levi-agent-connections'))")
            expect(
                page.get_by_role("button", name="检查本地模型", exact=True)
            ).to_be_visible()
            page.screenshot(path=str(tmp_path / "ollama-zh.png"), full_page=True)
            assert errors == []
            browser.close()
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=20)
        log.close()
