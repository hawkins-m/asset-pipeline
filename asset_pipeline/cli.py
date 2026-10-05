"""`ap`: asset pipeline command line. Stage commands are added as stages are built."""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config
from .comfy.client import ComfyClient, ComfyError
from .imagegen.base import GenRequest
from .imagegen.registry import backend_for, make_backend
from .llm.base import LLMError
from .paid import PaidAPIBlocked
from .project import ProjectStore


def _size(s: str) -> tuple[int, int]:
    w, _, h = s.lower().partition("x")
    return int(w), int(h or w)


def cmd_new(a) -> None:
    store = ProjectStore.create(a.slug, name=a.name or "", brief=a.brief or "")
    print(f"created project {a.slug} at {store.root}")


def cmd_comfy_check(a) -> None:
    c = config.backends()["comfyui"]
    stats = ComfyClient(c["url"]).health()
    sysinfo = stats.get("system", {})
    print(f"ComfyUI {sysinfo.get('comfyui_version', '?')} at {c['url']}")
    for d in stats.get("devices", []):
        print(f"  {d.get('name')}: {d.get('vram_free', 0) / 2**30:.1f} / "
              f"{d.get('vram_total', 0) / 2**30:.1f} GB free")


def cmd_gen(a) -> None:
    project = store = None
    if a.project:
        store = ProjectStore.open(a.project)
        project = store.load()
    backend = make_backend(a.backend) if a.backend else backend_for(a.stage, project)
    w, h = _size(a.size)
    anchor = None
    if a.anchor:
        if not (project and project.anchor):
            raise ValueError("--anchor needs --project with a saved style anchor")
        anchor = project.anchor
    req = GenRequest(prompt=a.prompt, width=w, height=h, n=a.n, seed=a.seed, steps=a.steps,
                     anchor=anchor)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = a.out or ((store.root if store else config.ap_root() / "outputs") / "gen" / stamp)
    t = time.time()
    results = backend.generate(req, Path(out))
    elapsed = round(time.time() - t, 1)
    if store:
        store.log_run({"stage": "gen", "backend": backend.name, "request": req.model_dump(mode="json"),
                       "outputs": [str(r.path) for r in results], "seconds": elapsed})
    for r in results:
        print(r.path)
    print(f"{len(results)} image(s) via {backend.name} in {elapsed}s", file=sys.stderr)


def cmd_style_explore(a) -> None:
    from .stages import s0_style
    store = ProjectStore.open(a.project)
    out = s0_style.explore(store, a.brief, n=a.n, seed=a.seed)
    for img in sorted(out.glob("scene_*.png")):
        print(img)


def cmd_style_star(a) -> None:
    from . import review
    store = ProjectStore.open(a.project)
    for p in a.paths:
        print(("unstarred " if a.unstar else "starred ") + review.set_star(store, p, not a.unstar))


def cmd_style_derive(a) -> None:
    from .stages import s0_style
    store = ProjectStore.open(a.project)
    out = s0_style.derive(store, a.scene, [n for n in a.nouns.split(",")], per_noun=a.per_noun,
                          regenerate=not a.no_redraw, style_text=a.style_text or "")
    for img in sorted(out.glob("*.png")):
        print(img)


def cmd_style_anchor(a) -> None:
    from .stages import s0_style
    store = ProjectStore.open(a.project)
    anchor = s0_style.save_anchor(store, strength=a.strength, style_text=a.style_text)
    print(f"anchor: {len(anchor.images)} image(s), strength {anchor.strength}, "
          f"style text {anchor.style_text!r}")


def cmd_style_show(a) -> None:
    from . import review
    from .stages import s0_style
    store = ProjectStore.open(a.project)
    p = store.load()
    print(f"project {p.slug}: brief {p.brief!r}")
    for b in sorted((store.root / s0_style.EXPLORE).glob("batch_*")):
        print(f"  {b.relative_to(store.root)}: {len(list(b.glob('scene_*.png')))} scenes")
    print("starred scenes:", *review.starred(store, s0_style.EXPLORE + "/") or ["(none)"], sep="\n  ")
    print("starred derived:", *review.starred(store, s0_style.DERIVE + "/") or ["(none)"], sep="\n  ")
    if p.anchor:
        print(f"anchor: {len(p.anchor.images)} image(s), strength {p.anchor.strength}, "
              f"style text {p.anchor.style_text!r}")


def cmd_plan_import(a) -> None:
    from .stages import s1_plan
    store = ProjectStore.open(a.project)
    for img in a.images:
        print(s1_plan.import_scene(store, img))


