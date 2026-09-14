import os
from datetime import datetime, timedelta
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


@st.cache_data(ttl=86400, show_spinner=False)
def load_universe():
    s, frame, errors = http_session(), None, []
    try:
        s.get("https://www.nseindia.com/", timeout=20)
        r = s.get(NSE_URL, timeout=30)
        r.raise_for_status()
        frame = pd.DataFrame(r.json().get("data", []))
    except Exception as e:
        errors.append(f"NSE: {e}")
    if frame is None or frame.empty:
        try:
            r = s.get(CSV_URL, timeout=30)
            r.raise_for_status()
            frame = pd.read_csv(StringIO(r.text))
        except Exception as e:
            errors.append(f"CSV: {e}")
    if frame is None or frame.empty:
        raise RuntimeError("Unable to load Nifty Midcap 150: " + "; ".join(errors))
    symbol, sector = pick(frame, ["symbol", "company_symbol"]), pick(frame, ["industry", "sector", "industry_info"])
    if not symbol:
        raise RuntimeError(f"Universe has no symbol column: {list(frame.columns)}")
    frame["stock"] = frame[symbol].astype(str).str.upper().str.strip()
    frame = frame[~frame.stock.str.contains("NIFTY", na=False)].copy()
    frame["sector"] = frame[sector].astype(str).str.strip() if sector else "UNKNOWN"
    master = pd.read_csv(StringIO(s.get(MASTER_URL, timeout=90).text), low_memory=False)
    segment, msymbol, sid = pick(master, ["exchange_segment", "exchange_segment_name", "exchange_segment_code", "sem_exm_exch_id"]), pick(master, ["symbol", "trading_symbol", "sem_trading_symbol"]), pick(master, ["security_id", "sem_security_id", "sem_smst_security_id"])
    market = pick(master, ["sem_segment", "segment"])
    if not all([segment, msymbol, sid]):
        raise RuntimeError(f"Dhan master columns not recognised: {list(master.columns)[:30]}")
    dm = master.copy(); ex = dm[segment].astype(str).str.upper()
    if segment.lower() == "sem_exm_exch_id":
        dm = dm[ex.eq("NSE")]
        if market: dm = dm[dm[market].astype(str).str.upper().isin(["E", "EQUITY", "NSE_EQ"])]
    else: dm = dm[ex.eq("NSE_EQ")]
    dm["key"] = dm[msymbol].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    dm["security_id"] = pd.to_numeric(dm[sid], errors="coerce")
    frame["key"] = frame.stock.str.replace(r"[^A-Z0-9]", "", regex=True)
    dm = dm.dropna(subset=["security_id"]).drop_duplicates("key")[["key", "security_id"]]
    result = frame[["stock", "sector", "key"]].merge(dm, on="key", how="inner")
    result["security_id"] = result.security_id.astype(int)
    result = result[["stock", "sector", "security_id"]].drop_duplicates("stock").sort_values("stock").reset_index(drop=True)
    if len(result) != 150:
        missing = sorted(set(frame.stock) - set(result.stock))
        raise RuntimeError(f"Expected 150 mapped stocks; got {len(result)}. Unmapped: {missing[:20]}")
    return result


@st.cache_data(ttl=30, show_spinner=False)
def live_quotes(ids):
    try:
        r = requests.post(QUOTE_URL, headers=headers(), json={"NSE_EQ": sorted({int(x) for x in ids})}, timeout=60)
        if r.status_code == 429: return {}, "Dhan rate limit reached; showing last successful quotes."
        r.raise_for_status(); payload = r.json(); out = {}
        for segment, items in (payload.get("data") or {}).items():
            for sid, quote in (items or {}).items():
                ohlc = quote.get("ohlc") or {}
                out[(segment, str(sid))] = {"LTP": quote.get("last_price"), "Today's Open": ohlc.get("open"), "Today's Low": ohlc.get("low"), "Today's High": ohlc.get("high")}
        return out, None
    except Exception as e:
        return {}, f"Live quote request failed: {e}"


