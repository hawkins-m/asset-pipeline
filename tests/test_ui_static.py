"""The UI always serves the repo's current code: index.html is never cached, every static URL in
it carries a content hash, and /api/version tells an open page that the code changed."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from asset_pipeline.jobs import JobQueue
from asset_pipeline.ui import app as ui_app


def _client():
    return TestClient(ui_app.create_app(JobQueue()))


def test_index_is_no_store_and_versions_every_static_url():
    r = _client().get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', r.text)
    assert refs and all(re.search(r"\?v=[0-9a-f]{8}$", u) for u in refs), refs
    assert f'<meta name="ap-build" content="{ui_app.build_id()}">' in r.text


def test_versioned_assets_are_immutable_and_plain_ones_revalidate():
    c = _client()
    js = re.search(r'src="(/static/shell\.js\?v=[0-9a-f]+)"', c.get("/").text).group(1)
    assert "immutable" in c.get(js).headers["cache-control"]
    plain = c.get("/static/shell.js")
    assert plain.headers["cache-control"] == "no-cache" and plain.headers.get("etag")


def test_a_changed_static_file_gets_a_new_url_and_build(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text('<head></head><script src="/static/a.js"></script>')
    (static / "a.js").write_text("one")
    monkeypatch.setattr(ui_app, "STATIC", static)
    first, b1 = ui_app.index_html(), ui_app.build_id()
    (static / "a.js").write_text("two")
    assert ui_app.index_html() != first and ui_app.build_id() != b1   # no server restart needed


def test_version_endpoint_names_the_repo_code():
    v = _client().get("/api/version").json()
    assert v["build"] == ui_app.build_id()
    assert (Path(v["code"]) / "asset_pipeline" / "ui" / "app.py").resolve() == Path(ui_app.__file__).resolve()
