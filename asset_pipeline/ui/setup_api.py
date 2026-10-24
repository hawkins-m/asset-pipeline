"""Setup screen endpoints (asset_pipeline/setup.py does the work; `ap setup` is the CLI twin).

    GET   /api/setup/first-run     {first_run}: cheap, polled once when the app opens
    GET   /api/setup               every item: current value, detected value, shown or not; GPUs
    POST  /api/setup/test          {id, value, values} -> {ok, warn, message}
    POST  /api/setup/save          {values, secrets} -> {config, storage}
API key values are only ever received (to test or save), never sent back.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import setup


class TestReq(BaseModel):
    id: str
    value: str | None = None
    values: dict[str, str] = {}


class SaveReq(BaseModel):
    values: dict[str, str] = {}
    secrets: dict[str, str] = {}


def register(app: FastAPI) -> None:
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
            return setup.save(req.values, req.secrets)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))
