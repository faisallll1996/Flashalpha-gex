
import os
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import HTMLResponse

API_BASE = "https://lab.flashalpha.com"
API_KEY = os.getenv("FLASHALPHA_API_KEY", "").strip()

ALLOWED = ["META", "MU", "TSLA", "AMD", "AVGO", "SNDK"]
MAX_DAILY_REQUESTS = 5
CACHE_TTL = 21600

app = FastAPI(title="GEX Dashboard", version="7.0.0")

cache = {}
daily_requests = 0
daily_date = datetime.now(timezone.utc).date()
request_lock = asyncio.Lock()


def reset_daily_counter():
    global daily_requests, daily_date
    today = datetime.now(timezone.utc).date()
    if today != daily_date:
        daily_requests = 0
        daily_date = today


async def request_api(path, params=None):
    global daily_requests

    if not API_KEY:
        raise HTTPException(503, "FLASHALPHA_API_KEY is not configured.")

    async with request_lock:
        reset_daily_counter()

        if daily_requests >= MAX_DAILY_REQUESTS:
            raise HTTPException(
                429,
                "Dashboard daily request limit reached (5). "
                "Try again after the next UTC day."
            )

        daily_requests += 1

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.get(
                f"{API_BASE}{path}",
                headers={"X-Api-Key": API_KEY},
                params=params
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"FlashAlpha connection error: {exc}")

    if response.is_error:
        detail = response.text[:1200]
        retry = response.headers.get("Retry-After", "")
        message = f"FlashAlpha HTTP {response.status_code}: {detail}"
        if retry:
            message += f" | Retry-After: {retry}"
        raise HTTPException(response.status_code, message)

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(502, "FlashAlpha returned invalid JSON.")

    if not isinstance(data, dict):
        raise HTTPException(502, "Unexpected FlashAlpha response.")

    return data


def validate_symbol(symbol):
    symbol = symbol.upper()
    if symbol not in ALLOWED:
        raise HTTPException(400, "Unsupported symbol.")
    return symbol


def save_gex(symbol, expiration, data):
    cache[f"gex:{symbol}:{expiration}"] = {
        "time": time.time(),
        "data": data
    }


def get_saved_gex(symbol):
    matches = [
        (key, item)
        for key, item in cache.items()
        if key.startswith(f"gex:{symbol}:")
    ]
    if not matches:
        return None

    key, item = max(matches, key=lambda x: x[1]["time"])
    age = int(time.time() - item["time"])

    return {
        **item["data"],
        "selected_expiration": key.split(":")[-1],
        "proxy_cache_age_seconds": age,
        "proxy_stale": age >= CACHE_TTL
    }


@app.get("/health")
def health():
    reset_daily_counter()
    return {
        "ok": True,
        "api_key_configured": bool(API_KEY),
        "requests_used_by_dashboard": daily_requests,
        "requests_limit": MAX_DAILY_REQUESTS,
        "utc_date": str(daily_date)
    }


@app.get("/symbols")
def symbols():
    return {"supported_symbols": ALLOWED}


@app.get("/cached/{symbol}")
def cached(symbol: str):
    symbol = validate_symbol(symbol)
    data = get_saved_gex(symbol)

    if not data:
        raise HTTPException(404, "No saved GEX data for this symbol yet.")

    return data


@app.get("/expirations/{symbol}")
async def expirations(symbol: str):
    symbol = validate_symbol(symbol)

    data = await request_api(f"/v1/options/{symbol}")
    rows = data.get("expirations", [])

    today = datetime.now(timezone.utc).date().isoformat()
    dates = sorted({
        row.get("expiration")
        for row in rows
        if isinstance(row, dict)
        and row.get("expiration")
        and row.get("expiration") > today
    })

    if not dates:
        raise HTTPException(404, f"No future expiration found for {symbol}.")

    return {"symbol": symbol, "expirations": dates}


