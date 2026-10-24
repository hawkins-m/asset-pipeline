"""Setup screen endpoints (asset_pipeline/setup.py does the work; `ap setup` is the CLI twin).

    GET   /api/setup/first-run     {first_run}: cheap, polled once when the app opens
    GET   /api/setup               every item: current value, detected value, shown or not; GPUs
    POST  /api/setup/test          {id, value, values} -> {ok, warn, message}
    POST  /api/setup/save          {values, secrets} -> {config, storage}
    GET   /api/doctor[?refresh=1]  doctor.report(): the checks, the features they switch off. Cached
                                   5 min; the first call starts a background run and answers
                                   {pending: true}; refresh=1 waits for a fresh run
API key values are only ever received (to test or save), never sent back.
"""
import threading
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import doctor, setup

_doc = {"report": None, "running": False}
_doc_lock = threading.Lock()
DOCTOR_MAX_AGE_S = 300


def _run_doctor() -> None:
    with _doc_lock:
        if _doc["running"]:
            return
        _doc["running"] = True
    try:
        _doc["report"] = doctor.report()
    finally:
        _doc["running"] = False


def doctor_report(refresh: bool = False, wait: bool = True) -> dict | None:
    """The cached report; a fresh run when asked, when there is none or when it is old. With
    wait=False a run starts in the background and the old report (or None) comes back."""
    rep = _doc["report"]
    stale = rep is None or time.time() - rep["at"] > DOCTOR_MAX_AGE_S
    if refresh or stale:
        if wait:
            _run_doctor()
        else:
            threading.Thread(target=_run_doctor, daemon=True).start()
    return _doc["report"]


class TestReq(BaseModel):
    id: str
    value: str | None = None
    values: dict[str, str] = {}


class SaveReq(BaseModel):
    values: dict[str, str] = {}
    secrets: dict[str, str] = {}


def register(app: FastAPI) -> None:
    @app.get("/api/doctor")
    def doctor_get(refresh: bool = False):
        rep = doctor_report(refresh=refresh, wait=refresh)
        return {"pending": True} if rep is None else rep | {"running": _doc["running"]}

    @app.get("/api/setup/first-run")
    def setup_first_run():
        return {"first_run": setup.first_run()}

    @app.get("/api/setup")
    def setup_state():
        return setup.state()

    @app.post("/api/setup/test")
    def setup_test(req: TestReq):
        try:
            return setup.test(req.id, req.value, req.values)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/setup/save")
    def setup_save(req: SaveReq):
        unknown = [k for k in req.values if k not in setup.BY_ID] + [
            k for k in req.secrets if k not in setup.config.SECRET_KEYS]
        if unknown:
            raise HTTPException(400, f"unknown setup items: {', '.join(unknown)}")
        try:
            out = setup.save(req.values, req.secrets)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))
        doctor_report(refresh=True, wait=False)                  # the Status box catches up
        return out