def previous_ohlc(sid, start, end):
    payload = {"securityId": str(int(sid)), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "expiryCode": 0, "oi": False, "fromDate": start, "toDate": end}
    r = requests.post(HISTORY_URL, headers=headers(), json=payload, timeout=30); r.raise_for_status()
    data = r.json().get("data") or {}; fields = ["timestamp", "close", "high", "low"]
    if not isinstance(data, dict) or not all(f in data for f in fields): return {"PDC": None, "PDH": None, "PDL": None}
    candles = pd.DataFrame({f: data[f] for f in fields})
    for f in fields: candles[f] = pd.to_numeric(candles[f], errors="coerce")
    candles = candles.dropna(subset=fields).sort_values("timestamp")
    if candles.empty: return {"PDC": None, "PDH": None, "PDL": None}
    return {"PDC": float(candles.iloc[-1]["close"]), "PDH": float(candles.iloc[-1]["high"]), "PDL": float(candles.iloc[-1]["low"])}


@st.cache_data(ttl=86400, show_spinner=False)
def load_history(ids, start, end):
    result, failures = {}, []
    progress = st.progress(0, text="Loading historical data once for today...")
    for i, sid in enumerate(ids, 1):
        try: result[int(sid)] = previous_ohlc(sid, start, end)
        except Exception as e: result[int(sid)] = {"PDC": None, "PDH": None, "PDL": None}; failures.append(str(sid))
        progress.progress(i / len(ids), text=f"Historical data: {i}/{len(ids)}")
    progress.empty()
    if failures: st.warning(f"Historical data unavailable for {len(failures)} stocks.")
    return result


def bias(x):
    if pd.isna(x): return "N/A"
    return "🟢 Bullish" if x > 1 else "🔴 Bearish" if x < 1 else "⚪ Neutral"


def calculate(df):
    for f in ["LTP", "PDC", "PDH", "PDL"]: df[f] = pd.to_numeric(df.get(f, pd.Series(index=df.index, dtype="float64")), errors="coerce")
    valid = df["LTP"].notna() & df["PDC"].notna()
    up = df["LTP"].gt(df["PDC"]).where(valid, False).groupby(df["Sector"]).transform("sum")
    down = df["LTP"].lt(df["PDC"]).where(valid, False).groupby(df["Sector"]).transform("sum")
    ratio = (up / down.replace(0, float("nan"))).mask((down == 0) & (up > 0), float("inf"))
    df["Sector A/D Ratio"], df["Sector Bias"] = ratio, ratio.apply(bias)
    good = df["PDC"].gt(0)
    df["PDC to PDH %"] = ((df["PDH"] - df["PDC"]) / df["PDC"] * 100).where(good)
    df["PDC to PDL %"] = ((df["PDC"] - df["PDL"]) / df["PDC"] * 100).where(good)
    return df


def render():
    now = datetime.now(IST); stocks = load_universe(); ids = tuple(stocks.security_id.astype(int))
    end, start = now.date(), now.date() - timedelta(days=30)
    history = load_history(ids, start.isoformat(), end.isoformat()); quotes, error = live_quotes(ids)
    if quotes: st.session_state["last_live_quotes"] = quotes
    if error: st.warning(error)
    live = st.session_state.get("last_live_quotes", {}); rows = []
    for rec in stocks.to_dict("records"):
        sid = int(rec["security_id"]); q = live.get(("NSE_EQ", str(sid)), {}); h = history.get(sid, {})
        rows.append({"Stock": rec["stock"], "Sector": rec["sector"], **{k: q.get(k) for k in ["LTP", "Today's Open", "Today's Low", "Today's High"]}, **{k: h.get(k) for k in ["PDC", "PDH", "PDL"]}})
    data = calculate(pd.DataFrame(rows)); cols = ["Stock", "Sector", "Sector A/D Ratio", "Sector Bias", "LTP", "Today's Open", "Today's Low", "Today's High", "PDC", "PDH", "PDL", "PDC to PDH %", "PDC to PDL %"]
    for c in cols:
        if c not in data: data[c] = pd.NA
    st.metric("Stocks scanned", len(data)); st.dataframe(data[cols].sort_values(["Sector", "Stock"]), use_container_width=True, hide_index=True)
    st.caption(f"Historical data cached for {end.isoformat()}; live quotes cached for 30 seconds.")


st.title("📈 Nifty Midcap 150 Stock Scanner")
st.caption("One batch live quote request; historical data cached for 24 hours.")
try:
    if hasattr(st, "fragment"): st.fragment(run_every="30s")(render)()
    else: render()
except Exception as e: st.error(f"Scanner error: {e}")
