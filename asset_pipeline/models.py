"""Model downloads: the manifest (config/models.toml), what is installed, and `ap models`.

Every model the pipeline loads is one [[model]] in the manifest: its files pinned to a hub
revision with size and checksum, where each file goes (a root from the user config plus a path),
what uses it, its license, and whether it is required. `ap models list` shows each with installed
or missing; `ap models download` fetches missing files with resume (.part / .incomplete files),
a disk-space check first, checksum verification, and progress as text or JSON lines (--json,
one event per line, for an installer's progress bar). Gated repos get exact instructions.

Roots: comfyui -> [comfyui] models_dir; models -> <storage>/models; hf_cache -> <storage>/hf/hub
(the Hugging Face cache layout: blobs/<hash> plus snapshots/<revision>/<path> links).
"""
import hashlib
import json
import os
import shutil
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config

MANIFEST = config.REPO_ROOT / "config" / "models.toml"
NEEDS = ("required", "local.model", "optional", "experimental")
DISK_MARGIN = 2 * 1024**3                        # keep 2 GB free on top of the downloads


@dataclass
class File:
    src: str
    dest: str
    size: int
    sha256: str | None = None
    git_sha1: str | None = None

    @property
    def checksum(self) -> str:
        return self.sha256 or self.git_sha1 or ""


@dataclass
class Source:
    repo: str
    revision: str
    files: list[File]


@dataclass
class Model:
    id: str
    title: str
    used_by: str
    feature: str
    need: str
    license: str
    repo: str
    revision: str
    gated: str | bool
    root: str
    vram_gb: float
    files: list[File]
    select: str = ""
    repo_setting: str = ""
    mirrors: list[Source] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.source().files)

    def source(self) -> Source:
        """The repo to fetch from: the manifest's, or a mirror the config names (repo_setting)."""
        want = config.get(self.repo_setting) if self.repo_setting else None
        if want and want != self.repo:
            for m in self.mirrors:
                if m.repo == want:
                    return m
            raise ValueError(f"{self.repo_setting} = {want!r} is not a mirror the manifest lists for {self.id} "
                             f"(known: {', '.join(m.repo for m in self.mirrors) or 'none'})")
        return Source(self.repo, self.revision, self.files)

    @property
    def page(self) -> str:
        return f"https://huggingface.co/{self.source().repo}"


def _files(rows) -> list[File]:
    return [File(**r) for r in rows]


def load(path: Path | None = None) -> list[Model]:
    with open(path or MANIFEST, "rb") as f:
        rows = tomllib.load(f)["model"]
    out = []
    for r in rows:
        r = dict(r)
        mirrors = [Source(m["repo"], m["revision"], _files(m["files"])) for m in r.pop("mirror", [])]
        r["files"] = _files(r["files"])
        out.append(Model(**r, mirrors=mirrors))
    return out


def get(model_id: str, models: list[Model] | None = None) -> Model:
    for m in models or load():
        if m.id == model_id:
            return m
    raise KeyError(model_id)


def required(m: Model) -> bool:
    """Needed on this install: always, or as the configured vision model."""
    if m.need == "required":
        return True
    if m.need == "local.model":
        return m.select == str(config.get("local.model", "32b"))
    return False


def need_label(m: Model) -> str:
    if m.need == "local.model":
        if required(m):
            return "required"
        return "fallback" if m.select == str(config.get("local.fallback", "")) else "optional"
    return m.need


# --- where files go -----------------------------------------------------------------------------

def root_dir(root: str) -> Path | None:
    if root == "comfyui":
        d = config.get("comfyui.models_dir") or ""
        return Path(os.path.expanduser(str(d))) if d else None
    if root == "models":
        return config.ap_root() / "models"
    if root == "hf_cache":
        return config.ap_root() / "hf" / "hub"
    raise ValueError(f"unknown root {root!r}")


def _cache_dir(base: Path, repo: str) -> Path:
    return base / ("models--" + repo.replace("/", "--"))