def cmd_plan_analyze(a) -> None:
    from .stages import s1_plan
    store = ProjectStore.open(a.project)
    keys = a.scenes or s1_plan.scenes(store)
    if not keys:
        raise ValueError("no scenes: star stage-0 scenes or `ap plan import` an image")
    for k in keys:
        t = time.time()
        plan = s1_plan.analyze(store, k, force=a.force)
        print(f"{s1_plan.plan_name(plan.scene)}: {len(plan.assets)} assets, "
              f"{len(plan.relations)} relations via {plan.llm} in {time.time() - t:.0f}s")


def cmd_plan_show(a) -> None:
    from .stages import s1_plan
    store = ProjectStore.open(a.project)
    names = [a.name] if a.name else [s1_plan.plan_name(k) for k in s1_plan.scenes(store)]
    for name in names:
        plan = s1_plan.load(store, name)
        if a.json:
            print(json.dumps(plan.model_dump(mode="json") if plan else None, indent=2))
            continue
        if not plan:
            print(f"{name}: not analysed yet\n")
            continue
        print(f"{name}  ({plan.scene}, {plan.llm}{', edited' if plan.edited else ''})")
        print(f"  {plan.summary}\n  scale: {plan.scale_notes}")
        for x in plan.assets:
            d = x.dimensions
            print(f"  {'x' if x.include else '-'} {x.id:<28} {x.category:<10} x{x.count:<3} "
                  f"{d.width:g}x{d.depth:g}x{d.height:g} m  {x.usage}"
                  + (f"  kit={x.kit}" if x.kit else ""))
        for r in plan.relations:
            print(f"    {r.subject} {r.relation} {r.object}")
        print()


