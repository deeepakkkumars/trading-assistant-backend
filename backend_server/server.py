#!/usr/bin/env python3
"""Small HTTPS-friendly proxy API for Trading Assistant. Uses only Python stdlib."""
import json, os, re, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from urllib.request import Request, urlopen

SERVICE = "Trading Assistant Backend"
ALLOWED_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y"}
ALLOWED_INTERVALS = {"1m", "5m", "10m", "15m", "30m", "60m", "1d", "1wk", "1mo"}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9^._=+\-]{1,48}$")


def http_json(url, headers=None, timeout=10):
    req = Request(url, headers={"User-Agent": "TradingAssistantBackend/1.0", "Accept": "application/json", **(headers or {})})
    with urlopen(req, timeout=timeout) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"upstream HTTP {resp.status}")
        raw = resp.read(4_000_000)
    return json.loads(raw.decode("utf-8"))


def fetch_yahoo_chart(symbol, range_="1y", interval="1d"):
    if not SYMBOL_RE.match(symbol or ""):
        raise ValueError("Invalid market symbol")
    range_ = range_ if range_ in ALLOWED_RANGES else "3mo"
    interval = interval if interval in ALLOWED_INTERVALS else "1d"
    requested_interval = interval
    upstream_interval = "5m" if interval == "10m" else interval
    # Yahoo restricts intraday ranges; keep query within commonly supported limits.
    if upstream_interval == "1m" and range_ not in {"1d", "5d"}:
        range_ = "1d"
    elif upstream_interval in {"5m", "15m", "30m"} and range_ not in {"1d", "5d", "1mo"}:
        range_ = "5d"
    params = urlencode({"range": range_, "interval": upstream_interval, "includePrePost": "false", "events": "div,splits"})
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{symbol}?{params}"
        try:
            root = http_json(url)
            chart = root.get("chart") or {}
            result = (chart.get("result") or [None])[0]
            if not result:
                err = chart.get("error") or {}
                raise RuntimeError(err.get("description") or "Yahoo chart returned no data")
            meta = result.get("meta") or {}
            indicators = result.get("indicators") or {}
            quotes = indicators.get("quote") or [{}]
            q = quotes[0] or {}
            ts = result.get("timestamp") or []
            opens, highs, lows, closes = q.get("open") or [], q.get("high") or [], q.get("low") or [], q.get("close") or []
            rows = []
            for i, stamp in enumerate(ts):
                try:
                    o, h, l, c = float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i])
                    t = int(stamp)
                    if t > 0 and all(v > 0 for v in (o, h, l, c)):
                        rows.append({"t": t, "o": o, "h": h, "l": l, "c": c, "date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")})
                except (ValueError, TypeError, IndexError):
                    continue
            if len(rows) < 2:
                raise RuntimeError("Yahoo returned fewer than two valid OHLC candles")
            if requested_interval == "10m":
                # Yahoo has no native 10-minute interval; combine consecutive 5-minute candles.
                grouped = []
                for i in range(0, len(rows) - 1, 2):
                    a, b = rows[i], rows[i + 1]
                    grouped.append({"t": a["t"], "o": a["o"], "h": max(a["h"], b["h"]), "l": min(a["l"], b["l"]), "c": b["c"], "date": a.get("date")})
                rows = grouped
                if len(rows) < 2:
                    raise RuntimeError("Yahoo returned fewer than two valid aggregated 10-minute candles")
            return {"meta": meta, "candles": rows, "symbol": symbol, "range": range_, "interval": requested_interval}
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Price/history source unavailable: {last or 'unknown source error'}")


def completed_daily_rows(rows, now=None):
    """Treat a daily candle as completed only after its full 24-hour timestamp window."""
    now = time.time() if now is None else now
    return [r for r in rows if int(r.get("t", 0)) + 86400 < now]


