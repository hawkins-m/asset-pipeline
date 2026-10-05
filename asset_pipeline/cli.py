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


def cmd_ui(a) -> None:
    import uvicorn
    from .ui.app import create_app
    print(f"Asset pipeline UI: http://127.0.0.1:{a.port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=a.port, log_level="warning")


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

    p = sub.add_parser("ui", help="start the local review UI (127.0.0.1 only)")
    p.add_argument("--port", type=int, default=8700)
    p.set_defaults(fn=cmd_ui)

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (ComfyError, FileNotFoundError, FileExistsError, ValueError, NotImplementedError) as e:
        sys.exit(f"ap: {e}")


if __name__ == "__main__":
    main()
