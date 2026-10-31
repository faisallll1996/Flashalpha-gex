import os
import time
from typing import Dict, Any
import httpx
from fastapi import FastAPI, HTTPException

API_BASE = "https://lab.flashalpha.com"
API_KEY = os.getenv("FLASHALPHA_API_KEY", "").strip()
ALLOWED = {"META", "MU", "NVDA", "TSLA", "SNDK"}
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "900"))  # Free plan may cache up to 15 minutes
_cache: Dict[str, Any] = {}

app = FastAPI(title="Equity GEX Proxy", version="1.0.0")

@app.get("/health")
def health():
    return {"ok": True, "api_key_configured": bool(API_KEY)}

async def fetch(path: str, cache_key: str):
    if not API_KEY:
        raise HTTPException(503, "Set FLASHALPHA_API_KEY in the hosting provider's environment variables.")
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached["time"] < CACHE_TTL:
        return {**cached["data"], "proxy_cache_age_seconds": int(now - cached["time"])}

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.get(
                f"{API_BASE}{path}",
                headers={"X-Api-Key": API_KEY}
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Upstream request failed: {exc.__class__.__name__}")
    if response.status_code == 401:
        raise HTTPException(502, "FlashAlpha rejected the API key. Check the server environment variable.")
    if response.status_code == 403:
        raise HTTPException(403, "Your FlashAlpha plan does not include this endpoint/symbol.")
    if response.status_code == 429:
        raise HTTPException(429, "FlashAlpha daily API quota reached.")
    if response.is_error:
        raise HTTPException(502, f"FlashAlpha returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise HTTPException(502, "FlashAlpha returned a non-JSON response.")
    _cache[cache_key] = {"time": now, "data": data}
    return {**data, "proxy_cache_age_seconds": 0}

@app.get("/gex/{symbol}")
async def gex(symbol: str):
    symbol = symbol.upper()
    if symbol not in ALLOWED:
        raise HTTPException(400, f"Allowed symbols: {', '.join(sorted(ALLOWED))}")
    return await fetch(f"/v1/exposure/gex/{symbol}", f"gex:{symbol}")

@app.get("/levels/{symbol}")
async def levels(symbol: str):
    symbol = symbol.upper()
    if symbol not in ALLOWED:
        raise HTTPException(400, f"Allowed symbols: {', '.join(sorted(ALLOWED))}")
    return await fetch(f"/v1/exposure/levels/{symbol}", f"levels:{symbol}")
