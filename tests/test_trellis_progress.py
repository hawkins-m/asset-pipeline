"""TRELLIS progress from its log: real stages, folded passes, indeterminate where there's no count,
and the server's /status entry for a running Make 3D."""
import time
from types import SimpleNamespace

from asset_pipeline import trellis_progress as tp
from asset_pipeline.project import ProjectStore


def bar(name, d, t):
    return f"{name}: {round(100 * d / t):3d}%|##        | {d}/{t} [00:01<00:02, 5.0it/s]\r"


RUN = ["[SPARSE] Conv backend: flex_gemm; Attention backend: sdpa\n"]
RUN += [bar("Loading weights", d, 415) for d in (0, 200, 415)] + ["\n"]
RUN += [bar("Loading weights", d, 754) for d in (0, 300, 754)] + ["\n"]
RUN += [bar("Sampling sparse structure", d, 12) for d in (0, 6, 12)] + ["\n"]
RUN += [bar("Sampling shape SLat", d, 12) for d in (0, 12)] + ["\n"]
RUN += [bar("Sampling shape SLat", d, 12) for d in (0, 3, 12)] + ["\n"]
RUN += [bar("Sampling texture SLat", d, 12) for d in (0, 12)] + ["\n"]
RUN += ["Original mesh: 7770734 vertices, 16339389 faces\n", "Cleaning mesh...\n", bar("Simplifying", 5, 23078960), "\n",
        "Parameterizing new mesh...\n", bar("xatlas: Computing charts", 50, 100), "\n", "Sampling attributes...Done\n",
        "Finalizing mesh...Done\n", '{\n  "glb_faces": 934246\n}\n']


def test_stages_and_fractions_only_move_forward_over_a_whole_run():
    seen, text = [], ""
    for chunk in RUN:
        text += chunk
        p = tp.parse(text, "1024_cascade")
        frac = p["done"] / p["total"] if p["total"] else None
        seen.append((p["stage"], frac, p["key"]))
    stages = [s for s, _, _ in seen]
    assert stages == sorted(stages) and stages[-1] == 6
    for (s0, f0, _), (s1, f1, _) in zip(seen, seen[1:]):       # within a stage, counted fractions never drop
        if s0 == s1 and f0 is not None and f1 is not None:
            assert f1 >= f0
    keys = {k: [f for s, f, kk in seen if kk == k] for k in ("load", "shape", "decode", "uv", "export")}
    assert keys["load"][-1] == 1.0 and 0.5 in keys["load"]       # pass 1 done = half the stage
    assert max(keys["shape"]) == 1.0 and 0.5 in keys["shape"]    # cascade: two shape passes
    assert all(f is None for k in ("decode", "uv", "export") for f in keys[k])   # no fake percentages
    assert tp.parse(text)["finished"]


def test_labels_and_an_unexpected_extra_pass_is_indeterminate():
    text = "".join(RUN[:18])                                    # into the second shape pass
    p = tp.parse(text, "1024_cascade")
    assert tp.line(p) == "Stage 3 of 7 · Sampling the shape · pass 2 of 2"
    p512 = tp.parse(text, "512")                                 # 512 expects one shape pass: the second is a surprise
    assert p512["done"] is None and tp.line(p512) == "Stage 3 of 7 · Sampling the shape · pass 2"
    assert tp.parse("")["stage"] == 0 and tp.parse("")["done"] is None


def test_status_reports_trellis_stages_for_a_running_make_3d(tmp_path, monkeypatch):
    from asset_pipeline.ui import app as ui_app
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("p")
    log = store.root / "3d/library/gate/trellis.log"
    log.parent.mkdir(parents=True)
    log.write_text("old run\n\n=== 2026-10-18 make 3d\n" + "".join(RUN[:12]))
    job = SimpleNamespace(kind="3d.trellis", tag={"plan": "library", "asset": "gate", "mode": "1024_cascade"},
                          created=time.time() - 30, note="")
    out = ui_app._progress(store, job, time.time() - 30, time.time())
    assert out["stage"] == 1 and out["stage_label"] == "Sampling the sparse structure"
    assert out["done"] == 12 and out["total"] == 12 and len(out["stages"]) == 7
    assert out["line"] == "gate · Stage 2 of 7 · Sampling the sparse structure" and "eta_s" not in out
