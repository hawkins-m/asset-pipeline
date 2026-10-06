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
        name = s1_plan.plan_name(plan.scene)
        print(f"{name}: {len(plan.assets)} assets, "
              f"{len(plan.relations)} relations via {plan.llm} in {time.time() - t:.0f}s")
        if not a.no_refine:
            _refine(store, name)


def _refine(store, name: str) -> None:
    from .stages import s1_plan
    t = time.time()
    try:
        plan = s1_plan.refine_boxes(store, name)
    except ComfyError as e:
        print(f"  boxes left as the LLM drew them ({e}); `ap plan refine` later")
        return
    n = sum(x.bbox_source == "sam" for x in plan.assets)
    print(f"  SAM 3.1 boxes for {n}/{len(plan.assets)} assets in {time.time() - t:.0f}s")


def cmd_plan_refine(a) -> None:
    from .stages import s1_plan
    store = ProjectStore.open(a.project)
    names = [a.name] if a.name else [s1_plan.plan_name(k) for k in s1_plan.scenes(store)
                                     if s1_plan.load(store, s1_plan.plan_name(k))]
    for name in names:
        print(name)
        _refine(store, name)


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
                  f"{d.width:g}x{d.depth:g}x{d.height:g} m  {x.usage}  box={x.bbox_source}"
                  + (f" (SAM found {x.sam_found})" if x.sam_found is not None else "")
                  + (f"  kit={x.kit}" if x.kit else ""))
        for r in plan.relations:
            print(f"    {r.subject} {r.relation} {r.object}")
        print()


def cmd_refs_generate(a) -> None:
    from .stages import s2_refs
    store = ProjectStore.open(a.project)
    if not a.unit:
        paths = s2_refs.generate_missing(store, n=a.n, plan=a.plan)
    else:
        by_key = {(u.plan, u.key): u for u in s2_refs.units(store, a.plan)}
        paths = []
        for key in a.unit:
            matches = [u for (plan, k), u in by_key.items() if k == key]
            if not matches:
                raise ValueError(f"no unit {key!r} (see `ap refs show`)")
            for u in matches:
                paths += s2_refs.generate(store, u, n=a.n, seed=a.seed)
    for p in paths:
        print(p)
    if not paths:
        print("nothing to do: every unit has sheets (name units with --unit to add more)")


def cmd_refs_show(a) -> None:
    from . import review
    from .stages import s2_refs
    store = ProjectStore.open(a.project)
    stars = review.load(store)["stars"]
    for u in s2_refs.units(store, a.plan):
        sh = s2_refs.sheets(store, u)
        print(f"{u.plan}/{u.key:<32} {u.kind:<8} {len(sh)} sheet(s), "
              f"{sum(1 for k in sh if stars.get(k))} starred  [{', '.join(x.id for x in u.assets)}]")


def cmd_views_cut(a) -> None:
    from .stages import s3_views
    store = ProjectStore.open(a.project)
    if a.sheets:
        for sheet in a.sheets:
            for v in s3_views.cut(store, sheet):
                print(f"{v['file']}  asset={v['asset']} asked={v['asked']} ({v['method']})")
    else:
        print(f"{s3_views.cut_starred(store)} view(s) cut")


def cmd_review_show(a) -> None:
    from . import review
    from .stages import s1_plan, s3_views, s5_3d
    store = ProjectStore.open(a.project)
    for name in [s1_plan.plan_name(k) for k in s1_plan.scenes(store)]:
        plan = s1_plan.load(store, name)
        for x in (plan.assets if plan else []):
            if not x.include or x.category == "terrain":
                continue
            views = s3_views.views(store, name, x.id)
            chosen = review.chosen(store, name, x.id)
            print(f"{name}/{x.id:<30} {x.usage:<5} {len(views)} view(s)  chosen={chosen or '-'}  "
                  f"3d={len(s5_3d.results(store, name, x.id))}")


def cmd_review_choose(a) -> None:
    from . import review
    store = ProjectStore.open(a.project)
    print(review.choose(store, a.plan, a.asset, a.view))


def cmd_review_tag(a) -> None:
    from .stages import s1_plan
    store = ProjectStore.open(a.project)
    s1_plan.set_usage(store, a.plan, a.asset, a.usage)
    print(f"{a.plan}/{a.asset}: {a.usage}")


def cmd_3d(a) -> None:
    from .stages import s5_3d
    store = ProjectStore.open(a.project)
    rec = s5_3d.run(store, a.plan, a.asset, view=a.view, mode=a.mode, seed=a.seed)
    print(store.root / rec["glb"])


