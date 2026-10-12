# Trading Assistant Backend API

This package includes a native DhanHQ option-chain adapter. It is deployment-ready source, not a public live server. It does not scrape NSE/BSE pages or fabricate OI. Price/history uses best-effort Yahoo Finance chart data and is not a guaranteed exchange-licensed real-time feed.

## Routes
- `GET /health`
- `GET /api/quote?symbol=%5ENSEI&interval=1d`
- `GET /api/history?symbol=%5ENSEI&range=3mo&interval=1d`
- `GET /api/oi?index=NIFTY&expiry=current_week`
- `GET /api/oi?index=SENSEX&expiry=current_week`
- `GET /api/marketbundle?index=NIFTY&expiry=current_week`

## Native DhanHQ OI adapter
Set on the hosting provider (never in the app or source repository):
- `OI_PROVIDER_MODE=dhan`
- `DHAN_CLIENT_ID`
- `DHAN_ACCESS_TOKEN`
- `DHAN_NIFTY_SECURITY_ID` (default 13; verify against current Dhan instrument list)
- `DHAN_SENSEX_SECURITY_ID` (required; verify against current Dhan instrument list)
- `DHAN_INDEX_SEGMENT=IDX_I`

The backend calls Dhan's documented `/v2/optionchain/expirylist` and `/v2/optionchain` endpoints, resolves `current_week`/`current_month` to an actual expiry, and aggregates each strike's `ce.oi`, `ce.previous_oi`, `pe.oi`, and `pe.previous_oi`. Missing credentials, invalid IDs, empty chain, or missing spot/OI fail clearly; no synthetic OI is returned. Access permissions, account terms, token expiry and provider rate limits apply.

Official docs: https://dhanhq.co/docs/v2/option-chain/

## Run locally
`python backend_server/server.py`

Test locally at `http://127.0.0.1:8080/health`. Deploy behind HTTPS before using from another device.

## Alternative custom OI adapter
Set `OI_PROVIDER_MODE=custom`, `OI_PROVIDER_URL` (HTTPS), and optional `OI_PROVIDER_API_KEY`. The custom endpoint must return JSON containing `ok`, `spot`, `call`, `put`, `callChange`, and `putChange`.

No hosting account, live credentials, provider entitlement, or deployment is created by this source package.