def history_payload(symbol, range_="3mo", interval="1d", chart=None, now=None):
    chart = chart or fetch_yahoo_chart(symbol, range_, interval)
    rows = chart.get("candles") or []
    if len(rows) < 2:
        raise RuntimeError("Not enough valid candles for chart/history")
    meta = chart.get("meta") or {}
    result = {
        "ok": True,
        "symbol": symbol,
        "price": float(meta.get("regularMarketPrice") or rows[-1]["c"]),
        "source": "Yahoo Finance chart API via backend (best-effort; not a guaranteed real-time exchange feed)",
        "interval": chart.get("interval", interval),
        "candles": rows[-240:],
        "marketTimeEpoch": int(meta.get("regularMarketTime") or time.time()),
        "marketTimeKind": "source" if meta.get("regularMarketTime") else "retrieved",
        "marketTime": datetime.fromtimestamp(int(meta.get("regularMarketTime") or time.time()), timezone.utc).isoformat(),
    }
    if interval == "1d":
        complete = completed_daily_rows(rows, now=now)
        if len(complete) < 2:
            complete = rows[:-1] if len(rows) > 2 else rows
        if len(complete) >= 2:
            previous, latest = complete[-2], complete[-1]
            prev_close, close = float(previous["c"]), float(latest["c"])
            diff = close - prev_close
            result.update({
                "date": latest.get("date") or datetime.fromtimestamp(latest["t"], timezone.utc).strftime("%Y-%m-%d"),
                "open": latest["o"], "high": latest["h"], "low": latest["l"], "close": close,
                "difference": diff, "differencePct": (diff / prev_close * 100) if prev_close else None,
                "change": diff, "changePct": (diff / prev_close * 100) if prev_close else None,
                "previousCandle": previous, "latestCandle": latest,
                "dailyCandles": complete[-120:],
                "closes": [float(r["c"]) for r in complete[-120:]],
            })
    return result


def quote_payload(symbol):
    chart = fetch_yahoo_chart(symbol, "1y", "1d")
    rows = chart["candles"]
    meta = chart.get("meta") or {}
    price = float(meta.get("regularMarketPrice") or rows[-1]["c"])
    previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
    if previous_close:
        change = price - float(previous_close)
        change_pct = change / float(previous_close) * 100 if float(previous_close) else None
    else:
        hist = history_payload(symbol, "1y", "1d", chart=chart)
        change = hist.get("difference")
        change_pct = hist.get("differencePct")
    market_time = int(meta.get("regularMarketTime") or time.time())
    closes = [float(r["c"]) for r in completed_daily_rows(rows)[-120:]]
    if len(closes) < 2:
        closes = [float(r["c"]) for r in rows[-120:]]
    return {
        "ok": True, "symbol": symbol, "price": price,
        "high": float(meta.get("regularMarketDayHigh") or rows[-1]["h"]),
        "low": float(meta.get("regularMarketDayLow") or rows[-1]["l"]),
        "changeAbs": change, "changePct": change_pct,
        "closes": closes,
        "source": "Yahoo Finance chart API via backend (best-effort; not a guaranteed real-time exchange feed)",
        "marketTimeEpoch": market_time,
        "marketTimeKind": "source" if meta.get("regularMarketTime") else "retrieved",
        "marketTime": datetime.fromtimestamp(market_time, timezone.utc).isoformat(),
    }


def _resolve_expiry(expiry, available):
    """Resolve a UI expiry selector to an actual provider expiry date."""
    dates = sorted({str(x) for x in available if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x))})
    today = datetime.now().astimezone().date().isoformat()
    dates = [x for x in dates if x >= today]
    if not dates:
        raise RuntimeError("Dhan returned no future option expiries")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry or ""):
        if expiry not in dates:
            raise RuntimeError(f"Requested expiry {expiry} is not in Dhan's current expiry list")
        return expiry
    selector = (expiry or "current_week").lower()
    if selector in {"current_week", "weekly", "nearest", ""}:
        return dates[0]
    if selector in {"current_month", "monthly"}:
        ym = dates[0][:7]
        same_month = [x for x in dates if x[:7] == ym]
        return same_month[-1] if same_month else dates[0]
    return dates[0]


