"""Review state in <project>/review.json, shared by the CLI and the UI.

    {"stars": {"style/explore/batch_001/scene_003.png": true, ...}}

Keys are paths relative to the project root. View-sets (stage 3/4) are added later.
"""
from pathlib import Path

from .project import ProjectStore, read_json, write_json


def _file(store: ProjectStore) -> Path:
    return store.root / "review.json"


def load(store: ProjectStore) -> dict:
    data = read_json(_file(store), default={}) or {}
    data.setdefault("stars", {})
    return data


def rel(store: ProjectStore, path: Path | str) -> str:
    """Normalise a path (absolute or project-relative) to a project-relative key."""
    p = Path(path)
    if p.is_absolute():
        p = p.resolve().relative_to(store.root.resolve())
    if ".." in p.parts:
        raise ValueError(f"path escapes the project: {path}")
    if not (store.root / p).is_file():
        raise FileNotFoundError(f"no such file in project: {p}")
    return p.as_posix()


def set_star(store: ProjectStore, path: Path | str, starred: bool = True) -> str:
    key = rel(store, path)
    data = load(store)
    if starred:
        data["stars"][key] = True
    else:
        data["stars"].pop(key, None)
    write_json(_file(store), data)
    return key


def starred(store: ProjectStore, prefix: str = "") -> list[str]:
    """Starred keys under a prefix (e.g. "style/explore/"), in sorted order."""
    return sorted(k for k, v in load(store)["stars"].items() if v and k.startswith(prefix)
                  and (store.root / k).is_file())