@app.get("/gex/{symbol}")
async def gex(
    symbol: str,
    expiration: Optional[str] = Query(default=None)
):
    symbol = validate_symbol(symbol)

    if not expiration:
        raise HTTPException(
            400,
            "Expiration is required. Use /expirations/{symbol} first."
        )

    try:
        datetime.strptime(expiration, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Expiration must be YYYY-MM-DD.")

    key = f"gex:{symbol}:{expiration}"
    old = cache.get(key)

    if old and time.time() - old["time"] < CACHE_TTL:
        return {
            **old["data"],
            "selected_expiration": expiration,
            "proxy_cache_age_seconds": int(time.time() - old["time"]),
            "proxy_stale": False
        }

    try:
        data = await request_api(
            f"/v1/exposure/gex/{symbol}",
            params={"expiration": expiration}
        )
        save_gex(symbol, expiration, data)

        return {
            **data,
            "selected_expiration": expiration,
            "proxy_cache_age_seconds": 0,
            "proxy_stale": False
        }

    except HTTPException:
        if old:
            return {
                **old["data"],
                "selected_expiration": expiration,
                "proxy_cache_age_seconds": int(time.time() - old["time"]),
                "proxy_stale": True
            }
        raise


@app.post("/refresh-all")
async def refresh_all(payload: dict = Body(default={})):
    expirations_map = payload.get("expirations", {})
    selected_symbols = payload.get("symbols", ALLOWED)

    selected_symbols = [
        s.upper() for s in selected_symbols
        if s.upper() in ALLOWED
    ]

    results = []

    for symbol in selected_symbols:
        expiration = expirations_map.get(symbol)

        if not expiration:
            results.append({
                "symbol": symbol,
                "status": "skipped",
                "error": "No saved expiration. Load expiration first."
            })
            continue

        try:
            datetime.strptime(expiration, "%Y-%m-%d")

            data = await request_api(
                f"/v1/exposure/gex/{symbol}",
                params={"expiration": expiration}
            )

            save_gex(symbol, expiration, data)

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
                "error": str(exc.detail)
            })

            if exc.status_code == 429:
                break

        except Exception as exc:
            results.append({
                "symbol": symbol,
                "status": "failed",
                "error": str(exc)
            })

    reset_daily_counter()

    return {
        "results": results,
        "updated": sum(x["status"] == "updated" for x in results),
        "requests_used": daily_requests,
        "requests_limit": MAX_DAILY_REQUESTS
    }


HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GEX Dashboard</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0b1020;color:#e8edf7;font-family:Arial,sans-serif}
header{padding:18px;border-bottom:1px solid #273148}
h1{font-size:22px;margin:0}
main{max-width:1100px;margin:auto;padding:15px}
.sub,small{color:#9ba9c2}
.sub{font-size:12px;margin-top:6px}
.toolbar{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}
select,button{background:#18233a;color:white;border:1px solid #34435f;border-radius:9px;padding:12px;font-size:14px}
button{cursor:pointer}
#status{font-size:12px;color:#9ba9c2;align-self:center}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.card{background:#131d31;border:1px solid #26344e;border-radius:13px;padding:15px;min-width:0}
.value{font-size:22px;font-weight:bold;margin-top:9px;overflow-wrap:anywhere}
.section{margin-top:16px}
.section h2{font-size:16px}
.notice{font-size:12px;color:#f4c66a;margin:12px 2px;overflow-wrap:anywhere}
.error{color:#ff8585}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:10px 7px;border-bottom:1px solid #26344e;text-align:right}
td:first-child,th:first-child{text-align:left}
.tablewrap{overflow-x:auto}
iframe{width:100%;height:430px;border:0;border-radius:8px}
@media(min-width:700px){.grid{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body>
<header>
<h1>GEX Dashboard</h1>
<div class="sub">Gamma Exposure · Trade Levels · TradingView</div>
</header>
<main>
<div class="toolbar">
<select id="ticker">
<option>META</option><option>MU</option><option>TSLA</option>
<option>AMD</option><option>AVGO</option><option>SNDK</option>
</select>
<button onclick="loadSaved()">Load Saved</button>
<button onclick="refreshOne()">Refresh</button>
<button onclick="refreshAll()">Refresh All</button>
<button onclick="loadExpiry()">Load Expiration</button>
<span id="status">Loading...</span>
</div>

<div id="notice" class="notice"></div>

<div class="grid">
<div class="card"><small>Underlying Price</small><div class="value" id="price">—</div></div>
<div class="card"><small>Gamma Flip</small><div class="value" id="flip">—</div></div>
<div class="card"><small>Net GEX</small><div class="value" id="net">—</div></div>
<div class="card"><small>Expiration</small><div class="value" id="expiry">—</div></div>
<div class="card"><small>Entry Reference</small><div class="value" id="entry">—</div></div>
<div class="card"><small>Stop Reference</small><div class="value" id="stop">—</div></div>
<div class="card"><small>Target 1</small><div class="value" id="t1">—</div></div>
<div class="card"><small>Target 2</small><div class="value" id="t2">—</div></div>
<div class="card"><small>Target 3</small><div class="value" id="t3">—</div></div>
<div class="card"><small>Liquidity Above</small><div class="value" id="above">—</div></div>
<div class="card"><small>Liquidity Below</small><div class="value" id="below">—</div></div>
</div>

<div class="section card">
<h2>GEX Strikes</h2>
<div class="tablewrap">
<table><thead><tr><th>Strike</th><th>Net GEX</th></tr></thead>
<tbody id="rows"><tr><td colspan="2">No saved data.</td></tr></tbody></table>
</div>
</div>

<div class="section card">
<h2>TradingView</h2>
<iframe id="tv" src="https://s.tradingview.com/widgetembed/?symbol=NASDAQ%3AMETA&interval=15&hidesidetoolbar=1&theme=dark&style=1&timezone=Asia%2FRiyadh" scrolling="no"></iframe>
</div>

<div class="notice">
Trade levels are reference levels calculated from available GEX strikes. They are not guaranteed price targets or financial advice.
</div>
</main>

<script>
const $=id=>document.getElementById(id);
const symbols=["META","MU","TSLA","AMD","AVGO","SNDK"];

function key(s){return "flashalpha_gex_"+s}
function expKey(s){return "flashalpha_exp_"+s}

function read(k){
 try{return JSON.parse(localStorage.getItem(k)||"null")}catch(e){return null}
}
function save(s,d){
 try{
  localStorage.setItem(key(s),JSON.stringify(d));
  if(d.selected_expiration)localStorage.setItem(expKey(s),d.selected_expiration);
 }catch(e){}
}
function fmt(v){
 if(v===null||v===undefined||v==="")return "—";
 let n=Number(v);
 return Number.isFinite(n)?n.toLocaleString("en-US",{maximumFractionDigits:2}):String(v);
}
function compact(v){
 if(v===null||v===undefined||v==="")return "—";
 let n=Number(v);
 return Number.isFinite(n)?n.toLocaleString("en-US",{notation:"compact",maximumFractionDigits:2}):String(v);
}
function getRows(d){
 let a=d.strikes||d.rows||d.data||d.exposures||d.gex_by_strike||d.strike_data||[];
 if(!Array.isArray(a))return [];
 return a.map(x=>({
  strike:Number(x.strike??x.price??x.strike_price),
  gex:Number(x.net_gex??x.gex??x.total_gex??x.value)
 })).filter(x=>Number.isFinite(x.strike)&&Number.isFinite(x.gex));
}
function render(d){
 $("price").textContent=fmt(d.underlying_price);
 $("flip").textContent=fmt(d.gamma_flip);
 $("net").textContent=compact(d.net_gex);
 $("expiry").textContent=d.selected_expiration||"—";

 const rows=getRows(d).sort((a,b)=>a.strike-b.strike);
 $("rows").innerHTML=rows.length?
 rows.slice(0,40).map(x=>`<tr><td>${fmt(x.strike)}</td><td>${compact(x.gex)}</td></tr>`).join("")
 :'<tr><td colspan="2">No strike breakdown in this API response.</td></tr>';

 const spot=Number(d.underlying_price);
 const up=rows.filter(x=>x.strike>spot).sort((a,b)=>a.strike-b.strike);
 const down=rows.filter(x=>x.strike<spot).sort((a,b)=>b.strike-a.strike);

 $("entry").textContent=fmt(spot);
 $("stop").textContent=down[0]?fmt(down[0].strike):"—";
 $("t1").textContent=up[0]?fmt(up[0].strike):"—";
 $("t2").textContent=up[1]?fmt(up[1].strike):"—";
 $("t3").textContent=up[2]?fmt(up[2].strike):"—";

 const strongestUp=up.slice().sort((a,b)=>Math.abs(b.gex)-Math.abs(a.gex))[0];
 const strongestDown=down.slice().sort((a,b)=>Math.abs(b.gex)-Math.abs(a.gex))[0];
 $("above").textContent=strongestUp?fmt(strongestUp.strike):"—";
 $("below").textContent=strongestDown?fmt(strongestDown.strike):"—";
}
function setChart(s){
 $("tv").src="https://s.tradingview.com/widgetembed/?symbol=NASDAQ%3A"+
 encodeURIComponent(s)+
 "&interval=15&hidesidetoolbar=1&theme=dark&style=1&timezone=Asia%2FRiyadh";
}
function selected(){return $("ticker").value}

async function loadSaved(){
 const s=selected();
 setChart(s);
 const local=read(key(s));
 if(local){
  render(local);
  $("status").textContent="Browser saved data";
  $("notice").textContent="Showing the last data saved in this browser.";
 }
 try{
  const r=await fetch("/cached/"+s);
  const d=await r.json();
  if(r.ok){
   save(s,d);render(d);
   $("status").textContent="Saved server data";
   $("notice").textContent="Saved data loaded.";
   return;
  }
 }catch(e){}
 if(!local){
  $("status").textContent="No saved data";
  $("notice").textContent="No saved data for "+s+". Load expiration, then refresh.";
 }
}
async function loadExpiry(){
 const s=selected();
 $("status").textContent="Loading expiration...";
 try{
  const r=await fetch("/expirations/"+s);
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||"Expiration request failed");
  const chosen=d.expirations[0];
  localStorage.setItem(expKey(s),chosen);
  $("status").textContent="Expiration saved";
  $("notice").textContent=s+" expiration saved: "+chosen;
 }catch(e){
  $("status").textContent="Failed";
  $("notice").textContent=e.message;
  $("notice").classList.add("error");
 }
}
async function refreshOne(){
 const s=selected();
 let expiration=localStorage.getItem(expKey(s));
 const old=read(key(s));
 if(!expiration&&old)expiration=old.selected_expiration;
 if(!expiration){
  $("notice").textContent="No saved expiration. Press Load Expiration first.";
  return;
 }
 $("status").textContent="Refreshing "+s+"...";
 try{
  const r=await fetch("/gex/"+s+"?expiration="+encodeURIComponent(expiration));
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||"Refresh failed");
  save(s,d);render(d);
  $("status").textContent="Updated "+s;
  $("notice").textContent="Latest GEX data received.";
 }catch(e){
  $("status").textContent="Update failed";
  $("notice").textContent=e.message;
  $("notice").classList.add("error");
  if(old)render(old);
 }
}
async function refreshAll(){
 const known={};
 for(const s of symbols){
  const d=read(key(s));
  const e=localStorage.getItem(expKey(s))||(d&&d.selected_expiration);
  if(e)known[s]=e;
 }
 const available=symbols.filter(s=>known[s]);
 if(!available.length){
  $("notice").textContent="No saved expiration dates. Load expiration for the symbols first.";
  return;
 }
 let start=Number(localStorage.getItem("flashalpha_rotation")||0)%available.length;
 const ordered=available.slice(start).concat(available.slice(0,start));
 const batch=ordered.slice(0,5);
 localStorage.setItem("flashalpha_rotation",String((start+5)%available.length));

 $("status").textContent="Refreshing up to 5 symbols...";
 $("notice").textContent="Sending one GEX request per selected symbol.";
 $("notice").classList.remove("error");

 try{
  const r=await fetch("/refresh-all",{
   method:"POST",
   headers:{"Content-Type":"application/json"},
   body:JSON.stringify({
    symbols:batch,
    expirations:known
   })
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||"Refresh All failed");

  for(const item of d.results||[]){
   if(item.status==="updated"){
    try{
     const cr=await fetch("/cached/"+item.symbol);
     if(cr.ok){
      const saved=await cr.json();
      save(item.symbol,saved);
     }
    }catch(e){}
   }
  }

  const failed=(d.results||[]).filter(x=>x.status!=="updated");
  $("status").textContent=d.updated+" symbols updated";
  $("notice").textContent=failed.length?
   failed.map(x=>x.symbol+": "+x.error).join(" | "):
   "Refresh completed successfully.";
  if(failed.length)$("notice").classList.add("error");
  loadSaved();
 }catch(e){
  $("status").textContent="Refresh All failed";
  $("notice").textContent=e.message;
  $("notice").classList.add("error");
 }
}
$("ticker").addEventListener("change",loadSaved);
loadSaved();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(HTML)