def file_path(m: Model, f: File, src: Source | None = None) -> Path | None:
    """Where the file is used from (None: its root isn't set, e.g. no ComfyUI models folder)."""
    base = root_dir(m.root)
    if base is None:
        return None
    if m.root == "hf_cache":
        s = src or m.source()
        return _cache_dir(base, s.repo) / "snapshots" / s.revision / f.dest
    return base / f.dest


def _present(m: Model, f: File, src: Source) -> Path | None:
    """The installed file (right size), or None. The cache accepts any revision's snapshot."""
    p = file_path(m, f, src)
    if p is None:
        return None
    cands = [p]
    if m.root == "hf_cache":
        cands += sorted((_cache_dir(root_dir("hf_cache"), src.repo) / "snapshots").glob(f"*/{f.dest}"))
    for c in cands:
        try:
            if c.is_file() and c.stat().st_size == f.size:
                return c
        except OSError:
            continue
    return None


def _part(m: Model, f: File, src: Source) -> Path:
    if m.root == "hf_cache":
        return _cache_dir(root_dir("hf_cache"), src.repo) / "blobs" / f"{f.checksum}.incomplete"
    return file_path(m, f, src).with_name(Path(f.dest).name + ".part")


def status(m: Model) -> dict:
    """{state: installed | missing | partial | no_root, have, missing_bytes, files, missing_files, where}."""
    src = m.source()
    base = root_dir(m.root)
    if base is None:
        return {"state": "no_root", "have": 0, "missing_bytes": m.size, "files": len(src.files),
                "missing_files": [f.dest for f in src.files], "where": None}
    have, missing, partial = 0, [], 0
    for f in src.files:
        if _present(m, f, src):
            have += f.size
            continue
        missing.append(f.dest)
        part = _part(m, f, src)
        if part.is_file():
            partial += min(part.stat().st_size, f.size)
    state = "installed" if not missing else ("partial" if have or partial else "missing")
    where = _cache_dir(base, src.repo) if m.root == "hf_cache" else base / os.path.commonpath([f.dest for f in src.files])
    return {"state": state, "have": have + partial, "missing_bytes": m.size - have - partial, "files": len(src.files),
            "missing_files": missing, "where": str(where)}


