import os
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

API_BASE = "https://lab.flashalpha.com"
API_KEY = os.getenv("FLASHALPHA_API_KEY", "").strip()

ALLOWED = {"META", "MU", "TSLA", "AMD", "AVGO", "SNDK"}
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "21600"))
REQUEST_DELAY = 1.5

_cache: Dict[str, Any] = {}

app = FastAPI(title="GEX Dashboard", version="6.2.0")


@app.get("/health")
def health():
    return {"ok": True, "api_key_configured": bool(API_KEY)}


@app.get("/symbols")
def symbols():
    return {"supported_symbols": sorted(ALLOWED)}


async def request_api(path, params=None):
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
                502, f"Upstream connection error: {exc}"
            )

    if response.is_error:
        detail = response.text[:1500] or "No response body."

        retry_after = response.headers.get("Retry-After")
        message = (
            f"FlashAlpha HTTP {response.status_code}: {detail}"
        )

        if retry_after:
            message += f" | Retry-After: {retry_after}"

        raise HTTPException(response.status_code, message)

    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(502, "FlashAlpha returned invalid JSON.")

    if not isinstance(payload, dict):
        raise HTTPException(502, "Unexpected FlashAlpha response.")

    return payload


async def cached_request(path, key, params=None):
    now = time.time()
    item = _cache.get(key)

    if item and now - item["time"] < CACHE_TTL:
        return {
            **item["data"],
            "proxy_cache_age_seconds": int(now - item["time"]),
            "proxy_stale": False
        }

    try:
        data = await request_api(path, params)
        _cache[key] = {
            "time": time.time(),
            "data": data
        }

        return {
            **data,
            "proxy_cache_age_seconds": 0,
            "proxy_stale": False
        }

    except HTTPException:
        if item:
            return {
                **item["data"],
                "proxy_cache_age_seconds": int(now - item["time"]),
                "proxy_stale": True
            }
        raise


async def nearest_expiration(symbol):
    key = f"expirations:{symbol}"
    now = time.time()
    item = _cache.get(key)

    if item and now - item["time"] < CACHE_TTL:
        options = item["data"]
    else:
        options = await request_api(f"/v1/options/{symbol}")
        _cache[key] = {
            "time": time.time(),
            "data": options
        }

    rows = options.get("expirations", [])

    today = datetime.now(
        ZoneInfo("America/New_York")
    ).date().isoformat()

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


@app.get("/cached/{symbol}")
def cached_gex(symbol: str):
    symbol = symbol.upper()

    if symbol not in ALLOWED:
        raise HTTPException(400, "Unsupported symbol.")

    matches = [
        (key, value)
        for key, value in _cache.items()
        if key.startswith(f"gex:{symbol}:")
    ]

    if not matches:
        raise HTTPException(
            404, "No saved GEX data for this symbol yet."
        )

    key, item = max(matches, key=lambda x: x[1]["time"])
    age = int(time.time() - item["time"])

    return {
        **item["data"],
        "selected_expiration": key.split(":")[-1],
        "proxy_cache_age_seconds": age,
        "proxy_stale": age >= CACHE_TTL
    }


@app.get("/expirations/{symbol}")
async def expirations(symbol: str):
    symbol = symbol.upper()

    if symbol not in ALLOWED:
        raise HTTPException(400, "Unsupported symbol.")

    return await cached_request(
        f"/v1/options/{symbol}",
        f"expirations:{symbol}"
    )


@app.get("/gex/{symbol}")
async def gex(
    symbol: str,
    expiration: Optional[str] = Query(default=None)
):
    symbol = symbol.upper()

    if symbol not in ALLOWED:
        raise HTTPException(400, "Unsupported symbol.")

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


