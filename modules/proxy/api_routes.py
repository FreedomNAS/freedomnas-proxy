"""
/api/proxy/* — the reverse proxy.

GET    /status        — Caddy installed and running, set up, every site (whichever
                        module wrote it) and FreedomNAS's CA fingerprint, and the job
POST   /setup         — install Caddy if needed, take over its configuration with
                        FreedomNAS's certificate and start it at boot (a job)
POST   /disable       — stop Caddy, keep it off at boot and put the apps behind it
                        back on their own ports (a job)
POST   /sites         — {name, port, upstream}: serve upstream over HTTPS on port
DELETE /sites/{name}  — remove a site added by hand
POST   /names         — {enabled}: serve every site by name too, https://<name>.<hostname>.home.arpa
"""

import asyncio
import importlib.util
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.deps import authenticate, get_config, get_engine


def _load(stem: str):
    name = f"freedomnas_proxy_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{stem}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


proxy = _load("proxy")
router = APIRouter()

_lock = threading.Lock()
_job: dict = {"kind": None, "state": "idle", "error": None, "started": None, "finished": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error_text(exc: Exception) -> str:
    text = str(exc)
    marker = "Stderr:"
    return text.split(marker, 1)[1].strip() if marker in text else text


def _run(kind: str, work) -> None:
    try:
        work()
        _job.update(state="done", error=None)
    except Exception as exc:
        proxy.logger.error("Proxy %s failed: %s", kind, exc)
        _job.update(state="failed", error=_error_text(exc))
    finally:
        _job["finished"] = _now()


def _start_job(kind: str, work):
    with _lock:
        if _job["state"] == "running":
            return JSONResponse(status_code=409, content={"detail": f"A {_job['kind']} is already running"})
        _job.update(kind=kind, state="running", error=None, started=_now(), finished=None)
    threading.Thread(target=_run, args=(kind, work), daemon=True).start()
    return {"job": dict(_job)}


async def _call(func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except proxy.ProxyError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"detail": _error_text(exc)})


@router.get("/status")
async def status(username: str = Depends(authenticate)):
    result = await asyncio.to_thread(proxy.status, get_engine(), get_config())
    result["job"] = dict(_job)
    return result


@router.post("/setup")
async def setup(username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    return _start_job("setup", lambda: proxy.setup(engine, config))


@router.post("/disable")
async def disable(username: str = Depends(authenticate)):
    engine, config = get_engine(), get_config()
    if not proxy.enabled(config):
        return JSONResponse(status_code=422, content={"detail": "The reverse proxy is not set up"})
    return _start_job("disable", lambda: proxy.disable(engine, config))


@router.post("/sites")
async def add_site(body: dict, username: str = Depends(authenticate)):
    return await _call(proxy.add_site, get_engine(), get_config(),
                       str(body.get("name") or "").strip(), body.get("port"), str(body.get("upstream") or ""))


@router.post("/names")
async def names(body: dict, username: str = Depends(authenticate)):
    return await _call(proxy.set_names, get_engine(), get_config(), bool(body.get("enabled")))


@router.delete("/sites/{name}")
async def remove_site(name: str, username: str = Depends(authenticate)):
    result = await _call(proxy.remove_site, get_engine(), get_config(), name)
    return result if isinstance(result, JSONResponse) else {"removed": name}