def gb(n: int | float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def download_command(ids: list[str]) -> str:
    return "ap models download " + " ".join(ids)


def missing(feature: str | None = None, required_only: bool = True) -> list[Model]:
    """Models not installed: the required ones (default), or every non-experimental one of a feature."""
    out = []
    for m in load():
        if feature and m.feature != feature:
            continue
        if required_only and not required(m):
            continue
        if not required_only and m.need == "experimental":
            continue
        if status(m)["state"] != "installed":
            out.append(m)
    return out


# --- tokens and gated repos -------------------------------------------------------------------

def endpoint() -> str:
    return (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")


def hf_token() -> str | None:
    """HF_TOKEN (environment or secrets.toml), else a token `hf auth login` saved."""
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    if config.secrets().get("HF_TOKEN"):
        return config.secrets()["HF_TOKEN"]
    for p in (config.ap_root() / "hf" / "token", Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser() / "token",
              Path("~/.cache/huggingface/token").expanduser()):
        try:
            t = p.read_text().strip()
            if t:
                return t
        except OSError:
            continue
    return None


def gated_help(m: Model, have_token: bool) -> str:
    src = m.source()
    page = f"https://huggingface.co/{src.repo}"
    if m.gated == "manual":
        steps = (f"{m.title} is gated and its owner reviews each request. Sign in at huggingface.co, open {page} and "
                 f"request access (approval can take days).")
    else:
        steps = (f"{m.title} is gated: sign in at huggingface.co, open {page} and accept its license "
                 f"(access is granted at once).")
    if have_token:
        steps += " Your token is set; it must belong to the account that accepted. Then run the same command again."
    else:
        steps += (" Then create a read token at https://huggingface.co/settings/tokens and give it to Terraformer: "
                  "`hf auth login`, or the Hugging Face token in `ap setup` (saved as HF_TOKEN in secrets.toml), "
                  "or export HF_TOKEN. Then run the same command again.")
    if m.mirrors and m.repo_setting:
        steps += (f" Or use the ungated mirror with the same weights: ap config set {m.repo_setting} "
                  f"{m.mirrors[0].repo}")
    return steps


class GatedError(RuntimeError):
    pass


class ChecksumError(RuntimeError):
    pass


# --- downloading --------------------------------------------------------------------------------

class Events:
    """Progress as text (stderr-free, one line per step; a live line on a terminal) or JSON lines."""

    def __init__(self, as_json: bool = False, out=None):
        self.json, self.out = as_json, out or sys.stdout
        self._last = 0.0
        self.tty = not as_json and hasattr(self.out, "isatty") and self.out.isatty()

    def __call__(self, event: str, **kw):
        if self.json:
            self.out.write(json.dumps({"event": event, "t": round(time.time(), 2), **kw}) + "\n")
            self.out.flush()
            return
        line = self._text(event, kw)
        if line is None:
            return
        if event == "progress" and self.tty:
            self.out.write("\r" + line[:120].ljust(120))
        else:
            if self.tty and event != "progress":
                self.out.write("\r" + " " * 120 + "\r")
            self.out.write(line + "\n")
        self.out.flush()

    def _text(self, e: str, k: dict) -> str | None:
        if e == "plan":
            return (f"{k['files']} file(s) to get from {k['models']} model(s), {gb(k['bytes'])}"
                    + (f" ({gb(k['resume_bytes'])} already downloaded)" if k.get("resume_bytes") else "") + ".")
        if e == "skip":
            return f"have      {k['model']}"
        if e == "disk":
            return f"disk      {k['where']}: {gb(k['need'])} needed, {gb(k['free'])} free"
        if e == "file_start":
            return f"get       {k['model']}: {k['file']} ({gb(k['size'])})" + (f", resuming at {gb(k['resume_from'])}" if k.get("resume_from") else "")
        if e == "progress":
            now = time.time()
            if not self.tty and now - self._last < 10 and k["done"] < k["total"]:
                return None                           # a log gets a line every 10 s, a terminal a live line
            self._last = now
            pct = 100 * k["done"] / max(k["total"], 1)
            return (f"          {k['file']}: {pct:5.1f}% {gb(k['done'])} of {gb(k['total'])}, {k['rate_mbs']:.0f} MB/s"
                    f" · all: {100 * k['overall_done'] / max(k['overall_total'], 1):.0f}%")
        if e == "verify":
            return f"verify    {k['file']} ({k['algo']})"
        if e == "file_done":
            return None
        if e == "model_done":
            return f"installed {k['model']} -> {k['where']}"
        if e == "error":
            return f"FAILED    {(k['model'] + ': ') if k.get('model') else ''}{k['message']}" + (f"\n          fix: {k['fix']}" if k.get("fix") else "")
        if e == "done" and k.get("dry_run"):
            return "dry run: nothing downloaded."
        if e == "done":
            return (f"done: {k['installed']} installed, {k['failed']} failed." if k["failed"] or k["installed"]
                    else "done: nothing to download.")
        return None


def _hasher(f: File):
    if f.sha256:
        return hashlib.sha256(), "sha256"
    h = hashlib.sha1()
    h.update(f"blob {f.size}\0".encode())             # the git blob id of a small (non-LFS) file
    return h, "git-sha1"


def _fetch(url: str, part: Path, f: File, token: str | None, emit, ctx: dict, model_id: str, client=None) -> None:
    """Download url into part (resuming it), verify, leave the complete file at part."""
    import httpx
    part.parent.mkdir(parents=True, exist_ok=True)
    h, algo = _hasher(f)
    start = part.stat().st_size if part.is_file() else 0
    if start > f.size:
        part.unlink()
        start = 0
    if start:                                          # hash what is there, then continue after it
        with open(part, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
    emit("file_start", model=model_id, file=f.dest, size=f.size, resume_from=start)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if start and start < f.size:
        headers["Range"] = f"bytes={start}-"
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, read=120), follow_redirects=True)
    try:
        if start < f.size:
            with client.stream("GET", url, headers=headers) as r:
                if r.status_code in (401, 403):
                    raise GatedError(f"{r.status_code} from {url}")
                if r.status_code == 404:
                    raise RuntimeError(f"not found: {url}")
                r.raise_for_status()
                if start and r.status_code != 206:      # the server ignored Range: start over
                    h, algo = _hasher(f)
                    start = 0
                done, t0, last = start, time.time(), 0.0
                with open(part, "ab" if start else "wb") as out:
                    for chunk in r.iter_bytes(1 << 20):
                        out.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        ctx["done"] += len(chunk)
                        now = time.time()
                        if now - last >= 0.5 or done >= f.size:
                            last = now
                            rate = (done - start) / max(now - t0, 1e-6) / 1e6
                            emit("progress", model=model_id, file=f.dest, done=done, total=f.size,
                                 overall_done=ctx["done"], overall_total=ctx["total"], rate_mbs=round(rate, 1))
    finally:
        if own:
            client.close()
    size = part.stat().st_size
    if size != f.size:
        raise RuntimeError(f"{f.dest}: got {size} bytes, expected {f.size} (run the same command again to resume)")
    emit("verify", model=model_id, file=f.dest, algo=algo)
    got = h.hexdigest()
    if got != f.checksum:
        part.unlink()
        raise ChecksumError(f"{f.dest}: {algo} {got[:12]}… doesn't match the manifest's {f.checksum[:12]}… "
                            "(the partial file was removed; run the command again)")


def _install(m: Model, f: File, src: Source, part: Path) -> Path:
    final = file_path(m, f, src)
    final.parent.mkdir(parents=True, exist_ok=True)
    if m.root != "hf_cache":
        os.replace(part, final)
        return final
    repo_dir = _cache_dir(root_dir("hf_cache"), src.repo)
    blob = repo_dir / "blobs" / f.checksum
    os.replace(part, blob)
    if final.is_symlink() or final.exists():
        final.unlink()
    final.symlink_to(os.path.relpath(blob, final.parent))
    ref = repo_dir / "refs" / "main"
    if not ref.exists():
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(src.revision)
    return final


def _fs_key(p: Path) -> tuple[int, Path]:
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return os.stat(probe).st_dev, probe


def select(names: list[str] | None = None, required_only: bool = False, all_: bool = False,
           feature: str | None = None) -> list[Model]:
    models = load()
    if names:
        known = {m.id for m in models}
        bad = [n for n in names if n not in known]
        if bad:
            raise ValueError(f"unknown model(s): {', '.join(bad)} (see ap models list)")
        return [m for m in models if m.id in names]
    if all_:
        return [m for m in models if (feature is None or m.feature == feature) and m.need != "experimental"]
    if required_only:
        return [m for m in models if required(m) and (feature is None or m.feature == feature)]
    raise ValueError("name the models to download, or use --required / --all")


def download(models: list[Model], emit=None, dry_run: bool = False, client=None) -> dict:
    """Fetch every missing file of `models`. Returns {installed, failed, skipped}."""
    emit = emit or Events()
    todo, skipped = [], []
    for m in models:
        if m.root == "comfyui" and root_dir("comfyui") is None:
            emit("error", model=m.id, kind="config", message="the ComfyUI models folder isn't set",
                 fix="ap setup (ComfyUI models folder), or ap config set comfyui.models_dir <your ComfyUI>/models")
            skipped.append(m.id)
            continue
        src = m.source()
        files = [f for f in src.files if not _present(m, f, src)]
        if not files:
            emit("skip", model=m.id)
            continue
        todo.append((m, src, files))
    total = sum(f.size for _, _, fs in todo for f in fs)
    resume = sum(min(_part(m, f, s).stat().st_size, f.size) for m, s, fs in todo for f in fs if _part(m, f, s).is_file())
    emit("plan", models=len(todo), files=sum(len(fs) for _, _, fs in todo), bytes=total - resume, resume_bytes=resume,
         items=[{"model": m.id, "files": [f.dest for f in fs], "bytes": sum(f.size for f in fs), "gated": m.gated} for m, _, fs in todo])
    # disk space per filesystem, before anything is fetched
    need: dict[int, list] = {}
    for m, s, fs in todo:
        dev, probe = _fs_key(_part(m, fs[0], s).parent)
        left = sum(f.size - (min(_part(m, f, s).stat().st_size, f.size) if _part(m, f, s).is_file() else 0) for f in fs)
        need.setdefault(dev, [probe, 0])[1] += left
    short = False
    for dev, (probe, n) in need.items():
        free = shutil.disk_usage(probe).free
        emit("disk", where=str(probe), need=n, free=free)
        if n + DISK_MARGIN > free:
            short = True
            emit("error", kind="disk", message=f"not enough space on {probe}: {gb(n)} needed (+2 GB margin), {gb(free)} free",
                 fix="free some space, or point the folder at a bigger drive: ap config set paths.ap_root ... "
                     "(models, cache) or comfyui.models_dir ... (ComfyUI models)")
    if dry_run or short:
        emit("done", installed=0, failed=len(todo) if short else 0, skipped=skipped, dry_run=dry_run)
        return {"installed": [], "failed": [m.id for m, _, _ in todo] if short else [], "skipped": skipped}
    token = hf_token()
    ctx = {"done": 0, "total": total}
    installed, failed = [], list(skipped)
    for m, src, files in todo:
        if m.gated and not token:
            emit("error", model=m.id, kind="gated", message=f"{src.repo} is gated and no Hugging Face token is set",
                 fix=gated_help(m, False))
            failed.append(m.id)
            continue
        try:
            for f in files:
                part = _part(m, f, src)
                url = f"{endpoint()}/{src.repo}/resolve/{src.revision}/{f.src}"
                _fetch(url, part, f, token, emit, ctx, m.id, client)
                _install(m, f, src, part)
                emit("file_done", model=m.id, file=f.dest)
            emit("model_done", model=m.id, where=status(m)["where"])
            installed.append(m.id)
        except GatedError as e:
            emit("error", model=m.id, kind="gated", message=f"no access to {src.repo} ({e})", fix=gated_help(m, bool(token)))
            failed.append(m.id)
        except ChecksumError as e:
            emit("error", model=m.id, kind="checksum", message=str(e))
            failed.append(m.id)
        except Exception as e:                        # noqa: BLE001 - network and file errors, per model
            emit("error", model=m.id, kind="network", message=str(e),
                 fix="run the same command again: finished files are kept and the partial one resumes")
            failed.append(m.id)
    emit("done", installed=len(installed), failed=len(failed))
    return {"installed": installed, "failed": failed, "skipped": skipped}


def verify(models: list[Model], emit=None) -> list[str]:
    """Hash installed files against the manifest; returns the ids with a mismatch."""
    emit = emit or Events()
    bad = []
    for m in models:
        src = m.source()
        for f in src.files:
            p = _present(m, f, src)
            if not p:
                continue
            h, algo = _hasher(f)
            emit("verify", model=m.id, file=f.dest, algo=algo)
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 22), b""):
                    h.update(chunk)
            if h.hexdigest() != f.checksum:
                emit("error", model=m.id, kind="checksum", message=f"{f.dest}: {algo} mismatch",
                     fix=f"delete {p} and run {download_command([m.id])}")
                bad.append(m.id)
                break
    return bad