@app.post("/refresh-all")
async def refresh_all():
    results = []

    for symbol in sorted(ALLOWED):
        try:
            expiration = await nearest_expiration(symbol)

            await asyncio.sleep(REQUEST_DELAY)

            gex_data = await request_api(
                f"/v1/exposure/gex/{symbol}",
                params={"expiration": expiration}
            )

            _cache[f"gex:{symbol}:{expiration}"] = {
                "time": time.time(),
                "data": gex_data
            }

            await asyncio.sleep(REQUEST_DELAY)

            levels_data = await request_api(
                f"/v1/exposure/levels/{symbol}"
            )

            _cache[f"levels:{symbol}"] = {
                "time": time.time(),
                "data": levels_data
            }

            results.append({
                "symbol": symbol,
                "status": "updated",
                "expiration": expiration
            })

        except HTTPException as exc:
            results.append({
                "symbol": symbol,
                "status": "failed",
                "http_status": exc.status_code,
                "error": str(exc.detail),
                "previous_data_kept": any(
                    key.startswith(f"gex:{symbol}:")
                    for key in _cache
                )
            })

            # Stop immediately on quota/rate-limit response.
            # Do not waste requests on the remaining symbols.
            if exc.status_code == 429:
                for remaining in sorted(ALLOWED):
                    if remaining not in {
                        item["symbol"] for item in results
                    }:
                        results.append({
                            "symbol": remaining,
                            "status": "skipped",
                            "error": "Skipped because FlashAlpha returned HTTP 429."
                        })
                break

        except Exception as exc:
            results.append({
                "symbol": symbol,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"
            })

        await asyncio.sleep(REQUEST_DELAY)

    return {
        "results": results,
        "updated": sum(
            1 for item in results
            if item["status"] == "updated"
        ),
        "failed": sum(
            1 for item in results
            if item["status"] == "failed"
        )
    }


DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GEX Dashboard</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0b1020;color:#e8edf7;font-family:Arial,sans-serif}
header{padding:18px 16px;border-bottom:1px solid #273148}
h1{font-size:22px;margin:0}
.sub{color:#9ba9c2;font-size:12px;margin-top:6px}
main{max-width:1100px;margin:auto;padding:15px}
select,button{background:#18233a;color:white;border:1px solid #34435f;border-radius:9px;padding:12px;font-size:15px}
button{cursor:pointer}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
#status{font-size:12px;color:#9ba9c2}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.card{background:#131d31;border:1px solid #26344e;border-radius:13px;padding:15px;min-width:0}
.card small{color:#9ba9c2;font-size:12px}
.value{font-size:23px;font-weight:bold;margin-top:9px;overflow-wrap:anywhere}
.chart{margin-top:14px;background:#131d31;border:1px solid #26344e;border-radius:13px;padding:10px}
.chart h2{font-size:15px;margin:5px 5px 12px}
iframe{width:100%;height:430px;border:0;border-radius:8px}
.section{margin-top:15px}
.section h2{font-size:16px}
.notice{font-size:12px;color:#f4c66a;margin:12px 2px;overflow-wrap:anywhere}
.error{color:#ff8585}
.trade-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:10px 7px;border-bottom:1px solid #26344e;text-align:right}
th{color:#9ba9c2}
td:first-child,th:first-child{text-align:left}
.tablewrap{overflow-x:auto}
@media(min-width:700px){
.grid{grid-template-columns:repeat(4,1fr)}
.trade-grid{grid-template-columns:repeat(3,1fr)}
}
</style>
</head>
<body>
<header>
<h1>GEX Dashboard</h1>
<div class="sub">Gamma Exposure · TradingView · Trade Plan</div>
</header>
<main>
<div class="toolbar">
<select id="ticker">
<option>META</option><option>MU</option><option>TSLA</option>
<option>AMD</option><option>AVGO</option><option>SNDK</option>
</select>
<button onclick="loadData()">Refresh</button>
<button onclick="refreshAll()">Refresh All</button>
<span id="status">Loading saved data...</span>
</div>
<div id="notice" class="notice"></div>

<div class="grid">
<div class="card"><small>Underlying Price</small><div class="value" id="price">—</div></div>
<div class="card"><small>Gamma Flip</small><div class="value" id="flip">—</div></div>
<div class="card"><small>Net GEX</small><div class="value" id="netgex">—</div></div>
<div class="card"><small>Expiration</small><div class="value" id="expiry">—</div></div>
</div>

<div class="section">
<h2>Trade Plan</h2>
<div class="grid trade-grid">
<div class="card"><small>Entry</small><div class="value" id="entry">—</div></div>
<div class="card"><small>Target 1</small><div class="value" id="target1">—</div></div>
<div class="card"><small>Target 2</small><div class="value" id="target2">—</div></div>
<div class="card"><small>Target 3</small><div class="value" id="target3">—</div></div>
<div class="card"><small>Stop Loss</small><div class="value" id="stopLoss">—</div></div>
<div class="card"><small>Liquidity Above</small><div class="value" id="liqAbove">—</div><small id="liqBelow">Below: —</small></div>
</div>
<div class="notice">Trade levels are calculated from available GEX strikes. They are reference levels, not guaranteed price targets.</div>
</div>

<div class="chart">
<h2 id="chartTitle">META · TradingView</h2>
<iframe id="tv" src="https://s.tradingview.com/widgetembed/?symbol=NASDAQ%3AMETA&interval=15&hidesidetoolbar=1&symboledit=1&saveimage=0&toolbarbg=0b1020&theme=dark&style=1&timezone=Asia%2FRiyadh&withdateranges=1&hideideas=1" allowtransparency="true" scrolling="no"></iframe>
</div>

<div class="section card">
<h2>GEX Strikes</h2>
<div class="tablewrap"><table>
<thead><tr><th>Strike</th><th>Net GEX</th></tr></thead>
<tbody id="levels"><tr><td colspan="2">No saved data yet.</td></tr></tbody>
</table></div>
</div>

<div class="section card">
<h2>Market Levels</h2>
<div class="tablewrap"><table>
<thead><tr><th>Level</th><th>Value</th></tr></thead>
<tbody id="marketLevels"><tr><td colspan="2">Not loaded.</td></tr></tbody>
</table></div>
</div>

<div class="sub" style="padding:15px 2px">FlashAlpha data · TradingView chart · Not investment advice.</div>
</main>

<script>
const $=id=>document.getElementById(id);

function number(v){
 if(v===null||v===undefined||v==="")return "—";
 const n=Number(v);
 return Number.isFinite(n)?n.toLocaleString("en-US",{maximumFractionDigits:2}):String(v);
}
function compact(v){
 if(v===null||v===undefined||v==="")return "—";
 const n=Number(v);
 return Number.isFinite(n)?n.toLocaleString("en-US",{notation:"compact",maximumFractionDigits:2}):String(v);
}
function findRows(data){
 for(const item of [data.strikes,data.rows,data.data,data.exposures,data.gex_by_strike,data.strike_data]){
  if(Array.isArray(item))return item;
 }
 return [];
}
function normalizedRows(data){
 return findRows(data).map(r=>({
  strike:Number(r.strike??r.price??r.strike_price),
  gex:Number(r.net_gex??r.gex??r.total_gex??r.value)
 })).filter(r=>Number.isFinite(r.strike)&&Number.isFinite(r.gex));
}
function renderRows(data){
 const rows=normalizedRows(data);
 if(!rows.length){
  $("levels").innerHTML='<tr><td colspan="2">No strike breakdown found.</td></tr>';
  return;
 }
 rows.sort((a,b)=>a.strike-b.strike);
 $("levels").innerHTML=rows.slice(0,30).map(r=>
  `<tr><td>${number(r.strike)}</td><td>${compact(r.gex)}</td></tr>`
 ).join("");
}
function renderMarketLevels(data){
 if(!data||typeof data!=="object"){
  $("marketLevels").innerHTML='<tr><td colspan="2">No levels available.</td></tr>';
  return;
 }
 const entries=Object.entries(data).filter(([k,v])=>
  !["symbol","proxy_cache_age_seconds","proxy_stale"].includes(k)&&
  (typeof v==="string"||typeof v==="number")
 );
 $("marketLevels").innerHTML=entries.length?
  entries.slice(0,20).map(([k,v])=>
   `<tr><td>${k.replaceAll("_"," ")}</td><td>${number(v)}</td></tr>`
  ).join(""):
  '<tr><td colspan="2">No levels available.</td></tr>';
}
function setChart(symbol){
 $("chartTitle").textContent=symbol+" · TradingView";
 $("tv").src="https://s.tradingview.com/widgetembed/?symbol=NASDAQ%3A"+
 encodeURIComponent(symbol)+
 "&interval=15&hidesidetoolbar=1&symboledit=1&saveimage=0&toolbarbg=0b1020&theme=dark&style=1&timezone=Asia%2FRiyadh&withdateranges=1&hideideas=1";
}
function showGex(data){
 $("price").textContent=number(data.underlying_price);
 $("flip").textContent=number(data.gamma_flip);
 $("netgex").textContent=compact(data.net_gex);
 $("expiry").textContent=data.selected_expiration||"—";
 renderRows(data);
}
function updateTradePlan(data){
 const spot=Number(data.underlying_price);
 const rows=normalizedRows(data);
 if(!Number.isFinite(spot)){
  ["entry","target1","target2","target3","stopLoss","liqAbove","liqBelow"].forEach(id=>$(id).textContent="—");
  return;
 }
 $("entry").textContent=number(spot);
 const above=rows.filter(r=>r.strike>spot).sort((a,b)=>a.strike-b.strike);
 const below=rows.filter(r=>r.strike<spot).sort((a,b)=>b.strike-a.strike);
 $("target1").textContent=above[0]?number(above[0].strike):"—";
 $("target2").textContent=above[1]?number(above[1].strike):"—";
 $("target3").textContent=above[2]?number(above[2].strike):"—";
 $("stopLoss").textContent=below[0]?number(below[0].strike):"—";
 const up=above.slice().sort((a,b)=>Math.abs(b.gex)-Math.abs(a.gex))[0];
 const down=below.slice().sort((a,b)=>Math.abs(b.gex)-Math.abs(a.gex))[0];
 $("liqAbove").textContent=up?number(up.strike):"—";
 $("liqBelow").textContent="Below: "+(down?number(down.strike):"—");
}
function gexKey(s){return "flashalpha_gex_"+s}
function levelsKey(s){return "flashalpha_levels_"+s}
function saveBrowserData(s,g,l){
 try{
  if(g)localStorage.setItem(gexKey(s),JSON.stringify(g));
  if(l)localStorage.setItem(levelsKey(s),JSON.stringify(l));
 }catch(e){}
}
function readBrowserData(k){
 try{return JSON.parse(localStorage.getItem(k)||"null")}catch(e){return null}
}
async function loadMarketLevels(){
 const s=$("ticker").value;
 try{
  const r=await fetch("/levels/"+s);
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||"Levels unavailable");
  localStorage.setItem(levelsKey(s),JSON.stringify(d));
  renderMarketLevels(d);
 }catch(e){
  const saved=readBrowserData(levelsKey(s));
  if(saved)renderMarketLevels(saved);
  else $("marketLevels").innerHTML='<tr><td colspan="2">Levels unavailable: '+e.message+'</td></tr>';
 }
}
async function loadCachedData(){
 const s=$("ticker").value;
 $("status").textContent="Reading saved data...";
 $("notice").textContent="";
 $("notice").classList.remove("error");
 setChart(s);
 try{
  const r=await fetch("/cached/"+s);
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||"No saved data");
  localStorage.setItem(gexKey(s),JSON.stringify(d));
  showGex(d);
  updateTradePlan(d);
  await loadMarketLevels();
  const age=d.proxy_cache_age_seconds||0;
  $("notice").textContent="Saved data · Last update: "+Math.floor(age/3600)+"h "+Math.floor((age%3600)/60)+"m ago.";
  $("status").textContent=d.proxy_stale?"Saved / stale":"Saved data";
 }catch(e){
  const saved=readBrowserData(gexKey(s));
  if(saved){
   showGex(saved);
   updateTradePlan(saved);
   const l=readBrowserData(levelsKey(s));
   if(l)renderMarketLevels(l);
   $("notice").textContent="Showing last browser-saved data. Press Refresh for latest data.";
   $("status").textContent="Browser saved data";
   return;
  }
  $("price").textContent="—";
  $("flip").textContent="—";
  $("netgex").textContent="—";
  $("expiry").textContent="—";
  $("levels").innerHTML='<tr><td colspan="2">No saved GEX data.</td></tr>';
  $("marketLevels").innerHTML='<tr><td colspan="2">No saved data.</td></tr>';
  updateTradePlan({});
  $("notice").textContent="No saved data for "+s+". No successful update has been saved yet.";
  $("status").textContent="No saved data";
 }
}
async function loadData(){
 const s=$("ticker").value;
 $("status").textContent="Requesting FlashAlpha...";
 $("notice").textContent="";
 $("notice").classList.remove("error");
 setChart(s);
 try{
  const r=await fetch("/gex/"+s);
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||"API error");
  showGex(d);
  updateTradePlan(d);
  let l=null;
  try{
   const lr=await fetch("/levels/"+s);
   if(lr.ok){l=await lr.json();renderMarketLevels(l)}
  }catch(e){}
  saveBrowserData(s,d,l);
  $("status").textContent=d.proxy_stale?"Saved / stale":"Updated successfully";
  $("notice").textContent=d.proxy_stale?"FlashAlpha unavailable. Showing saved data.":"Data received from FlashAlpha.";
 }catch(e){
  $("status").textContent="Update failed";
  $("notice").textContent="⚠ "+e.message;
  $("notice").classList.add("error");
  const saved=readBrowserData(gexKey(s));
  if(saved){
   showGex(saved);
   updateTradePlan(saved);
   const l=readBrowserData(levelsKey(s));
   if(l)renderMarketLevels(l);
  }
 }
}
async function refreshAll(){
 $("status").textContent="Refreshing symbols one by one...";
 $("notice").textContent="Please wait. Requests are sent sequentially.";
 $("notice").classList.remove("error");
 try{
  const r=await fetch("/refresh-all",{method:"POST"});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||"Refresh failed");
  const results=d.results||[];
  const updated=results.filter(x=>x.status==="updated").length;
  const failed=results.filter(x=>x.status==="failed");
  const skipped=results.filter(x=>x.status==="skipped");
  $("status").textContent=updated+"/6 symbols updated";
  if(failed.length||skipped.length){
   $("notice").classList.add("error");
   $("notice").textContent=results.filter(x=>x.status!=="updated").map(x=>
    x.symbol+": "+(x.error||"Failed")+
    (x.http_status?" (HTTP "+x.http_status+")":"")
   ).join(" | ");
  }else{
   $("notice").textContent="All six symbols updated successfully.";
  }
  for(const item of results){
   if(item.status!=="updated")continue;
   try{
    const gr=await fetch("/cached/"+item.symbol);
    const g=gr.ok?await gr.json():null;
    const lr=await fetch("/levels/"+item.symbol);
    const l=lr.ok?await lr.json():null;
    saveBrowserData(item.symbol,g,l);
   }catch(e){}
  }
 }catch(e){
  $("status").textContent="Refresh All failed";
  $("notice").textContent="⚠ "+e.message;
  $("notice").classList.add("error");
 }
}
$("ticker").addEventListener("change",loadCachedData);
loadCachedData();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)
