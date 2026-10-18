"""TRELLIS.2 progress from its log (3d/<plan>/<asset>/trellis.log, the newest run after "=== "):
the stage it is in and, where TRELLIS prints a tqdm bar, how far that stage is.

Stages, in order (markers verified on a 1024_cascade run, 2026-10-18):
    load      Loading the model              "Loading weights" bars (2 passes: two checkpoints)
    sparse    Sampling the sparse structure  "Sampling sparse structure" steps
    shape     Sampling the shape             "Sampling shape SLat" steps (cascade: 2 passes)
    texture   Sampling the texture           "Sampling texture SLat" steps
    decode    Decoding and cleaning the mesh "Original mesh", hole filling, remeshing, simplifying
    uv        UV unwrapping, baking textures "Parameterizing new mesh", xatlas, attribute sampling
    export    Writing the GLB                "Finalizing mesh"
decode, uv and export print no usable count (xatlas bars run in milliseconds, the simplifier
counts faces of an unknown number of rounds), so they are indeterminate: done is None.
A stage with passes counts them together (pass k of n), so its fraction never drops between
passes; more passes than expected make it indeterminate rather than guessing.
"""
import re

STAGES = [
    ("load", "Loading the model", "Loading weights"),
    ("sparse", "Sampling the sparse structure", "Sampling sparse structure"),
    ("shape", "Sampling the shape", "Sampling shape SLat"),
    ("texture", "Sampling the texture", "Sampling texture SLat"),
    ("decode", "Decoding and cleaning the mesh", None),
    ("uv", "UV unwrapping and baking textures", None),
    ("export", "Writing the GLB", None),
]
MARKERS = {   # a stage has begun once one of these is in the log
    "load": [r"Loading weights", r"\[SPARSE\]", r"\[ATTENTION\]"],
    "sparse": [r"Sampling sparse structure"],
    "shape": [r"Sampling shape SLat"],
    "texture": [r"Sampling texture SLat"],
    "decode": [r"Original mesh:", r"After filling holes", r"Cleaning mesh", r"Sparse Grid", r"After remeshing", r"Simplifying"],
    "uv": [r"Parameterizing new mesh", r"xatlas", r"Sampling attributes"],
    "export": [r"Finalizing mesh"],
}


def passes(mode: str | None) -> dict:
    """Expected passes of the counted stages for a TRELLIS mode."""
    return {"load": 2, "sparse": 1, "shape": 2 if (mode or "").endswith("cascade") else 1, "texture": 1}


def _bar(text: str, name: str) -> tuple[int, int | None, int | None]:
    """(passes started, done, total) of a named tqdm bar's latest pass."""
    started = len(re.findall(re.escape(name) + r":\s+0%\|", text))
    last = re.findall(re.escape(name) + r":\s+\d+%\|[^|\n]*\|\s*(\d+)/(\d+)", text)
    if not last:
        return started, None, None
    d, t = map(int, last[-1])
    return max(started, 1), d, t


def parse(text: str | None, mode: str | None = None) -> dict:
    """{stage (index), key, label, stages [labels], done, total (None: indeterminate), pass,
    passes, finished} for the newest run's log text."""
    text = (text or "").replace("\r", "\n")
    at = 0
    for i, (key, _, _) in enumerate(STAGES):
        if any(re.search(m, text) for m in MARKERS[key]):
            at = i
    finished = bool(re.search(r'"glb_faces":', text))
    key, label, bar = STAGES[at]
    out = {"stage": at, "key": key, "label": label, "stages": [s[1] for s in STAGES], "done": None, "total": None,
           "pass": None, "passes": None, "finished": finished}
    if bar:
        k, d, t = _bar(text, bar)
        want = passes(mode).get(key, 1)
        if d is not None and t and k <= want:
            # passes folded into one count: pass k of want, d of t steps -> (k - 1 + d / t) / want
            out.update({"done": round((k - 1) * t + d), "total": want * t, "pass": k, "passes": want})
        elif d is not None:
            out.update({"pass": k})                     # more passes than expected: indeterminate
    return out


def line(p: dict) -> str:
    """'Stage 3 of 7 · Sampling the shape · pass 2 of 2'."""
    s = f"Stage {p['stage'] + 1} of {len(p['stages'])} · {p['label']}"
    if p.get("passes") and p["passes"] > 1:
        s += f" · pass {p['pass']} of {p['passes']}"
    elif p.get("pass") and p.get("done") is None:
        s += f" · pass {p['pass']}"
    return s
