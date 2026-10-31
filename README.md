# Equity GEX Proxy (FlashAlpha)

A small private proxy for individual US equities. It does **not** connect to MyOna.
It calls FlashAlpha's documented REST API using your API key stored as a server
environment variable (never in source code or a browser).

## Supported symbols
META, MU, NVDA, TSLA, SNDK. Edit `ALLOWED` in `main.py` to add more.

## Deploy
1. Create a private GitHub repository and upload these files.
2. Create a Railway project from that GitHub repository.
3. In Railway → Variables, add:
   - `FLASHALPHA_API_KEY` = your key (paste it directly into Railway, not into chat/GitHub)
   - optionally `CACHE_TTL_SECONDS` = `900`
4. Deploy. Railway should run the Procfile.
5. Test `/health`, then `/gex/META` and `/levels/META`.

## Important limitations
- FlashAlpha Free currently documents 5 API requests/day and GEX for individual US equities;
  data can be cached up to 15 minutes. A proxy cache reduces repeat calls.
- This server exposes JSON data for a dashboard or other client.
- Pine Script cannot fetch arbitrary external REST URLs or receive pushed API values.
  Therefore this proxy cannot automatically draw these levels inside a TradingView chart.
  A TradingView-native integration requires a supported data-feed/vendor symbol, or the
  levels must be viewed in a separate dashboard. Do not put the API key in Pine Script.
- This is a starter service, not yet deployed. No live data has been fetched until you deploy
  it and configure your own key.