def fetch_dhan_oi(index, expiry=""):
    """Fetch live option-chain OI from DhanHQ using server-side credentials."""
    client_id = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
    access_token = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()
    if not client_id or not access_token:
        raise RuntimeError("Dhan provider selected but DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN is missing in server environment")
    ids = {
        "NIFTY": (os.environ.get("DHAN_NIFTY_SECURITY_ID") or "13").strip(),
        "SENSEX": (os.environ.get("DHAN_SENSEX_SECURITY_ID") or "").strip(),
    }
    security_id = ids.get(index, "")
    if not security_id or not security_id.isdigit():
        raise RuntimeError(f"Set DHAN_{index}_SECURITY_ID to the verified Dhan index security ID")
    segment = (os.environ.get("DHAN_INDEX_SEGMENT") or "IDX_I").strip()
    headers = {"access-token": access_token, "client-id": client_id, "Content-Type": "application/json"}
    expiry_list_url = "https://api.dhan.co/v2/optionchain/expirylist"
    expiry_payload = http_json_post(expiry_list_url, {"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment}, headers=headers)
    if not isinstance(expiry_payload, dict) or str(expiry_payload.get("status", "")).lower() not in {"success", "ok"}:
        raise RuntimeError("Dhan expiry-list request failed; verify credentials and index security ID")
    available = expiry_payload.get("data") or []
    actual_expiry = _resolve_expiry(expiry, available)
    chain_payload = http_json_post("https://api.dhan.co/v2/optionchain", {
        "UnderlyingScrip": int(security_id), "UnderlyingSeg": segment, "Expiry": actual_expiry
    }, headers=headers)
    if not isinstance(chain_payload, dict) or str(chain_payload.get("status", "")).lower() not in {"success", "ok"}:
        raise RuntimeError("Dhan option-chain request failed; verify API access and expiry")
    data = chain_payload.get("data") or {}
    chain = data.get("oc") or {}
    if not isinstance(chain, dict) or not chain:
        raise RuntimeError("Dhan returned an empty option chain")
    call_oi = put_oi = call_change = put_change = 0.0
    for strike in chain.values():
        if not isinstance(strike, dict):
            continue
        ce, pe = strike.get("ce") or {}, strike.get("pe") or {}
        if ce:
            cur = float(ce.get("oi") or 0)
            prev = float(ce.get("previous_oi") or 0)
            call_oi += cur
            call_change += cur - prev
        if pe:
            cur = float(pe.get("oi") or 0)
            prev = float(pe.get("previous_oi") or 0)
            put_oi += cur
            put_change += cur - prev
    spot = float(data.get("last_price") or 0)
    if spot <= 0 or call_oi <= 0 or put_oi <= 0:
        raise RuntimeError("Dhan option-chain data incomplete: valid spot and both call/put OI totals are required")
    return {
        "ok": True, "index": index, "expiry": actual_expiry, "spot": spot,
        "call": call_oi, "put": put_oi, "callChange": call_change, "putChange": put_change,
        "source": "DhanHQ Option Chain API (authorized account; live data subject to provider permissions)",
        "fetchedAt": datetime.now().astimezone().isoformat(),
        "provider": "DhanHQ", "strikeCount": len(chain),
    }


def http_json_post(url, body, headers=None, timeout=12):
    req_headers = {"User-Agent": "TradingAssistantBackend/1.1", "Accept": "application/json", "Content-Type": "application/json", **(headers or {})}
    req = Request(url, data=json.dumps(body).encode("utf-8"), headers=req_headers, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"upstream HTTP {resp.status}")
        raw = resp.read(4_000_000)
    return json.loads(raw.decode("utf-8"))


def fetch_authorized_oi(index, expiry=""):
    mode = (os.environ.get("OI_PROVIDER_MODE") or "custom").strip().lower()
    if mode == "dhan":
        return fetch_dhan_oi(index, expiry)
    endpoint = (os.environ.get("OI_PROVIDER_URL") or "").strip()
    api_key = (os.environ.get("OI_PROVIDER_API_KEY") or "").strip()
    if not endpoint:
        raise RuntimeError("OI provider is not configured; set OI_PROVIDER_MODE=dhan with Dhan credentials, or configure an authorized custom provider")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https":
        raise RuntimeError("OI provider URL must use HTTPS")
    query = parse_qs(parsed.query)
    query["index"] = [index]
    if expiry:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry):
            query["expiry"] = [expiry]
        else:
            query["expiryType"] = [expiry]
    url = urlunparse(parsed._replace(query=urlencode({k: v[-1] for k, v in query.items()})))
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = http_json(url, headers=headers, timeout=12)
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError((payload or {}).get("error") if isinstance(payload, dict) else "OI provider returned an invalid response")
    required = ("call", "put", "callChange", "putChange", "spot")
    if any(k not in payload or payload[k] is None for k in required):
        raise RuntimeError("OI provider response is missing call/put OI, OI changes, or spot")
    try:
        if float(payload["call"]) <= 0 or float(payload["put"]) <= 0 or float(payload["spot"]) <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError("OI provider returned invalid numeric values")
    payload.setdefault("index", index)
    payload.setdefault("expiry", expiry)
    payload.setdefault("source", "Configured authorized OI provider (server-side)")
    payload.setdefault("fetchedAt", datetime.now().astimezone().isoformat())
    return payload


def market_bundle(index, expiry=""):
    """Combine an authorized OI response with a separate best-effort price quote.

    Never fabricates OI. Uses the OI provider's spot as the aligned underlying price;
    separately reports quote-source price/change and flags material source disagreement.
    """
    index = (index or "").upper()
    if index not in {"NIFTY", "SENSEX"}:
        raise ValueError("index must be NIFTY or SENSEX")
    symbol = "^NSEI" if index == "NIFTY" else "^BSESN"
    oi = fetch_authorized_oi(index, expiry)
    quote = None
    quote_error = None
    try:
        quote = quote_payload(symbol)
    except Exception as exc:
        quote_error = str(exc)
    out = dict(oi)
    oi_spot = float(oi["spot"])
    out["index"] = index
    out["spot"] = oi_spot
    out["price"] = oi_spot
    out["priceSource"] = oi.get("source", "authorized OI provider underlying spot")
    if quote and float(quote.get("price") or 0) > 0:
        qp = float(quote["price"])
        out["quotePrice"] = qp
        out["quoteChangePct"] = quote.get("changePct")
        out["quoteSource"] = quote.get("source")
        out["quoteTimestamp"] = quote.get("marketTime")
        # Do not silently mix a materially different price quote with the OI snapshot.
        disagreement = abs(qp - oi_spot) / oi_spot * 100 if oi_spot else 100.0
        provider_has_spot_change = any(oi.get(k) is not None for k in ("spotChangePct", "changePct", "priceChangePct"))
        if disagreement > 0.5:
            out["priceMismatch"] = True
            out["dataQualityNote"] = f"Price quote and OI spot differ by {disagreement:.2f}%; source timestamps need review."
        else:
            out["priceMismatch"] = False
            out["dataQualityNote"] = "Price quote and OI spot are within 0.5%; source delays are still possible."
        if not provider_has_spot_change and disagreement <= 0.5:
            out["spotChangePct"] = quote.get("changePct")
            out["previousClose"] = quote.get("previousClose")
        out["priceSource"] = oi.get("source", "authorized OI provider") + " (underlying spot)"
    else:
        out["dataQualityNote"] = "Separate price quote unavailable; using underlying spot from authorized OI provider only."
        out["priceQuoteError"] = quote_error or "price quote unavailable"
    out["source"] = (oi.get("source") or "authorized OI provider") + (" + " + quote.get("source", "price source") if quote else "")
    out["bundle"] = True
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAssistantBackend/1.0"
    def _send(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)
    def do_OPTIONS(self):
        self._send(204, {})
    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                return self._send(200, {"ok": True, "service": SERVICE, "oiConfigured": ((os.environ.get("OI_PROVIDER_MODE") or "custom").lower() == "dhan" and bool(os.environ.get("DHAN_CLIENT_ID") and os.environ.get("DHAN_ACCESS_TOKEN"))) or bool(os.environ.get("OI_PROVIDER_URL")), "oiProviderMode": os.environ.get("OI_PROVIDER_MODE", "custom"), "routes": ["/api/quote", "/api/history", "/api/oi", "/api/marketbundle"]})
            if parsed.path == "/api/marketbundle":
                index = (q.get("index") or [""])[0].upper()
                expiry = (q.get("expiry") or [""])[0].strip()
                try:
                    return self._send(200, market_bundle(index, expiry))
                except Exception as exc:
                    return self._send(503, {"ok": False, "error": str(exc) or "Price + OI bundle unavailable", "oiConfigured": ((os.environ.get("OI_PROVIDER_MODE") or "custom").lower() == "dhan" and bool(os.environ.get("DHAN_CLIENT_ID") and os.environ.get("DHAN_ACCESS_TOKEN"))) or bool(os.environ.get("OI_PROVIDER_URL"))})
            if parsed.path in ("/api/quote", "/api/history"):
                symbol = (q.get("symbol") or [""])[0].strip()
                if not SYMBOL_RE.match(symbol):
                    return self._send(400, {"ok": False, "error": "Missing/invalid symbol"})
                range_ = (q.get("range") or ["3mo"])[0]
                interval = (q.get("interval") or ["1d"])[0]
                if parsed.path == "/api/quote":
                    return self._send(200, quote_payload(symbol))
                chart = fetch_yahoo_chart(symbol, range_, interval)
                return self._send(200, history_payload(symbol, range_, interval, chart=chart))
            if parsed.path == "/api/oi":
                index = (q.get("index") or [""])[0].upper()
                expiry = (q.get("expiry") or [""])[0].strip()
                if index not in {"NIFTY", "SENSEX"}:
                    return self._send(400, {"ok": False, "error": "index must be NIFTY or SENSEX"})
                try:
                    return self._send(200, fetch_authorized_oi(index, expiry))
                except Exception as exc:
                    return self._send(503, {"ok": False, "error": str(exc)})
            return self._send(404, {"ok": False, "error": "Route not found"})
        except (ValueError, RuntimeError, URLError, HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            return self._send(502, {"ok": False, "error": str(exc) or "Upstream data source failed"})
        except Exception as exc:
            return self._send(500, {"ok": False, "error": "Internal backend error: " + (str(exc)[:180])})
    def log_message(self, fmt, *args):
        # Keep logs compact; do not log headers/secrets.
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    print(f"{SERVICE} listening on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()

if __name__ == "__main__":
    main()
