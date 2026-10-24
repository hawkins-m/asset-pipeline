"""ap models: the manifest, status, and downloads against a tiny fake hub served on localhost
(Range requests, a gated repo). Nothing real is downloaded."""
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from asset_pipeline import cli, config, models

A = b"tiny weights " * 5000                    # 65 kB, an "LFS" file (sha256)
B = b'{"tiny": true}\n'                         # a small file (git blob sha1)
C = b"cached " * 3000
G = b"gated " * 100


def git_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Hub:
    """repo/resolve/rev/path -> bytes; counts requests and Range starts; some repos need a token."""

    def __init__(self):
        self.files = {"org/tiny": {"w.safetensors": A, "config.json": B}, "org/cached": {"m.safetensors": C},
                      "org/gated": {"g.safetensors": G}}
        self.gated = {"org/gated": "secret-token"}
        self.ranges, self.requests = [], 0
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hub.requests += 1
                parts = self.path.lstrip("/").split("/")
                repo, path = "/".join(parts[:2]), "/".join(parts[4:])
                tok = hub.gated.get(repo)
                if tok and self.headers.get("Authorization") != f"Bearer {tok}":
                    self.send_response(401 if not self.headers.get("Authorization") else 403)
                    self.end_headers()
                    return
                data = hub.files.get(repo, {}).get(path)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.split("=")[1].split("-")[0])
                    hub.ranges.append(start)
                    self.send_response(206)
                    body = data[start:]
                else:
                    self.send_response(200)
                    body = data
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


MANIFEST = """
[[model]]
id = "tiny"
title = "Tiny"
used_by = "tests"
feature = "segdepth"
need = "required"
license = "MIT"
repo = "org/tiny"
revision = "r1"
gated = false
root = "models"
vram_gb = 1
files = [
  {{ src = "w.safetensors", dest = "tiny/w.safetensors", size = {a}, sha256 = "{sa}" }},
  {{ src = "config.json", dest = "tiny/config.json", size = {b}, git_sha1 = "{sb}" }},
]

[[model]]
id = "tiny-cache"
title = "Tiny cached"
used_by = "tests"
feature = "trellis"
need = "optional"
license = "MIT"
repo = "org/cached"
revision = "r2"
gated = false
root = "hf_cache"
vram_gb = 0
files = [ {{ src = "m.safetensors", dest = "m.safetensors", size = {c}, sha256 = "{sc}" }} ]

[[model]]
id = "tiny-gated"
title = "Tiny gated"
used_by = "tests"
feature = "comfy"
need = "optional"
license = "Non-Commercial"
repo = "org/gated"
revision = "r3"
gated = "auto"
root = "comfyui"
vram_gb = 0
files = [ {{ src = "g.safetensors", dest = "checkpoints/g.safetensors", size = {g}, sha256 = "{sg}" }} ]
"""


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h = Hub()
    man = tmp_path / "models.toml"
    man.write_text(MANIFEST.format(a=len(A), sa=hashlib.sha256(A).hexdigest(), b=len(B), sb=git_sha1(B),
                                   c=len(C), sc=hashlib.sha256(C).hexdigest(), g=len(G), sg=hashlib.sha256(G).hexdigest()))
    monkeypatch.setattr(models, "MANIFEST", man)
    monkeypatch.setenv("AP_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("HF_ENDPOINT", h.url)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "no-hf-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))         # never this machine's `hf auth login` token
    monkeypatch.setattr(models, "DISK_MARGIN", 0)
    config.save_user({"comfyui": {"models_dir": str(tmp_path / "comfy")}})
    yield h
    h.srv.shutdown()


def events(capsys):
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]


def test_download_installs_and_verifies(hub, tmp_path, capsys):
    assert models.status(models.get("tiny"))["state"] == "missing"
    cli.main(["models", "download", "tiny", "tiny-cache", "--json"])
    ev = events(capsys)
    kinds = [e["event"] for e in ev]
    assert kinds[0] == "plan" and kinds[-1] == "done" and kinds.count("model_done") == 2 and "error" not in kinds
    plan = ev[0]
    assert plan["bytes"] == len(A) + len(B) + len(C) and plan["files"] == 3
    prog = [e for e in ev if e["event"] == "progress"]
    assert prog and all({"done", "total", "overall_done", "overall_total", "rate_mbs"} <= e.keys() for e in prog)
    assert (tmp_path / "root/models/tiny/w.safetensors").read_bytes() == A
    assert (tmp_path / "root/models/tiny/config.json").read_bytes() == B
    snap = tmp_path / "root/hf/hub/models--org--cached/snapshots/r2/m.safetensors"
    assert snap.is_symlink() and snap.read_bytes() == C                      # the Hugging Face cache layout
    assert (tmp_path / "root/hf/hub/models--org--cached/refs/main").read_text() == "r2"
    assert models.status(models.get("tiny"))["state"] == "installed"
    n = hub.requests
    cli.main(["models", "download", "tiny", "--json"])                       # installed: nothing fetched
    assert hub.requests == n and [e["event"] for e in events(capsys)] == ["skip", "plan", "done"]
    assert models.verify([models.get("tiny"), models.get("tiny-cache")]) == []


