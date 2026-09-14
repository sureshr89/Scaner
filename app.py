import os
import time
from datetime import datetime, timedelta, time as dtime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Nifty Midcap 150 Scanner", page_icon="📈", layout="wide")
IST = ZoneInfo("Asia/Kolkata")
NSE_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20MIDCAP%20150"
CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"
HISTORY_URL = "https://api.dhan.co/v2/charts/historical"


def secret(name):
    try:
        return os.getenv(name) or st.secrets.get(name, "")
    except Exception:
        return os.getenv(name, "")


def headers():
    client, token = secret("DHAN_CLIENT_ID"), secret("DHAN_ACCESS_TOKEN")
    if not client or not token:
        raise RuntimeError("Configure DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets.")
    return {"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client}


def pick(frame, names):
    lookup = {str(c).lower().replace(" ", "_"): c for c in frame.columns}
    return next((lookup[n.lower()] for n in names if n.lower() in lookup), None)


def http_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/csv,*/*", "Referer": "https://www.nseindia.com/"})
    return s


def request_with_retry(method, url, **kwargs):
    last = None
    for attempt in range(3):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                return response
            last = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last = str(exc)
        if attempt < 2:
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"Request failed after retries: {last}")


@st.cache_data(ttl=86400, show_spinner=False)
def load_universe():
    s, frame, errors = http_session(), None, []
    try:
        s.get("https://www.nseindia.com/", timeout=20)
        r = s.get(NSE_URL, timeout=30)
        r.raise_for_status()
        frame = pd.DataFrame(r.json().get("data", []))
    except Exception as exc:
        errors.append(f"NSE: {exc}")
    if frame is None or frame.empty:
        try:
            r = s.get(CSV_URL, timeout=30)
            r.raise_for_status()
            if "<html" in r.text[:500].lower():
                raise RuntimeError("CSV endpoint returned HTML instead of CSV")
            frame = pd.read_csv(StringIO(r.text))
        except Exception as exc:
            errors.append(f"CSV: {exc}")
    if frame is None or frame.empty:
        raise RuntimeError("Unable to load Nifty Midcap 150: " + "; ".join(errors))

    symbol = pick(frame, ["symbol", "company_symbol"])
    sector = pick(frame, ["industry", "sector", "industry_info"])
    if not symbol:
        raise RuntimeError(f"Universe has no symbol column: {list(frame.columns)}")
    frame["stock"] = frame[symbol].astype(str).str.upper().str.strip()
    frame = frame[~frame.stock.str.contains("NIFTY", na=False)].copy()
    frame["sector"] = frame[sector].astype(str).str.strip() if sector else "UNKNOWN"

    master_response = s.get(MASTER_URL, timeout=90)
    master_response.raise_for_status()
    master = pd.read_csv(StringIO(master_response.text), low_memory=False)
    segment = pick(master, ["exchange_segment", "exchange_segment_name", "exchange_segment_code", "sem_exm_exch_id"])
    msymbol = pick(master, ["symbol", "trading_symbol", "sem_trading_symbol"])
    sid = pick(master, ["security_id", "sem_security_id", "sem_smst_security_id"])
    market = pick(master, ["sem_segment", "segment"])
    if not all([segment, msymbol, sid]):
        raise RuntimeError(f"Dhan master columns not recognised: {list(master.columns)[:30]}")

    dm = master.copy()
    ex = dm[segment].astype(str).str.upper()
    if segment.lower() == "sem_exm_exch_id":
        dm = dm[ex.eq("NSE")]
        if market:
            dm = dm[dm[market].astype(str).str.upper().isin(["E", "EQUITY", "NSE_EQ"])]
    else:
        dm = dm[ex.eq("NSE_EQ")]
    dm["key"] = dm[msymbol].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    dm["security_id"] = pd.to_numeric(dm[sid], errors="coerce")
    frame["key"] = frame.stock.str.replace(r"[^A-Z0-9]", "", regex=True)
    dm = dm.dropna(subset=["security_id"]).drop_duplicates("key")[["key", "security_id"]]
    result = frame[["stock", "sector", "key"]].merge(dm, on="key", how="inner")
    result["security_id"] = result.security_id.astype(int)
    result = result[["stock", "sector", "security_id"]].drop_duplicates("stock").sort_values("stock").reset_index(drop=True)
    if len(result) < 120:
        missing = sorted(set(frame.stock) - set(result.stock))
        raise RuntimeError(f"Only {len(result)} stocks mapped; too many missing: {missing[:20]}")
    return result


@st.cache_data(ttl=30, show_spinner=False)
def live_quotes(ids):
    response = request_with_retry("POST", QUOTE_URL, headers=headers(), json={"NSE_EQ": sorted({int(x) for x in ids})}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    out = {}
    for segment, items in (payload.get("data") or {}).items():
        for sid, quote in (items or {}).items():
            quote = quote or {}
            ohlc = quote.get("ohlc") or {}
            out[(segment, str(sid))] = {"LTP": quote.get("last_price"), "Today's Open": ohlc.get("open"), "Today's Low": ohlc.get("low"), "Today's High": ohlc.get("high")}
    return out


def previous_ohlc(sid, start, end):
    payload = {"securityId": str(int(sid)), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "expiryCode": 0, "oi": False, "fromDate": start, "toDate": end}
    response = request_with_retry("POST", HISTORY_URL, headers=headers(), json=payload, timeout=30)
    response.raise_for_status()
    data = response.json().get("data") or {}
    fields = ["timestamp", "close", "high", "low"]
    if not isinstance(data, dict) or not all(field in data for field in fields):
        return {"PDC": None, "PDH": None, "PDL": None}
    candles = pd.DataFrame({field: data[field] for field in fields})
    for field in fields:
        candles[field] = pd.to_numeric(candles[field], errors="coerce")
    candles = candles.dropna(subset=fields).sort_values("timestamp")
    if candles.empty:
        return {"PDC": None, "PDH": None, "PDL": None}
    max_timestamp = candles["timestamp"].abs().max()
    unit = "ms" if max_timestamp > 10_000_000_000 else "s"
    candles["trade_date"] = pd.to_datetime(candles["timestamp"], unit=unit, errors="coerce", utc=True).dt.tz_convert(IST).dt.date
    completed = candles[candles["trade_date"] < datetime.now(IST).date()]
    candle = (completed if not completed.empty else candles).iloc[-1]
    return {"PDC": float(candle["close"]), "PDH": float(candle["high"]), "PDL": float(candle["low"])}


@st.cache_data(ttl=86400, show_spinner=False)
def load_history(ids, start, end):
    result, failures = {}, []
    progress = st.progress(0, text="Loading historical data once for today...")
    for index, sid in enumerate(ids, 1):
        try:
            result[int(sid)] = previous_ohlc(sid, start, end)
        except Exception as exc:
            result[int(sid)] = {"PDC": None, "PDH": None, "PDL": None}
            failures.append(f"{sid}: {exc}")
        progress.progress(index / len(ids), text=f"Historical data: {index}/{len(ids)}")
    progress.empty()
    return result, failures


def bias(value):
    if pd.isna(value):
        return "N/A"
    return "🟢 Bullish" if value > 1 else "🔴 Bearish" if value < 1 else "⚪ Neutral"


def calculate(data):
    for field in ["LTP", "PDC", "PDH", "PDL"]:
        data[field] = pd.to_numeric(data.get(field, pd.Series(index=data.index)), errors="coerce")
    valid = data["LTP"].notna() & data["PDC"].notna()
    up = (valid & data["LTP"].gt(data["PDC"])).groupby(data["Sector"]).transform("sum")
    down = (valid & data["LTP"].lt(data["PDC"])).groupby(data["Sector"]).transform("sum")
    ratio = up.div(down.replace(0, pd.NA))
    ratio = ratio.mask((up > 0) & (down == 0), float("inf"))
    ratio = ratio.mask((up == 0) & (down > 0), 0)
    ratio = ratio.mask((up == 0) & (down == 0), pd.NA)
    data["Sector A/D Ratio"] = ratio
    data["Sector Bias"] = ratio.apply(bias)
    good = data["PDC"].gt(0)
    data["PDC to PDH %"] = ((data["PDH"] - data["PDC"]) / data["PDC"] * 100).where(good)
    data["PDC to PDL %"] = ((data["PDC"] - data["PDL"]) / data["PDC"] * 100).where(good)
    return data


def market_open(now):
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def render():
    now = datetime.now(IST)
    stocks = load_universe()
    ids = tuple(stocks.security_id.astype(int))
    end, start = now.date(), now.date() - timedelta(days=30)
    history, failures = load_history(ids, start.isoformat(), end.isoformat())
    if failures:
        with st.expander(f"Historical data warnings ({len(failures)})"):
            st.code("\n".join(failures[:50]))

    quotes, quote_error = {}, None
    if market_open(now):
        try:
            quotes = live_quotes(ids)
            st.session_state["last_live_quotes"] = quotes
            st.session_state["quote_time"] = now
        except Exception as exc:
            quote_error = str(exc)
    else:
        quote_error = "Market is closed; showing the last successful quote snapshot."

    live = st.session_state.get("last_live_quotes", {})
    quote_time = st.session_state.get("quote_time")
    if quote_error:
        st.warning(quote_error)
    if quote_time:
        age = int((now - quote_time).total_seconds() // 60)
        st.caption(f"Quote snapshot: {quote_time.strftime('%Y-%m-%d %H:%M:%S %Z')} ({age} min old)")

    rows = []
    for record in stocks.to_dict("records"):
        sid = int(record["security_id"])
        quote = live.get(("NSE_EQ", str(sid)), {})
        old = history.get(sid, {})
        rows.append({"Stock": record["stock"], "Sector": record["sector"], **{key: quote.get(key) for key in ["LTP", "Today's Open", "Today's Low", "Today's High"]}, **{key: old.get(key) for key in ["PDC", "PDH", "PDL"]}})

    data = calculate(pd.DataFrame(rows))
    columns = ["Stock", "Sector", "Sector A/D Ratio", "Sector Bias", "LTP", "Today's Open", "Today's Low", "Today's High", "PDC", "PDH", "PDL", "PDC to PDH %", "PDC to PDL %"]
    for column in columns:
        if column not in data.columns:
            data[column] = pd.NA
    st.metric("Stocks scanned", len(data))
    st.dataframe(data[columns].sort_values(["Sector", "Stock"]), use_container_width=True, hide_index=True)
    st.caption(f"Previous completed trading-day OHLC; historical cache: 24 hours; live quote cache: 30 seconds.")


st.title("📈 Nifty Midcap 150 Stock Scanner")
st.caption("Batched live quotes, retry protection, completed-day OHLC, and visible data warnings.")
try:
    if hasattr(st, "fragment"):
        st.fragment(run_every="30s")(render)()
    else:
        render()
except Exception as exc:
    st.error(f"Scanner error: {exc}")
