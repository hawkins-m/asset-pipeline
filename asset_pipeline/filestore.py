"""Shared file store: one content-addressed store per project for reference sheets, cut views
and 3D builds (every GLB under 3d/: builds, TRELLIS results, versions, review rounds, instances).

    files/<aa>/<sha256><ext>     the content, named by its hash, read-only (0444)

An entry or version holds a *reference*: its usual path (refs/<plan>/<unit>/sheet_000.png,
3d/library/<asset>/versions/v3/procedural.glb, ...) is a hard link to the store object, so every
reader (the UI, ComfyUI uploads, TRELLIS, Blender, Unreal) works on the same path as before and
`review.rel` keys don't change. A duplicate links the same objects instead of copying them.

References are independent:
  - the content is read-only: nothing can change an object in place (a writer that misses
    `detach` gets PermissionError instead of altering another entry's file);
  - every writer that replaces a stored path calls `detach` first, which drops only that path's
    link, then `intern` stores the new content (a new object, or a link to an identical one);
  - deleting a path drops its reference; an object goes when nothing references it (its link
    count is 1: the store's own name), via `release` right away or `clean` (Tools › Storage…).
"""
import hashlib
import os
import re
import stat
from pathlib import Path

from .project import ProjectStore

DIR = "files"
RO = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
RW = RO | stat.S_IWUSR
_UNIT_PNG = re.compile(r"^(refs|views)/[^/]+/[^/]+/[^/]+\.png$")
_GLB = re.compile(r"^3d/.+\.glb$")


def root(store: ProjectStore) -> Path:
    return store.root / DIR


def stored_kind(store: ProjectStore, path: Path | str) -> bool:
    """Whether a project file belongs in the store: a sheet or view PNG of a unit, or a GLB under 3d/."""
    p = Path(path)
    rel = (p.relative_to(store.root) if p.is_absolute() else p).as_posix()
    return bool(_UNIT_PNG.match(rel) or _GLB.match(rel))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def object_path(store: ProjectStore, sha: str, ext: str) -> Path:
    return root(store) / sha[:2] / f"{sha}{ext.lower()}"


def _objects(store: ProjectStore):
    r = root(store)
    return (p for p in r.glob("??/*") if p.is_file()) if r.is_dir() else iter(())


