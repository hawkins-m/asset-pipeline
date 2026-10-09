"""Review state in <project>/review.json, shared by the CLI and the UI.

    {"stars":  {"style/explore/batch_001/scene_003.png": true, ...},
     "chosen": {"<plan>/<asset id>": "views/<plan>/<unit>/sheet_000_v2.png", ...},
     "roles":  {"frames/<shot>/batch_002/frame_003.png": "design_ref" | "source", ...},
     "notes":  {"frames/<shot>/batch_002/frame_003.png": "fountain toward the sea", ...}}

Keys are paths relative to the project root. "chosen" is the view an asset goes to 3D
with (stage 4). A frame's role says what it's for: a design_ref guides generation (a
shot's or a typology's reference) and never seeds the asset library; a source may. Usage tags (game / cine / hero) live on the plan asset.
"""
from pathlib import Path

from .project import ProjectStore, read_json, write_json


def _file(store: ProjectStore) -> Path:
    return store.root / "review.json"


def load(store: ProjectStore) -> dict:
    data = read_json(_file(store), default={}) or {}
    data.setdefault("stars", {})
    data.setdefault("chosen", {})
    data.setdefault("roles", {})
    data.setdefault("notes", {})
    return data


ROLES = ("design_ref", "source")


def set_role(store: ProjectStore, path: Path | str, role: str | None, note: str | None = None) -> str:
    """Set (None clears) a frame's role, and optionally its note."""
    if role is not None and role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)} (or none)")
    key = rel(store, path)
    data = load(store)
    if role:
        data["roles"][key] = role
    else:
        data["roles"].pop(key, None)
    if note is not None:
        if note.strip():
            data["notes"][key] = note.strip()
        else:
            data["notes"].pop(key, None)
    write_json(_file(store), data)
    return key


def roles(store: ProjectStore) -> dict[str, str]:
    return dict(load(store)["roles"])


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
