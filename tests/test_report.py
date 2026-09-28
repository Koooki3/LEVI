"""Technical report routes: a read-only view of LEVI_REPORT_DIR."""

import json

import pytest


@pytest.fixture
def report(tmp_path, monkeypatch):
    root = tmp_path / "outside" / "report"
    (root / "assets").mkdir(parents=True)
    (root / "LEVI.md").write_text("# Report\n\nEnglish body\n")
    (root / "LEVI.zh-CN.md").write_text("# 报告\n\n中文正文\n")
    (root / "status.json").write_text(
        json.dumps({"schema": "levi.report.status.v1", "levi_main": "abc1234"})
    )
    (root / "assets/ui.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "assets/diagram.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'/>"
    )
    (root / "assets/notes.md").write_text("secret")
    (root / "secret.png").write_bytes(b"\x89PNG")
    monkeypatch.setenv("LEVI_REPORT_DIR", str(root))
    return root


def test_unconfigured_report_is_an_explained_empty_state(client, monkeypatch):
    monkeypatch.delenv("LEVI_REPORT_DIR", raising=False)
    body = client.get("/api/levi/report").json()
    assert body["configured"] is False
    assert body["markdown"] is None and body["status"] is None
    assert client.get("/api/levi/report/assets/ui.png").status_code == 404


def test_missing_folder_is_reported_not_raised(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEVI_REPORT_DIR", str(tmp_path / "nowhere"))
    body = client.get("/api/levi/report").json()
    assert body["configured"] is True and body["exists"] is False
    assert body["dir"] == str(tmp_path / "nowhere")


def test_language_and_english_fallback(client, report):
    en = client.get("/api/levi/report?lang=en").json()
    assert en["markdown"].startswith("# Report") and en["document_lang"] == "en"
    assert en["status"]["levi_main"] == "abc1234" and en["errors"] == []
    zh = client.get("/api/levi/report?lang=zh").json()
    assert zh["markdown"].startswith("# 报告") and zh["document_lang"] == "zh"
    (report / "LEVI.zh-CN.md").unlink()
    fallback = client.get("/api/levi/report?lang=zh").json()
    assert fallback["lang"] == "zh" and fallback["document_lang"] == "en"
    assert fallback["markdown"].startswith("# Report")
    # Unknown languages read as English.
    assert client.get("/api/levi/report?lang=fr").json()["lang"] == "en"


def test_broken_status_keeps_the_document(client, report):
    (report / "status.json").write_text("{not json")
    body = client.get("/api/levi/report").json()
    assert body["markdown"].startswith("# Report")
    assert body["status"] is None
    assert body["errors"] and body["errors"][0].startswith("status.json")


def test_etag_changes_with_the_files_and_answers_304(client, report):
    first = client.get("/api/levi/report?lang=en")
    etag = first.headers["etag"]
    assert first.json()["etag"] == etag
    assert client.get("/api/levi/report/version?lang=en").json()["etag"] == etag
    again = client.get("/api/levi/report?lang=en", headers={"If-None-Match": etag})
    assert again.status_code == 304
    # Languages have their own marker (the document differs).
    assert client.get("/api/levi/report/version?lang=zh").json()["etag"] != etag
    (report / "status.json").write_text(json.dumps({"levi_main": "def5678"}))
    changed = client.get("/api/levi/report/version?lang=en").json()["etag"]
    assert changed != etag
    fresh = client.get("/api/levi/report?lang=en", headers={"If-None-Match": etag})
    assert fresh.status_code == 200 and fresh.json()["status"]["levi_main"] == "def5678"


def test_assets_are_confined_to_the_assets_folder(client, report, tmp_path):
    ok = client.get("/api/levi/report/assets/ui.png")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    svg = client.get("/api/levi/report/assets/diagram.svg")
    assert svg.status_code == 200
    assert "sandbox" in svg.headers["content-security-policy"]
    assert svg.headers["x-content-type-options"] == "nosniff"
    # Not an image type, outside assets/, traversal, hidden, missing.
    assert client.get("/api/levi/report/assets/notes.md").status_code == 403
    assert client.get("/api/levi/report/assets/..%2Fsecret.png").status_code in (
        403,
        404,
    )
    assert client.get("/api/levi/report/assets/.hidden.png").status_code == 403
    assert client.get("/api/levi/report/assets/none.png").status_code == 404
    # A symlink that leaves the folder is refused, even to an image.
    (tmp_path / "elsewhere.png").write_bytes(b"\x89PNG")
    (report / "assets/link.png").symlink_to(tmp_path / "elsewhere.png")
    assert client.get("/api/levi/report/assets/link.png").status_code == 403


def test_asset_resolution_rejects_traversal_directly(report):
    from levi import report as technical_report

    for bad in (
        "../secret.png",
        "/etc/passwd.png",
        "a/../../secret.png",
        "",
        "x\\y.png",
    ):
        with pytest.raises(PermissionError):
            technical_report.asset(bad)


def test_report_is_read_only(client, report):
    before = sorted((p.name, p.stat().st_mtime_ns) for p in report.rglob("*"))
    client.get("/api/levi/report?lang=zh")
    client.get("/api/levi/report/version")
    client.get("/api/levi/report/assets/ui.png")
    assert sorted((p.name, p.stat().st_mtime_ns) for p in report.rglob("*")) == before
    assert client.post("/api/levi/report").status_code == 405