def is_ref(path: Path) -> bool:
    """A stored path: read-only with another link (the store's object)."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1 and not st.st_mode & stat.S_IWUSR


def intern(store: ProjectStore, path: Path) -> Path | None:
    """Store a file's content and make the path a reference to it. Identical content shares one
    object (the path's own copy is dropped). Returns the object, or None for a file that doesn't
    belong in the store."""
    path = Path(path)
    if not path.is_file() or path.is_symlink() or not stored_kind(store, path):
        return None
    if is_ref(path):
        return None                                     # already a reference
    obj = object_path(store, digest(path), path.suffix)
    if obj.exists():
        if not os.path.samefile(obj, path):             # same content elsewhere: point at it
            tmp = path.with_name(f".{path.name}.ln")
            tmp.unlink(missing_ok=True)
            os.link(obj, tmp)
            os.replace(tmp, path)
    else:
        obj.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path, RO)
        os.link(path, obj)
    return obj


def intern_all(store: ProjectStore, paths) -> int:
    return sum(1 for p in paths if intern(store, Path(p)) is not None)


def candidates(store: ProjectStore, under: Path | None = None):
    """Every project file that belongs in the store (under one dir, or the whole project)."""
    bases = [Path(under)] if under else [store.root / d for d in ("refs", "views", "3d")]
    for b in bases:
        files = [b] if b.is_file() else (b.rglob("*") if b.is_dir() else [])
        for f in files:
            if f.is_file() and not f.is_symlink() and stored_kind(store, f):
                yield f


def intern_tree(store: ProjectStore, under: Path) -> int:
    return intern_all(store, list(candidates(store, under)))


def detach(path: Path) -> None:
    """Before writing over `path`: drop its reference, so the write makes new content and no
    other entry's file changes. A plain file is made writable again."""
    path = Path(path)
    if is_ref(path):
        path.unlink()
    elif path.is_file() and not os.access(path, os.W_OK):
        os.chmod(path, RW)


def copy(store: ProjectStore, src: Path, dst: Path) -> None:
    """dst gets src's content: a new reference to its object when it belongs in the store
    (src is stored first), else a plain copy."""
    import shutil
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    detach(dst)
    dst.unlink(missing_ok=True)
    if stored_kind(store, src) and stored_kind(store, dst):
        obj = intern(store, src) or (object_path(store, digest(src), src.suffix) if is_ref(src) else None)
        if obj is not None and obj.exists():
            os.link(obj, dst)
            return
    shutil.copy2(src, dst)
    if not os.access(dst, os.W_OK):
        os.chmod(dst, RW)


def copy_function(store: ProjectStore):
    """For shutil.copytree: references are linked, other files copied."""
    return lambda s, d: copy(store, Path(s), Path(d)) if stored_kind(store, Path(d)) else _plain_copy(s, d)


def _plain_copy(s, d):
    import shutil
    shutil.copy2(s, d)
    if not os.access(d, os.W_OK):
        os.chmod(d, RW)
    return d


def release(store: ProjectStore, path: Path) -> int:
    """Delete a path's reference; its object goes too when nothing else references it.
    Returns the bytes freed on disk."""
    path = Path(path)
    if not path.is_file():
        return 0
    size = path.stat().st_size
    obj = object_path(store, digest(path), path.suffix) if is_ref(path) else None
    path.unlink()
    if obj is None:
        return size
    if obj.exists() and obj.stat().st_nlink == 1:
        obj.unlink()
        return size
    return 0


def stats(store: ProjectStore) -> dict:
    """{objects, bytes (on disk), references, referenced_bytes (what plain copies would take),
    saved (bytes sharing saves), unreferenced, unreferenced_bytes}."""
    n = b = refs = logical = un = un_b = 0
    for o in _objects(store):
        st = o.stat()
        k = st.st_nlink - 1
        n, b = n + 1, b + st.st_size
        if k <= 0:
            un, un_b = un + 1, un_b + st.st_size
        refs, logical = refs + k, logical + k * st.st_size
    return {"objects": n, "bytes": b, "references": refs, "referenced_bytes": logical,
            "saved": max(0, logical - (b - un_b)), "unreferenced": un, "unreferenced_bytes": un_b}


def clean(store: ProjectStore, dry_run: bool = False) -> dict:
    """Remove the objects nothing references any more."""
    gone = [o for o in _objects(store) if o.stat().st_nlink == 1]
    b = sum(o.stat().st_size for o in gone)
    if not dry_run:
        for o in gone:
            o.unlink()
        for d in root(store).glob("??"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    return {"removed": len(gone), "bytes": b}


def migrate(store: ProjectStore, log=print) -> dict:
    """Move a project's sheets, views and GLBs into the store, sharing identical files (e.g. a
    duplicate's copies of its source's sheets and builds). Safe to run again."""
    before = stats(store)
    files = list(candidates(store))
    disk0 = sum(f.stat().st_size for f in files if not is_ref(f)) + before["bytes"]
    n = intern_all(store, files)
    _ignore(store)
    after = stats(store)
    freed = disk0 - after["bytes"]
    log(f"{store.root.name}: {n} file(s) stored, {after['objects']} objects for {after['references']} references, "
        f"{freed / 1e6:.1f} MB freed")
    return {"stored": n, "freed": freed} | after


def _ignore(store: ProjectStore) -> None:
    """The store is generated media: keep it out of the project's git repo."""
    gi = store.root / ".gitignore"
    if not (store.root / ".git").exists() and not gi.exists():
        return
    txt = gi.read_text() if gi.exists() else ""
    if not any(ln.strip() in (f"{DIR}/", f"/{DIR}/") for ln in txt.splitlines()):
        gi.write_text(txt + ("" if txt.endswith("\n") or not txt else "\n")
                      + f"\n# shared file store (sheets, views, GLBs by content hash; filestore.py)\n/{DIR}/\n")
