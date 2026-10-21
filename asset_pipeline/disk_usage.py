"""A project's size on disk by category, for the toolbar's "Project: 3.1 GB" and the
Properties dialog (pie chart, drive usage, space saved by sharing).

Every file is counted once by inode, so the shared store's objects (files/, hard-linked from
refs/, views/ and 3d/) and hard-linked UE backups never count twice. An object takes the
category of the first path that references it; one nothing references is "Other". Sizes are
allocated blocks (size on disk), not file lengths.

The walk runs in a background thread (`Cache`), at most once per 30 s per project: the UI
gets the last result straight away, with `pending` while a newer one is on the way.
"""
import os
import shutil
import threading
import time
from pathlib import Path

from . import filestore
from .project import ProjectStore

CATEGORIES = [
    ("sheets", "Reference sheets"),
    ("views", "Reference views"),
    ("models", "3D models (versions)"),
    ("textures", "Textures and materials"),
    ("frames", "Frames and shot passes"),
    ("greybox", "Greybox and plans"),
    ("unreal", "Unreal project"),
    ("other", "Other"),
]
MIN_INTERVAL = 30.0
_IMAGES = {".png", ".jpg", ".jpeg", ".exr", ".tga", ".tif", ".tiff", ".hdr", ".ktx2", ".dds"}
_MAP_WORDS = ("base", "albedo", "basecolor", "diffuse", "normal", "rough", "metal", "ao", "orm", "height", "tile")


def category(rel: str) -> str:
    """The category of a path relative to the project root (posix)."""
    parts = rel.split("/")
    top, name = parts[0], parts[-1].lower()
    ext = os.path.splitext(name)[1]
    if top == "refs":
        return "sheets"
    if top == "views":
        return "views"
    if top in ("frames", "shots"):
        return "frames"
    if top in ("site", "plan", "areas", "library") or rel.startswith("export/terrain/"):
        return "greybox"
    if top in ("3d", "export"):
        if ext in _IMAGES and ("textures" in parts[:-1] or "procedural" in parts[:-1]
                               or any(w in os.path.splitext(name)[0].split("_") for w in _MAP_WORDS)):
            return "textures"
        if top == "3d" or ext == ".glb":
            return "models"
    if top == "materials":
        return "textures"
    return "other"


def _walk(base: Path):
    """(path, lstat) of every regular file under base; symlinks are not followed."""
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        yield Path(e.path), e.stat(follow_symlinks=False)
                except (FileNotFoundError, PermissionError):
                    continue


def _on_disk(st: os.stat_result) -> int:
    return st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size


def unreal_dirs(store: ProjectStore) -> dict[str, Path]:
    """The project's UE project folder and its backups (both outside the project folder)."""
    from . import ue
    try:
        out = {"project": ue.project_dir(store), "backups": ue.backups_dir() / store.load().slug}
    except Exception:  # noqa: BLE001  (a missing UE config must not break the size)
        return {}
    return {k: p for k, p in out.items() if p.is_dir()}


def measure(store: ProjectStore) -> dict:
    """{total, categories: [{key, label, bytes, files}], saved, unreferenced_bytes,
    drive: {path, total, used, free} (the project's volume), unreal: {project|backups: {path, bytes,
    other_drive}}, seconds}."""
    t0 = time.monotonic()
    root = store.root
    seen: set[tuple[int, int]] = set()
    sizes = {k: 0 for k, _ in CATEGORIES}
    counts = {k: 0 for k, _ in CATEGORIES}

    def add(cat, st):
        key = (st.st_dev, st.st_ino)
        if key in seen:
            return
        seen.add(key)
        sizes[cat] += _on_disk(st)
        counts[cat] += 1

    store_files = []
    for p, st in _walk(root):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(filestore.DIR + "/"):
            store_files.append(st)                       # after every reference: they name the category
            continue
        add(category(rel), st)
    unref = 0
    for st in store_files:
        if (st.st_dev, st.st_ino) not in seen:
            unref += _on_disk(st)
            add("other", st)
    ue_dirs = unreal_dirs(store)
    ue_parts = {}
    for k, d in ue_dirs.items():
        before = sizes["unreal"]
        for _, st in _walk(d):
            add("unreal", st)
        ue_parts[k] = sizes["unreal"] - before
    drive = shutil.disk_usage(root)
    dev = os.stat(root).st_dev
    elsewhere = {k: str(d) for k, d in ue_dirs.items() if os.stat(d).st_dev != dev}
    fs = filestore.stats(store)
    return {"total": sum(sizes.values()),
            "categories": [{"key": k, "label": lb, "bytes": sizes[k], "files": counts[k]} for k, lb in CATEGORIES],
            "saved": fs["saved"], "unreferenced": fs["unreferenced"], "unreferenced_bytes": unref,
            "drive": {"path": _mount_point(root), "total": drive.total, "used": drive.used, "free": drive.free},
            "unreal": {k: {"path": str(d), "bytes": ue_parts[k], "other_drive": k in elsewhere}
                       for k, d in ue_dirs.items()},
            "seconds": round(time.monotonic() - t0, 2)}


def _mount_point(p: Path) -> str:
    p = Path(p).resolve()
    while not os.path.ismount(p) and p.parent != p:
        p = p.parent
    return str(p)


class Cache:
    """Last measurement per project; `get` starts a background walk when the result is missing,
    older than MIN_INTERVAL, or marked dirty (a job finished). A dirty request inside the
    interval runs when the interval is up."""

    def __init__(self, store_of, interval: float = MIN_INTERVAL):
        self.store_of, self.interval = store_of, interval
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}

    def get(self, slug: str, dirty: bool = False) -> dict:
        with self.lock:
            s = self.state.setdefault(slug, {"result": None, "at": 0.0, "running": False, "dirty": False,
                                             "timer": None, "error": None})
            s["dirty"] = s["dirty"] or dirty
            wait = s["at"] + self.interval - time.monotonic()
            due = s["result"] is None or s["dirty"] or wait <= 0
            if due and not s["running"]:
                if wait > 0 and s["result"] is not None:
                    if s["timer"] is None:
                        s["timer"] = threading.Timer(wait, self._start, (slug,))
                        s["timer"].daemon = True
                        s["timer"].start()
                else:
                    self._start_locked(slug, s)
            pending = s["running"] or s["timer"] is not None
            return {"result": s["result"], "pending": pending, "error": s["error"],
                    "age": None if not s["at"] else round(time.monotonic() - s["at"], 1)}

    def _start(self, slug):
        with self.lock:
            s = self.state[slug]
            s["timer"] = None
            if not s["running"]:
                self._start_locked(slug, s)

    def _start_locked(self, slug, s):
        s["running"], s["dirty"] = True, False
        threading.Thread(target=self._run, args=(slug,), daemon=True, name=f"disk-usage-{slug}").start()

    def _run(self, slug):
        try:
            os.nice(5)                       # this thread only (Linux): never compete with the UI
        except (AttributeError, OSError):
            pass
        res, err = None, None
        try:
            res = measure(self.store_of(slug))
        except Exception as e:  # noqa: BLE001
            err = str(e)
        with self.lock:
            s = self.state[slug]
            s["running"], s["at"], s["error"] = False, time.monotonic(), err
            if res is not None:
                s["result"] = res
