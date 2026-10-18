"""No UI path can show "[object Object]": every error that reaches an alert or a dialog is
readable text. net.js (api, errorText) runs in node against every kind of error answer the
server gives; a static check covers every alert / dialog / confirm call in the UI scripts."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "asset_pipeline" / "ui" / "static"

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const alerts = [];
const ctx = {console, setTimeout, Promise, JSON, Error, Array, Object, String, Number, Math, alerts, FormData: class FormData {}};
ctx.window = {alert: m => alerts.push(String(m))};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\nthis.api = api; this.errorText = errorText;", ctx);
const cases = JSON.parse(process.argv[2]);
(async () => {
  const out = {};
  for (const [name, c] of Object.entries(cases)) {
    const answers = c.answers.slice();
    ctx.fetch = async () => { const a = answers.shift();
      return {ok: a.status < 400, status: a.status, statusText: a.statusText || "",
              json: async () => { if (a.body === undefined) throw new SyntaxError("not JSON"); return a.body; }}; };
    ctx.offerComfyStart = c.offer === undefined ? undefined : async msg => { out[name + ":offered"] = msg; return c.offer; };
    try { const r = await ctx.api("/x", {}); out[name] = {ok: true, result: r}; }
    catch (e) { out[name] = {ok: false, message: e.message, type: typeof e.message}; }
  }
  for (const [name, v] of Object.entries({obj: {}, nested: {detail: {message: "inner"}}, arr: [{loc: ["body", "n"], msg: "too big"}],
                                          err: new Error("plain"), num: 3, nul: null, deep: {a: {b: 1}}}))
    out["text:" + name] = ctx.errorText(v);
  ctx.window.alert({message: {code: "x"}}); ctx.window.alert({});
  out.alerts = alerts;
  console.log(JSON.stringify(out));
})();
"""

CASES = {
    "string_detail": {"answers": [{"status": 400, "body": {"detail": "no unit nope"}}]},
    "object_detail": {"answers": [{"status": 400, "body": {"detail": {"message": "bad brief", "code": "x"}}}]},
    "object_no_message": {"answers": [{"status": 400, "body": {"detail": {"code": "weird", "n": 3}}}]},
    "pydantic_list": {"answers": [{"status": 422, "body": {"detail": [{"loc": ["body", "n"], "msg": "must be > 0"}]}}]},
    "non_json": {"answers": [{"status": 502, "statusText": "Bad Gateway"}]},
    "empty_json": {"answers": [{"status": 500, "body": {}}]},
    "no_detail_object": {"answers": [{"status": 500, "body": {"error": {"why": "x"}}}]},
    # "+2 more" with ComfyUI down: offer "Start ComfyUI and run"; yes -> the request is sent again
    "comfy_down_start": {"offer": True, "answers": [
        {"status": 409, "body": {"detail": {"code": "comfy_down", "message": "ComfyUI is not running."}}},
        {"status": 200, "body": {"id": 7, "kind": "refs.generate", "status": "queued"}}]},
    "comfy_down_declined": {"offer": False, "answers": [
        {"status": 409, "body": {"detail": {"code": "comfy_down", "message": "ComfyUI is not running."}}}]},
    "comfy_down_no_shell": {"answers": [
        {"status": 409, "body": {"detail": {"code": "comfy_down", "message": {"nested": True}}}}]},
}


@pytest.fixture(scope="module")
def results():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "-e", HARNESS, str(STATIC / "net.js"), json.dumps(CASES)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_no_error_answer_becomes_object_object(results):
    assert "[object Object]" not in json.dumps(results)
    for name, res in results.items():
        if isinstance(res, dict) and res.get("ok") is False:
            assert res["type"] == "string" and res["message"].strip(), (name, res)


def test_error_messages_are_the_real_ones(results):
    assert results["string_detail"]["message"] == "no unit nope"
    assert results["object_detail"]["message"] == "bad brief"
    assert '"code":"weird"' in results["object_no_message"]["message"]
    assert results["pydantic_list"]["message"] == "n: must be > 0"
    assert results["non_json"]["message"] == "Bad Gateway"
    assert results["empty_json"]["message"] == "HTTP 500"
    assert results["text:nested"] == "inner" and results["text:arr"] == "n: too big"
    assert results["alerts"] == ['{"code":"x"}', "{}"]


def test_comfy_down_routes_to_start_comfyui_and_run(results):
    assert results["comfy_down_start:offered"] == "ComfyUI is not running."
    assert results["comfy_down_start"] == {"ok": True, "result": {"id": 7, "kind": "refs.generate", "status": "queued"}}
    assert "Start it from the Status box" in results["comfy_down_declined"]["message"]
    assert "ComfyUI is not running" in results["comfy_down_no_shell"]["message"]


DISPLAY = re.compile(r"\b(alert|confirm|dialog)\(")


def _calls(src: str):
    """Each alert( / confirm( / dialog( call's argument text (balanced parentheses)."""
    for m in DISPLAY.finditer(src):
        i, depth = m.end(), 1
        while i < len(src) and depth:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        yield m.group(1), src[m.end():i - 1], src.count("\n", 0, m.start()) + 1


