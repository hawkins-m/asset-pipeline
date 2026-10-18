"""Routes of the asset version bank (asset_pipeline/versions.py), Tools › Storage… and Settings….

    GET     /api/projects/{slug}/library/asset/{asset}/versions          the strip (migrates lazily)
    GET     .../versions/compare?a=&b=                                   two versions side by side
    POST    .../versions/{n}/restore    job library.restore (cpu)        brief back (+ rebuild) / TRELLIS result current
    POST    .../versions/{n}/pin, /unpin
    DELETE  .../versions/{n}?confirm_pinned=true
    POST    .../versions/prune {dry_run}                                 versions beyond the project's "keep"
    GET     /api/projects/{slug}/storage                                 totals per asset and project
    POST    /api/projects/{slug}/storage/prune {asset?, dry_run}         delete unpinned versions
    POST    /api/projects/{slug}/storage/clean {dry_run}                 shared-store files nothing references
    POST    /api/projects/{slug}/storage/loose                           remove builds that are no version
    GET/PUT /api/projects/{slug}/settings                                {versions_keep}
TRELLIS-route props use the same routes: their versions are the TRELLIS results.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import filestore, versions


class StorageCleanReq(BaseModel):
    dry_run: bool = False


class PruneReq(BaseModel):
    dry_run: bool = True


class StoragePruneReq(BaseModel):
    asset: str | None = None
    dry_run: bool = False


class SettingsReq(BaseModel):
    versions_keep: int


def register(app: FastAPI, jobs, store_of) -> None:
    base = "/api/projects/{slug}/library/asset/{asset}/versions"

    def _call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except PermissionError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get(base)
    def versions_list(slug: str, asset: str):
        return _call(versions.listing, store_of(slug), asset)

    @app.get(base + "/compare")
    def versions_compare(slug: str, asset: str, a: int, b: int):
        return _call(versions.compare, store_of(slug), asset, a, b)

    @app.post(base + "/prune")
    def versions_prune(slug: str, asset: str, req: PruneReq):
        return _call(versions.prune, store_of(slug), asset, dry_run=req.dry_run)

    @app.post(base + "/{n}/restore")
    def versions_restore(slug: str, asset: str, n: int):
        store = store_of(slug)
        _call(versions.get, store, asset, n)
        return jobs.submit("library.restore", slug, lambda: versions.restore(store, asset, n), lane="cpu",
                           tag={"asset": asset, "version": n}).public()

    @app.post(base + "/{n}/pin")
    def versions_pin(slug: str, asset: str, n: int):
        return _call(versions.pin, store_of(slug), asset, n)

    @app.post(base + "/{n}/unpin")
    def versions_unpin(slug: str, asset: str, n: int):
        return _call(versions.unpin, store_of(slug), asset, n)

    @app.delete(base + "/{n}")
    def versions_delete(slug: str, asset: str, n: int, confirm_pinned: bool = False):
        return _call(versions.delete, store_of(slug), asset, n, confirm_pinned=confirm_pinned)

    @app.get("/api/projects/{slug}/storage")
    def storage(slug: str):
        store = store_of(slug)
        loose = versions.loose_files(store)
        return versions.storage(store) | {"files": filestore.stats(store), "loose": loose,
                                          "loose_bytes": sum(r["bytes"] for r in loose)}

    @app.post("/api/projects/{slug}/storage/loose")
    def storage_loose(slug: str):
        return versions.remove_loose(store_of(slug))

    @app.post("/api/projects/{slug}/storage/clean")
    def storage_clean(slug: str, req: StorageCleanReq):
        return filestore.clean(store_of(slug), dry_run=req.dry_run)

    @app.post("/api/projects/{slug}/storage/prune")
    def storage_prune(slug: str, req: StoragePruneReq):
        store = store_of(slug)
        if req.dry_run:
            s = versions.storage(store)
            rows = [r for r in s["assets"] if not req.asset or r["asset"] == req.asset]
            return {"removed": sum(r["removable"] for r in rows), "bytes": sum(r["removable_bytes"] for r in rows)}
        return versions.storage_prune(store, req.asset)

    @app.get("/api/projects/{slug}/settings")
    def settings_get(slug: str):
        return versions.settings(store_of(slug))

    @app.put("/api/projects/{slug}/settings")
    def settings_put(slug: str, req: SettingsReq):
        store = store_of(slug)
        _call(versions.set_keep, store, req.versions_keep)
        return versions.settings(store)