def cmd_ui(a) -> None:
    import uvicorn
    from .ui.app import create_app
    print(f"Asset pipeline UI: http://127.0.0.1:{a.port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=a.port, log_level="warning")


VLM_PIDFILE = "vlm.pid"


def cmd_vlm(a) -> None:
    import os
    import signal
    import subprocess
    from .llm.base import LLMError
    from .llm.local import LocalVision
    root = config.ap_root()
    pidfile = root / "logs" / VLM_PIDFILE
    url = config.backends().get("local", {}).get("vlm_url", "")
    if a.action == "status":
        try:
            print(LocalVision(url).health())
        except LLMError as e:
            print(e)
        return
    if a.action == "down":
        if not pidfile.exists():
            print("not running (no pid file)")
            return
        pid = int(pidfile.read_text())
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(100):  # wait so an immediate `up` doesn't find the port taken
                os.kill(pid, 0)
                time.sleep(0.1)
            print(f"vlm server (pid {pid}) did not exit after 10 s")
        except ProcessLookupError:
            print(f"stopped vlm server (pid {pid})")
        pidfile.unlink()
        return
    # up
    try:
        print("already running:", LocalVision(url).health())
        return
    except LLMError:
        pass
    py = root / "envs" / "qwen-vl" / "bin" / "python"
    if not py.exists():
        raise FileNotFoundError(f"{py} missing; run scripts/install_qwen_vl.sh")
    log = (root / "logs" / "vlm.log").open("a")
    env = dict(os.environ, HIP_VISIBLE_DEVICES=str(a.gpu), AP_ROOT=str(root))
    proc = subprocess.Popen([str(py), str(config.REPO_ROOT / "scripts" / "vlm_server.py"),
                             "--idle-unload", str(a.idle_unload)],
                            stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    pidfile.write_text(str(proc.pid))
    for _ in range(300):  # wait until it serves /health (the model itself loads lazily)
        if proc.poll() is not None:
            raise SystemExit(f"vlm server exited with code {proc.returncode}; "
                             f"see {root / 'logs' / 'vlm.log'}")
        try:
            LocalVision(url).health()
            break
        except LLMError:
            time.sleep(0.2)
    else:
        raise SystemExit(f"vlm server not answering at {url} after 60 s")
    print(f"started vlm server (pid {proc.pid}, GPU {a.gpu}); log: {root / 'logs' / 'vlm.log'}")


def cmd_set(a) -> None:
    from .llm.registry import PROVIDERS
    from .paid import paid_allowed
    store = ProjectStore.open(a.project)
    p = store.load()
    if a.llm:
        if a.llm not in PROVIDERS:
            raise ValueError(f"--llm must be one of {', '.join(PROVIDERS)}")
        p.llm = a.llm
    for item in a.backend or []:
        stage, _, name = item.partition("=")
        if name not in ("comfyui", "gemini"):
            raise ValueError(f"--backend {item!r}: expected STAGE=comfyui|gemini")
        p.backends[stage] = name
    store.save(p)
    print(f"{p.slug}: llm={p.llm}, backends={p.backends}")
    paid = [x for x in [p.llm] + list(p.backends.values()) if x in ("gemini", "claude")]
    if paid and not paid_allowed():
        print(f"note: {', '.join(sorted(set(paid)))} are paid APIs and stay blocked unless "
              f"AP_ALLOW_PAID_APIS=1 is set for the run.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ap", description="Asset pipeline (see USAGE.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="create a project under $AP_ROOT/projects/")
    p.add_argument("slug")
    p.add_argument("--name")
    p.add_argument("--brief")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("comfy-check", help="check the ComfyUI server is reachable")
    p.set_defaults(fn=cmd_comfy_check)

    p = sub.add_parser("gen", help="generate images through the image adapter")
    p.add_argument("prompt")
    p.add_argument("-n", type=int, default=1)
    p.add_argument("--seed", type=int)
    p.add_argument("--size", default="1024x1024", help="WxH (default 1024x1024)")
    p.add_argument("--steps", type=int, help="sampler steps (backend default if unset)")
    p.add_argument("--project", help="project slug: use its backends, log to its runs.jsonl")
    p.add_argument("--stage", default="style", help="which stage's backend to use (default style)")
    p.add_argument("--backend", help="override: comfyui | gemini")
    p.add_argument("--anchor", action="store_true", help="apply the project's style anchor")
    p.add_argument("--out", type=Path, help="output dir (default: project or $AP_ROOT/outputs/gen/<time>)")
    p.set_defaults(fn=cmd_gen)

    st = sub.add_parser("style", help="stage 0: explore scenes, derive the style anchor")
    ssub = st.add_subparsers(dest="style_cmd", required=True)
    p = ssub.add_parser("explore", help="generate scene concepts from the brief")
    p.add_argument("project")
    p.add_argument("--brief", help="defaults to the project's brief")
    p.add_argument("-n", type=int, default=8)
    p.add_argument("--seed", type=int)
    p.set_defaults(fn=cmd_style_explore)
    p = ssub.add_parser("star", help="star (or --unstar) images by path")
    p.add_argument("project")
    p.add_argument("paths", nargs="+")
    p.add_argument("--unstar", action="store_true")
    p.set_defaults(fn=cmd_style_star)
    p = ssub.add_parser("derive", help="cut objects from a starred scene and redraw them on white")
    p.add_argument("project")
    p.add_argument("scene", help="scene image path (absolute or project-relative)")
    p.add_argument("--nouns", required=True, help="comma-separated objects, e.g. 'boat,house'")
    p.add_argument("--per-noun", type=int, default=2)
    p.add_argument("--style-text", help="defaults to the current anchor's style text")
    p.add_argument("--no-redraw", action="store_true", help="only cut out, don't regenerate")
    p.set_defaults(fn=cmd_style_derive)
    p = ssub.add_parser("anchor", help="save starred derived objects as the style anchor")
    p.add_argument("project")
    p.add_argument("--strength", type=float, default=0.06)
    p.add_argument("--style-text", help="keeps the current style text if omitted")
    p.set_defaults(fn=cmd_style_anchor)
    p = ssub.add_parser("show", help="list batches, stars and the anchor")
    p.add_argument("project")
    p.set_defaults(fn=cmd_style_show)

    pl = sub.add_parser("plan", help="stage 1: scene -> asset plan via the vision LLM")
    psub = pl.add_subparsers(dest="plan_cmd", required=True)
    p = psub.add_parser("import", help="copy outside scene images into the project's scenes/")
    p.add_argument("project")
    p.add_argument("images", nargs="+", type=Path)
    p.set_defaults(fn=cmd_plan_import)
    p = psub.add_parser("analyze", help="draft plans (default: every starred/imported scene)")
    p.add_argument("project")
    p.add_argument("scenes", nargs="*", help="scene paths (absolute or project-relative)")
    p.add_argument("--force", action="store_true", help="replace plans edited in the UI")
    p.set_defaults(fn=cmd_plan_analyze)
    p = psub.add_parser("show", help="print plans")
    p.add_argument("project")
    p.add_argument("name", nargs="?", help="one plan, e.g. batch_001__scene_002")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan_show)

    p = sub.add_parser("ui", help="start the local review UI (127.0.0.1 only)")
    p.add_argument("--port", type=int, default=8700)
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("vlm", help="local vision LLM server (Qwen3-VL on GPU 0)")
    p.add_argument("action", choices=["up", "down", "status"])
    p.add_argument("--gpu", type=int, default=0)
    p.add_argument("--idle-unload", type=float, default=600,
                   help="free the GPU after this many idle seconds (0 = never)")
    p.set_defaults(fn=cmd_vlm)

    p = sub.add_parser("set", help="project settings: vision LLM and per-stage image backend")
    p.add_argument("project")
    p.add_argument("--llm", help="local | gemini | claude")
    p.add_argument("--backend", action="append", help="STAGE=comfyui|gemini (repeatable)")
    p.set_defaults(fn=cmd_set)

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (ComfyError, LLMError, PaidAPIBlocked, FileNotFoundError, FileExistsError,
            ValueError, NotImplementedError) as e:
        sys.exit(f"ap: {e}")


if __name__ == "__main__":
    main()
