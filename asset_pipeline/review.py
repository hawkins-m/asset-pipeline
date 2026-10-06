"""Review state in <project>/review.json, shared by the CLI and the UI.

    {"stars":  {"style/explore/batch_001/scene_003.png": true, ...},
     "chosen": {"<plan>/<asset id>": "views/<plan>/<unit>/sheet_000_v2.png", ...}}

Keys are paths relative to the project root. "chosen" is the view an asset goes to 3D
with (stage 4). Usage tags (game / cine / hero) live on the plan asset.
"""
from pathlib import Path

from .project import ProjectStore, read_json, write_json


def _file(store: ProjectStore) -> Path:
    return store.root / "review.json"


def load(store: ProjectStore) -> dict:
    data = read_json(_file(store), default={}) or {}
    data.setdefault("stars", {})
    data.setdefault("chosen", {})
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


def forget(store: ProjectStore, keys: list[str]) -> None:
    """Drop stars for keys whose files are being deleted (no existence check)."""
    data = load(store)
    removed = [k for k in keys if data["stars"].pop(k, None) is not None]
    if removed:
        write_json(_file(store), data)


def choose(store: ProjectStore, plan: str, asset_id: str, view: Path | str | None) -> str | None:
    """Set (or with None clear) the view an asset goes to 3D with."""
    data = load(store)
    key = rel(store, view) if view else None
    if key:
        data["chosen"][f"{plan}/{asset_id}"] = key
    else:
        data["chosen"].pop(f"{plan}/{asset_id}", None)
    write_json(_file(store), data)
    return key


def chosen(store: ProjectStore, plan: str, asset_id: str) -> str | None:
    key = load(store)["chosen"].get(f"{plan}/{asset_id}")
    return key if key and (store.root / key).is_file() else None