def cmd_ui(a) -> None:
    import uvicorn
    from .ui.app import create_app
    print(f"Asset pipeline UI: http://127.0.0.1:{a.port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=a.port, log_level="warning")


def cmd_vlm(a) -> None:
    from . import vlm
    if a.action == "status":
        print(json.dumps(vlm.status(), indent=2))
    elif a.action == "down":
        print(vlm.down(gpu=a.gpu if a.gpu_given else None, force=a.force))
    else:
        st = vlm.up(a.model, gpu=a.gpu, idle_unload=a.idle_unload)
        print(f"vlm {st['model']} up on GPU {st['gpu']} at {st['url']} (pid {st['pid']}); "
              f"log: {config.ap_root() / 'logs' / ('vlm-' + st['model'] + '.log')}")


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
    p.add_argument("--no-refine", action="store_true",
                   help="keep the LLM's boxes (default: SAM 3.1 boxes if ComfyUI is up)")
    p.set_defaults(fn=cmd_plan_analyze)
    p = psub.add_parser("refine", help="redo asset boxes with SAM 3.1 (needs ComfyUI)")
    p.add_argument("project")
    p.add_argument("name", nargs="?", help="one plan (default: all)")
    p.set_defaults(fn=cmd_plan_refine)
    p = psub.add_parser("show", help="print plans")
    p.add_argument("project")
    p.add_argument("name", nargs="?", help="one plan, e.g. batch_001__scene_002")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan_show)

    rf = sub.add_parser("refs", help="stage 2: reference sheets for the plans' assets")
    rsub = rf.add_subparsers(dest="refs_cmd", required=True)
    p = rsub.add_parser("generate", help="sheets for units without any (or --unit to add more)")
    p.add_argument("project")
    p.add_argument("--plan", help="one plan only")
    p.add_argument("--unit", action="append", help="asset id or kit-<name> (repeatable)")
    p.add_argument("-n", type=int, default=2, help="sheets per unit (default 2)")
    p.add_argument("--seed", type=int)
    p.set_defaults(fn=cmd_refs_generate)
    p = rsub.add_parser("show", help="list units, sheet counts and stars")
    p.add_argument("project")
    p.add_argument("--plan")
    p.set_defaults(fn=cmd_refs_show)

    p = sub.add_parser("views", help="stage 3: cut views out of starred reference sheets")
    vsub = p.add_subparsers(dest="views_cmd", required=True)
    p = vsub.add_parser("cut", help="cut starred sheets without views (or the given sheets)")
    p.add_argument("project")
    p.add_argument("sheets", nargs="*", help="sheet paths (project-relative), re-cut even if done")
    p.set_defaults(fn=cmd_views_cut)

    rv = sub.add_parser("review", help="stage 4: choose views, tag game / cine / hero")
    rvsub = rv.add_subparsers(dest="review_cmd", required=True)
    p = rvsub.add_parser("show", help="assets with their views, choice, tag and 3D results")
    p.add_argument("project")
    p.set_defaults(fn=cmd_review_show)
    p = rvsub.add_parser("choose", help="the view an asset goes to 3D with")
    p.add_argument("project")
    p.add_argument("plan")
    p.add_argument("asset")
    p.add_argument("view", nargs="?", help="view path (omit to clear)")
    p.set_defaults(fn=cmd_review_choose)
    p = rvsub.add_parser("tag", help="usage: game | cine | hero")
    p.add_argument("project")
    p.add_argument("plan")
    p.add_argument("asset")
    p.add_argument("usage", choices=["game", "cine", "hero"])
    p.set_defaults(fn=cmd_review_tag)

    p = sub.add_parser("3d", help="stage 5: TRELLIS.2 on an asset's chosen view (GPU 0)")
    p.add_argument("project")
    p.add_argument("plan")
    p.add_argument("asset")
    p.add_argument("--view", help="override the chosen view")
    p.add_argument("--mode", default="1024_cascade", help="1024_cascade (default) | 512 | 1024")
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(fn=cmd_3d)

    p = sub.add_parser("ui", help="start the local review UI (127.0.0.1 only)")
    p.add_argument("--port", type=int, default=8700)
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("vlm", help="local vision LLM server (Qwen3-VL, GPU 0; see config [local])")
    p.add_argument("action", choices=["up", "down", "status"])
    p.add_argument("--model", help="32b | 8b (default: config, falling back to the 8B)")
    p.add_argument("--gpu", type=int, default=None,
                   help="up: GPU to use (default 0); down: only stop it if it's on this GPU")
    p.add_argument("--idle-unload", type=float, default=None,
                   help="free the GPU after this many idle seconds (0 = never; default: config)")
    p.add_argument("--force", action="store_true", help="down: don't wait for a running request")
    p.set_defaults(fn=cmd_vlm)

    p = sub.add_parser("set", help="project settings: vision LLM and per-stage image backend")
    p.add_argument("project")
    p.add_argument("--llm", help="local | gemini | claude")
    p.add_argument("--backend", action="append", help="STAGE=comfyui|gemini (repeatable)")
    p.set_defaults(fn=cmd_set)

    a = ap.parse_args(argv)
    if a.cmd == "vlm":
        a.gpu_given, a.gpu = a.gpu is not None, 0 if a.gpu is None else a.gpu
    try:
        a.fn(a)
    except (ComfyError, LLMError, PaidAPIBlocked, FileNotFoundError, FileExistsError,
            ValueError, NotImplementedError) as e:
        sys.exit(f"ap: {e}")


if __name__ == "__main__":
    main()