def test_resume_continues_a_partial_file(hub, tmp_path, capsys):
    part = tmp_path / "root/models/tiny/w.safetensors.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(A[:20000])
    assert models.status(models.get("tiny"))["state"] == "partial"
    res = models.download([models.get("tiny")], models.Events(as_json=True))
    ev = events(capsys)
    assert res["installed"] == ["tiny"] and hub.ranges == [20000]
    assert ev[0]["resume_bytes"] == 20000
    assert next(e for e in ev if e["event"] == "file_start" and e["file"].endswith("w.safetensors"))["resume_from"] == 20000
    assert (tmp_path / "root/models/tiny/w.safetensors").read_bytes() == A and not part.exists()


def test_a_corrupt_download_fails_its_checksum(hub, tmp_path, capsys):
    hub.files["org/tiny"]["w.safetensors"] = b"x" * len(A)                    # same size, wrong bytes
    with pytest.raises(SystemExit):
        cli.main(["models", "download", "tiny"])
    out = capsys.readouterr().out
    assert "FAILED    tiny:" in out and "doesn't match" in out
    assert not (tmp_path / "root/models/tiny/w.safetensors").exists()
    assert not (tmp_path / "root/models/tiny/w.safetensors.part").exists()   # removed: the next run starts clean


def test_gated_models_say_exactly_what_to_do(hub, tmp_path, monkeypatch, capsys):
    res = models.download([models.get("tiny-gated")], models.Events(as_json=True))
    err = next(e for e in events(capsys) if e["event"] == "error")
    assert res["failed"] == ["tiny-gated"] and hub.requests == 0 and err["kind"] == "gated"
    for words in ("accept its license", f"https://huggingface.co/org/gated", "settings/tokens", "hf auth login", "HF_TOKEN"):
        assert words in err["fix"]
    monkeypatch.setenv("HF_TOKEN", "someone-elses")                          # a token without access: 403
    models.download([models.get("tiny-gated")], models.Events(as_json=True))
    err = next(e for e in events(capsys) if e["event"] == "error")
    assert err["kind"] == "gated" and "must belong to the account" in err["fix"]
    config.save_secret("HF_TOKEN", "secret-token")                           # the token from Setup (secrets.toml)
    monkeypatch.delenv("HF_TOKEN")
    assert models.download([models.get("tiny-gated")], models.Events(as_json=True))["installed"] == ["tiny-gated"]
    assert (tmp_path / "comfy/checkpoints/g.safetensors").read_bytes() == G


def test_disk_space_is_checked_before_anything_is_fetched(hub, monkeypatch, capsys):
    import shutil
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(10**9, 10**9, 1000))
    res = models.download([models.get("tiny")], models.Events(as_json=True))
    ev = events(capsys)
    assert res["failed"] == ["tiny"] and hub.requests == 0
    assert any(e["event"] == "error" and e["kind"] == "disk" for e in ev)


def test_dry_run_and_comfy_folder_not_set(hub, capsys):
    cli.main(["models", "download", "--required", "--dry-run"])
    assert "2 file(s) to get from 1 model(s)" in capsys.readouterr().out and hub.requests == 0
    config.save_user({}, remove=["comfyui.models_dir"])
    assert models.status(models.get("tiny-gated"))["state"] == "no_root"
    res = models.download([models.get("tiny-gated")], models.Events(as_json=True))
    assert res["failed"] == ["tiny-gated"] and "models_dir" in events(capsys)[0]["fix"]


def test_list_text_and_json(hub, capsys):
    cli.main(["models", "list"])
    out = capsys.readouterr().out
    assert "tiny" in out and "Required: 0 of 1 installed" in out and "ap models download --required" in out
    cli.main(["models", "list", "--json"])
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["tiny-gated"]["gated"] == "auto" and rows["tiny"]["state"] == "missing" and rows["tiny"]["size"] == len(A) + len(B)


def test_unknown_names_and_no_selection(hub):
    with pytest.raises(ValueError, match="unknown model"):
        models.select(["nope"])
    with pytest.raises(ValueError, match="--required"):
        models.select([])


def test_the_real_manifest_is_complete():
    ms = models.load()
    assert len({m.id for m in ms}) == len(ms)
    for m in ms:
        assert m.need in models.NEEDS and m.root in ("comfyui", "models", "hf_cache") and m.license and m.revision
        assert m.files and all(f.size > 0 and (f.sha256 or f.git_sha1) for f in m.files), m.id
        assert m.need != "local.model" or m.select, m.id
    vlm = {m.select for m in ms if m.need == "local.model"}
    assert set(config.backends()["local"]["models"]) <= vlm       # every configured vision model has an entry