def test_every_alert_and_dialog_shows_text_not_an_error_object():
    bad = []
    for f in sorted(STATIC.glob("*.js")):
        src = f.read_text()
        for kind, arg, line in _calls(src):
            if kind == "dialog" and arg.lstrip().startswith("{title"):
                checks = re.findall(r"\$\{([^}]*)\}", arg)
            else:
                checks = [arg] + re.findall(r"\$\{([^}]*)\}", arg)
            for expr in checks:
                e = expr.strip()
                # a caught error or a server answer must go through errorText (or be its .message
                # inside a condition, never displayed raw)
                if re.fullmatch(r"(err|e|error|ex|exc|j|r|res|detail|d|s\.error)", e) or re.search(r"\b(err|e)\.message\b", e):
                    bad.append(f"{f.name}:{line}: {kind}({e})")
    assert not bad, "\n".join(bad)
    assert "throw new Error(d" not in (STATIC / "net.js").read_text()


def test_server_error_details_are_text_or_carry_a_message():
    app = (STATIC.parent / "app.py").read_text()
    assert not re.search(r"HTTPException\(\s*\d+\s*,\s*\{", app)          # dict details only via the handler,
    assert '"message":' in app.split("def comfy_down")[1].split("def ")[0]   # which always has a message


# --- no CLI flags in what the UI shows: job errors and API errors pass through ui.messages ---

def _flagged_literals():
    """Every string in the pipeline's code (not the CLI's argparse) that names a --flag: the
    raw material of error messages that the CLI and the UI share."""
    import ast
    root = STATIC.parents[1]
    out = []
    for f in root.rglob("*.py"):
        if f.name == "cli.py" or "ui/messages.py" in f.as_posix():
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            parts = [node.value] if isinstance(node, ast.Constant) and isinstance(node.value, str) else \
                [v.value for v in node.values if isinstance(v, ast.Constant)] if isinstance(node, ast.JoinedStr) else []
            out += [(f.name, p) for p in parts if re.search(r"(?<![\w-])--[a-z]", p)]
    return out


def test_no_error_text_keeps_a_cli_flag():
    from asset_pipeline.ui.messages import app_text
    lits = _flagged_literals()
    assert len(lits) > 5                                           # the scan finds the CLI hints
    bad = [(f, s, app_text(s)) for f, s in lits if "--" in app_text(s)]
    assert not bad, bad
    samples = {
        "give --form and --dims (no catalog form with a generator, or no size)": ("Form", "Size"),
        "give an area (--area NAME) or a district (--district ID)": ("Library › Areas…",),
        "no area 'x': define it with ap area define p x --shots ...": ("New area… in Library › Areas…",),
        "street trees need a city layout (ap site init --city)": ("City tab",),
        "files exist; use --force to rebuild": ("Rebuild unchanged",),
    }
    for msg, want in samples.items():
        t = app_text(msg)
        assert "--" not in t and all(w in t for w in want), (msg, t)


def test_job_and_api_errors_reach_the_ui_without_cli_flags():
    import time
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from asset_pipeline.jobs import JobQueue
    from asset_pipeline.ui import app as ui_app
    q = JobQueue()
    client = TestClient(ui_app.create_app(q))

    def boom():
        raise ValueError("give --form and --dims (no catalog form with a generator, or no size)")
    job = q.submit("x", "", boom, lane="cpu")
    for _ in range(200):
        if q.get(job.id).status == "error":
            break
        time.sleep(0.01)
    err = q.get(job.id).error
    assert "--" not in err and "Form" in err and "Size" in err

    @client.app.get("/api/test/cli-error")
    def cli_error():
        raise HTTPException(400, "nothing to build: run ap library brief p a s front, or use --force")
    r = client.get("/api/test/cli-error")
    assert r.status_code == 400 and "--" not in r.json()["detail"] and "Brief & build" in r.json()["detail"]


def test_ui_scripts_show_no_cli_flags():
    bad = []
    for f in sorted(STATIC.glob("*.js")):
        for kind, arg, line in _calls(f.read_text()):
            if re.search(r"(?<![\w-])--[a-z]", arg):
                bad.append(f"{f.name}:{line}: {kind}(... {arg.strip()[:80]})")
    assert not bad, "\n".join(bad)


def test_ui_text_names_no_cli_commands_or_flags():
    """What the app shows (hints, titles, empty states) names menu items and fields, never `ap`
    commands or --flags; comments and CSS custom properties are not shown text."""
    cmd = re.compile(r"\bap (?:library|ue|site|frames|area|shots|comfy|preflight|vlm|launcher|terrain|refs|plan|style)\b")
    flag = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
    bad = []
    for f in sorted(list(STATIC.glob("*.js")) + list(STATIC.glob("*.html"))):
        src = f.read_text()
        src = re.sub(r"/\*.*?\*/|<!--.*?-->", "", src, flags=re.S)
        for n, ln in enumerate(src.splitlines(), 1):
            code = re.sub(r"(^|\s)//.*$", "", ln)                    # line comments
            code = re.sub(r"var\(--[\w-]+\)|--[\w-]+\s*:", "", code)  # CSS custom properties
            if cmd.search(code) or flag.search(code):
                bad.append(f"{f.name}:{n}: {ln.strip()[:100]}")
    assert not bad, "\n".join(bad)
