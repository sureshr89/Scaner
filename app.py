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
MAX_QUOTE_AGE_MINUTES = 5


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


def retry_request(method, url, session=None, **kwargs):
    last = None
    client = session or requests
    for attempt in range(3):
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code not in (429, 500, 502, 503, 504):
                return response
            last = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last = str(exc)
        if attempt < 2:
            time.sleep(1.5 * 2**attempt)
    raise RuntimeError(f"Request failed after retries: {last}")


def pick(frame, names):
    lookup = {str(c).lower().replace(" ", "_"): c for c in frame.columns}
    return next((lookup[n.lower()] for n in names if n.lower() in lookup), None)


def clean_key(value):
    return "".join(ch for ch in str(value).upper().strip() if ch.isalnum())


@st.cache_data(ttl=86400, show_spinner=False)
def load_universe():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/csv,*/*", "Referer": "https://www.nseindia.com/"})
    frame, errors = None, []
    try:
        retry_request("GET", "https://www.nseindia.com/", session=session, timeout=20)
        response = retry_request("GET", NSE_URL, session=session, timeout=30)
        response.raise_for_status()
        frame = pd.DataFrame(response.json().get("data", []))
    except Exception as exc:
        errors.append(f"NSE: {exc}")
    if frame is None or frame.empty:
        try:
            response = retry_request("GET", CSV_URL, session=session, timeout=30)
            response.raise_for_status()
            if "<html" in response.text[:500].lower():
                raise RuntimeError("CSV endpoint returned HTML")
            frame = pd.read_csv(StringIO(response.text))
        except Exception as exc:
            errors.append(f"CSV: {exc}")
    if frame is None or frame.empty:
        raise RuntimeError("Unable to load universe: " + "; ".join(errors))

    symbol = pick(frame, ["symbol", "company_symbol"])
    sector = pick(frame, ["industry", "sector", "industry_info"])
    if not symbol:
        raise RuntimeError(f"Universe has no symbol column: {list(frame.columns)}")
    frame["stock"] = frame[symbol].astype(str).str.upper().str.strip()
    frame = frame[~frame.stock.str.contains("NIFTY", na=False)].copy()
    frame["sector"] = frame[sector].astype(str).str.strip() if sector else "UNKNOWN"

    response = retry_request("GET", MASTER_URL, session=session, timeout=90)
    response.raise_for_status()
    master = pd.read_csv(StringIO(response.text), low_memory=False)
    segment = pick(master, ["exchange_segment", "exchange_segment_name", "exchange_segment_code", "sem_exm_exch_id"])
    msymbol = pick(master, ["symbol", "trading_symbol", "sem_trading_symbol"])
    sid = pick(master, ["security_id", "sem_security_id", "sem_smst_security_id"])
    market = pick(master, ["sem_segment", "segment"])
    if not all([segment, msymbol, sid]):
        raise RuntimeError(f"Dhan master columns not recognised: {list(master.columns)[:30]}")

    dm = master.copy()
    ex = dm[segment].astype(str).str.upper().str.strip()
    allowed_exchange = {"NSE", "NSE_EQ", "NSE EQUITY"}
    dm = dm[ex.isin(allowed_exchange)]
    if market:
        market_values = dm[market].astype(str).str.upper().str.strip()
        dm = dm[market_values.isin({"E", "EQUITY", "NSE_EQ", "NSE EQUITY", "NA", "NAN"})]
    dm["security_id"] = pd.to_numeric(dm[sid], errors="coerce")
    dm["exact_key"] = dm[msymbol].astype(str).str.upper().str.strip()
    dm["key"] = dm[msymbol].map(clean_key)
    frame["exact_key"] = frame.stock.astype(str).str.upper().str.strip()
    frame["key"] = frame.stock.map(clean_key)
    dm = dm.dropna(subset=["security_id"])
    exact = dm.drop_duplicates("exact_key")[["exact_key", "security_id"]]
    result = frame[["stock", "sector", "exact_key", "key"]].merge(exact, on="exact_key", how="left")
    missing = result["security_id"].isna()
    fallback = dm.drop_duplicates("key")[["key", "security_id"]]
    result.loc[missing, "security_id"] = result.loc[missing, ["key"]].merge(fallback, on="key", how="left")["security_id_y"].to_numpy()
    result = result.dropna(subset=["security_id"])
    result["security_id"] = result["security_id"].astype(int)
    result = result[["stock", "sector", "security_id"]].drop_duplicates("stock").sort_values("stock").reset_index(drop=True)
    if len(result) < 120:
        raise RuntimeError(f"Only {len(result)} stocks mapped; too many missing")
    return result


@st.cache_data(ttl=30, show_spinner=False)
def live_quotes(ids):
    response = retry_request("POST", QUOTE_URL, headers=headers(), json={"NSE_EQ": sorted({int(x) for x in ids})}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    out = {}
    for segment, items in (payload.get("data") or {}).items():
        for sid, quote in (items or {}).items():
            quote = quote or {}
            ohlc = quote.get("ohlc") or {}
            out[(segment, str(sid))] = {"LTP": quote.get("last_price"), "Today's Open": ohlc.get("open"), "Today's Low": ohlc.get("low"), "Today's High": ohlc.get("high")}
    if not out:
        raise RuntimeError(f"Dhan returned no quote data: {payload}")
    return out


def previous_ohlc(sid, start, end):
    payload = {"securityId": str(int(sid)), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "expiryCode": 0, "oi": False, "fromDate": start, "toDate": end}
    response = retry_request("POST", HISTORY_URL, headers=headers(), json=payload, timeout=30)
    response.raise_for_status()
    raw = response.json().get("data") or {}
    fields = ["timestamp", "close", "high", "low"]
    if not isinstance(raw, dict) or not all(field in raw for field in fields):
        return {"PDC": None, "PDH": None, "PDL": None}
    candles = pd.DataFrame({field: pd.to_numeric(raw[field], errors="coerce") for field in fields}).dropna().sort_values("timestamp")
    if candles.empty:
        return {"PDC": None, "PDH": None, "PDL": None}
    unit = "ms" if candles["timestamp"].abs().max() > 10_000_000_000 else "s"
    candles["trade_date"] = pd.to_datetime(candles["timestamp"], unit=unit, utc=True, errors="coerce").dt.tz_convert(IST).dt.date
    previous_day = datetime.now(IST).date() - timedelta(days=1)
    completed = candles[candles["trade_date"] < datetime.now(IST).date()]
    if completed.empty:
        return {"PDC": None, "PDH": None, "PDL": None}
    if previous_day in set(completed["trade_date"]):
        candle = completed[completed["trade_date"] == previous_day].iloc[-1]
    else:
        candle = completed.iloc[-1]
    return {"PDC": float(candle["close"]), "PDH": float(candle["high"]), "PDL": float(candle["low"])}


@st.cache_data(ttl=86400, show_spinner=False)
def load_history(ids, start, end):
    result, failures = {}, []
    progress = st.progress(0, text="Loading previous-day OHLC once for today...")
    for index, sid in enumerate(ids, 1):
        try:
            result[int(sid)] = previous_ohlc(sid, start, end)
        except Exception as exc:
            result[int(sid)] = {"PDC": None, "PDH": None, "PDL": None}
            failures.append(f"{sid}: {exc}")
        progress.progress(index / len(ids), text=f"Historical data: {index}/{len(ids)}")
    progress.empty()
    return result, failures


def calculate(data, live_is_fresh=True):
    numeric_fields = ["LTP", "PDC", "PDH", "PDL", "Today's Low", "Today's High"]
    for field in numeric_fields:
        data[field] = pd.to_numeric(data.get(field, pd.Series(index=data.index, dtype=float)), errors="coerce")
    valid = data["LTP"].notna() & data["PDC"].notna() & data["PDH"].notna() & data["PDL"].notna()
    up = (valid & data["LTP"].gt(data["PDC"])).groupby(data["Sector"]).transform("sum")
    down = (valid & data["LTP"].lt(data["PDC"])).groupby(data["Sector"]).transform("sum")
    ratio = up.div(down.replace(0, pd.NA))
    ratio = ratio.mask((up > 0) & (down == 0), float("inf"))
    ratio = ratio.mask((up == 0) & (down == 0), pd.NA)
    data["Sector A/D Ratio"] = ratio
    data["Sector Bias"] = ratio.map(lambda x: "🟢 Bullish" if pd.notna(x) and x > 1 else "🔴 Bearish" if pd.notna(x) and x < 1 else "⚪ Neutral" if pd.notna(x) else "N/A")
    good = data["PDC"].gt(0)
    data["PDC to PDH %"] = ((data["PDH"] - data["PDC"]) / data["PDC"] * 100).where(good)
    data["PDC to PDL %"] = ((data["PDC"] - data["PDL"]) / data["PDC"] * 100).where(good)
    tight_up = data["PDC to PDH %"].between(0, 0.15, inclusive="both")
    tight_down = data["PDC to PDL %"].between(0, 0.15, inclusive="both")
    data["Green Signal"] = valid & live_is_fresh & data["Today's High"].gt(data["PDH"]) & data["Today's Low"].lt(data["PDH"]) & data["LTP"].gt(data["PDH"]) & data["Sector A/D Ratio"].gt(1) & tight_up
    data["Red Signal"] = valid & live_is_fresh & data["Today's Low"].lt(data["PDL"]) & data["Today's High"].gt(data["PDL"]) & data["LTP"].lt(data["PDL"]) & data["Sector A/D Ratio"].lt(1) & tight_down
    data["Signal"] = ""
    data.loc[data["Green Signal"], "Signal"] = "🟢 BUY"
    data.loc[data["Red Signal"], "Signal"] = "🔴 SELL"
    data["Signal Priority"] = data["Signal"].map({"🟢 BUY": 0, "🔴 SELL": 1, "": 2}).fillna(3)
    return data


def market_open(now):
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def render():
    now = datetime.now(IST)
    stocks = load_universe()
    ids = tuple(stocks["security_id"].astype(int))
    history, failures = load_history(ids, (now.date() - timedelta(days=30)).isoformat(), now.date().isoformat())
    if failures:
        with st.expander(f"Historical data warnings ({len(failures)})"):
            st.code("\n".join(failures[:50]))
    quote_error, live_is_fresh = None, False
    if market_open(now):
        try:
            quotes = live_quotes(ids)
            st.session_state.last_live_quotes = quotes
            st.session_state.quote_time = now
        except Exception as exc:
            quote_error = f"Live quote error: {exc}"
    else:
        quote_error = "Market is closed; live signals are disabled."
    live = st.session_state.get("last_live_quotes", {})
    quote_time = st.session_state.get("quote_time")
    if quote_time:
        age = max(0, int((now - quote_time).total_seconds() // 60))
        live_is_fresh = age <= MAX_QUOTE_AGE_MINUTES and market_open(now)
        st.caption(f"Quote snapshot: {quote_time.strftime('%Y-%m-%d %H:%M:%S %Z')} ({age} min old)")
    if quote_error:
        st.warning(quote_error)
    rows = []
    for record in stocks.to_dict("records"):
        sid = int(record["security_id"])
        quote = live.get(("NSE_EQ", str(sid)), {})
        old = history.get(sid, {})
        rows.append({"Stock": record["stock"], "Sector": record["sector"], **{k: quote.get(k) for k in ["LTP", "Today's Open", "Today's Low", "Today's High"]}, **{k: old.get(k) for k in ["PDC", "PDH", "PDL"]}})
    data = calculate(pd.DataFrame(rows), live_is_fresh=live_is_fresh)
    columns = ["Signal", "Stock", "Sector", "Sector A/D Ratio", "Sector Bias", "LTP", "Today's Open", "Today's Low", "Today's High", "PDC", "PDH", "PDL", "PDC to PDH %", "PDC to PDL %"]
    st.metric("Stocks scanned", len(data))
    st.dataframe(data.reindex(columns=columns).sort_values(["Signal Priority", "Sector", "Stock"]), use_container_width=True, hide_index=True)
    st.caption("Signals are enabled only during market hours with a quote snapshot no older than 5 minutes.")


st.title("📈 Nifty Midcap 150 Stock Scanner")
st.caption("Nifty Midcap 150 • Dhan live quotes • previous-day OHLC • sector breadth")
try:
    if hasattr(st, "fragment"):
        st.fragment(run_every="30s")(render)()
    else:
        render()
except Exception as exc:
    st.error(f"Scanner error: {exc}")
