
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query

API_BASE = "https://lab.flashalpha.com"
API_KEY = os.getenv("FLASHALPHA_API_KEY", "").strip()

ALLOWED = {"META", "MU", "TSLA", "AMD", "AVGO", "AAPL"}
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "900"))
_cache: Dict[str, Any] = {}

app = FastAPI(title="Equity GEX Proxy", version="2.0.0")


@app.get("/health")
def health():
    return {"ok": True, "api_key_configured": bool(API_KEY)}


async def request_api(path: str, params=None):
    if not API_KEY:
        raise HTTPException(503, "API key is not configured.")

    async with httpx.AsyncClient(timeout=25) as client:
        try:
            response = await client.get(
                f"{API_BASE}{path}",
                headers={"X-Api-Key": API_KEY},
                params=params
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                502, f"Upstream error: {exc.__class__.__name__}"
            )

    if response.status_code == 401:
        raise HTTPException(502, "FlashAlpha rejected the API key.")
    if response.status_code == 403:
        raise HTTPException(
            403, "Plan does not allow this endpoint, symbol or expiration."
        )
    if response.status_code == 429:
        raise HTTPException(429, "FlashAlpha API quota reached.")
    if response.is_error:
        raise HTTPException(502, f"FlashAlpha HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError:
        raise HTTPException(502, "Invalid JSON from FlashAlpha.")


async def cached_request(path, key, params=None):
    now = time.time()
    item = _cache.get(key)

    if item and now - item["time"] < CACHE_TTL:
        return {
            **item["data"],
            "proxy_cache_age_seconds": int(now - item["time"])
        }

    data = await request_api(path, params)
    _cache[key] = {"time": now, "data": data}

    return {**data, "proxy_cache_age_seconds": 0}


async def nearest_expiration(symbol: str):
    options = await request_api(f"/v1/options/{symbol}")

    rows = options.get("expirations", [])
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    dates = sorted({
        row.get("expiration")
        for row in rows
        if isinstance(row, dict)
        and row.get("expiration", "") > today
    })

    if not dates:
        raise HTTPException(
            404, f"No future expiration found for {symbol}."
        )

    return dates[0]


@app.get("/symbols")
def symbols():
    return {"supported_symbols": sorted(ALLOWED)}


@app.get("/expirations/{symbol}")
async def expirations(symbol: str):
    symbol = symbol.upper()

    if symbol not in ALLOWED:
        raise HTTPException(400, "Unsupported symbol.")

    return await request_api(f"/v1/options/{symbol}")


@app.get("/gex/{symbol}")
async def gex(
    symbol: str,
    expiration: Optional[str] = Query(default=None)
):
    symbol = symbol.upper()

    if symbol not in ALLOWED:
        raise HTTPException(
            400, f"Allowed symbols: {', '.join(sorted(ALLOWED))}"
        )

    selected = expiration or await nearest_expiration(symbol)

    try:
        datetime.strptime(selected, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Expiration must be YYYY-MM-DD.")

    data = await cached_request(
        f"/v1/exposure/gex/{symbol}",
        f"gex:{symbol}:{selected}",
        params={"expiration": selected}
    )

    data["selected_expiration"] = selected
    return data


@app.get("/levels/{symbol}")
async def levels(symbol: str):
    symbol = symbol.upper()

    if symbol not in ALLOWED:
        raise HTTPException(400, "Unsupported symbol.")

    return await cached_request(
        f"/v1/exposure/levels/{symbol}",
        f"levels:{symbol}"
    )
