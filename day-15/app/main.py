from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, HttpUrl
from typing import Optional
import os

from storage import init_db, create_mapping, get_mapping, update_last_accessed
from id_service import generate_id
from cache import cache_get, cache_set, increment_clicks, get_clicks
from events import emit_event

app = FastAPI(title="Day-15 URL Shortener Demo")

class ShortenRequest(BaseModel):
    url: HttpUrl
    custom_alias: Optional[str] = None
    ttl: Optional[int] = None
    owner_id: Optional[str] = None

class ShortenResponse(BaseModel):
    short_id: str
    short_url: str


@app.on_event("startup")
def startup():
    init_db()


@app.post("/shorten", response_model=ShortenResponse)
def shorten(req: ShortenRequest, request: Request):
    # Validate and create mapping
    if req.custom_alias:
        short_id = req.custom_alias
        try:
            create_mapping(short_id=short_id, original_url=str(req.url), ttl=req.ttl, owner_id=req.owner_id)
        except Exception:
            raise HTTPException(status_code=409, detail="alias already exists")
    else:
        # generate id and try insert — simple loop to handle rare collisions
        for _ in range(3):
            short_id = generate_id()
            try:
                create_mapping(short_id=short_id, original_url=str(req.url), ttl=req.ttl, owner_id=req.owner_id)
                break
            except Exception:
                short_id = None
        if not short_id:
            raise HTTPException(status_code=500, detail="failed to generate unique id")

    short_url = f"{request.base_url}r/{short_id}"
    # optionally prime cache
    cache_set(short_id, str(req.url), ttl=req.ttl)
    return ShortenResponse(short_id=short_id, short_url=short_url)


@app.get("/r/{short_id}")
def redirect_short(short_id: str, request: Request):
    # Cache-first
    original = cache_get(short_id)
    if original:
        # increment counters and emit event async
        increment_clicks(short_id)
        emit_event({"type": "click", "short_id": short_id, "from": request.client.host})
        # Note: FastAPI's RedirectResponse is available, but we return a simple tuple to uvicorn
        from fastapi.responses import RedirectResponse
        update_last_accessed(short_id)
        return RedirectResponse(url=original, status_code=302)

    # DB fallback
    mapping = get_mapping(short_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="not found")
    cache_set(short_id, mapping.original_url, ttl=mapping.ttl)
    increment_clicks(short_id)
    emit_event({"type": "click", "short_id": short_id, "from": request.client.host})
    update_last_accessed(short_id)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=mapping.original_url, status_code=302)


@app.get("/stats/{short_id}")
def stats(short_id: str):
    mapping = get_mapping(short_id)
    if not mapping:
        raise HTTPException(status_code=404, detail="not found")
    clicks = get_clicks(short_id)
    return {
        "short_id": short_id,
        "clicks": clicks,
        "created_at": mapping.created_at.isoformat() if mapping.created_at else None,
        "last_accessed": mapping.last_accessed.isoformat() if mapping.last_accessed else None,
        "owner_id": mapping.owner_id,
    }
