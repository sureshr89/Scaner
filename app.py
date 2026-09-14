import os
import re
from datetime import datetime, time as dt_time, timedelta
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
    cid, token = secret("DHAN_CLIENT_ID"), secret("DHAN_ACCESS_TOKEN")
    if not cid or not token:
        raise RuntimeError("Configure DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets.")
    return {"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": cid}


def norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper().strip())


def col(frame, names):
    lookup = {str(x).lower().replace(" ", "_"): x for x in frame.columns}
    return next((lookup[x.lower()] for x in names if x.lower() in lookup), None)


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/csv,*/*", "Referer": "https://www.nseindia.com/"})
    return s


@st.cache_data(ttl=86400, show_spinner=False)
def load_universe():
    s, errors, frame = session(), [], None
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

    symbol = col(frame, ["symbol", "company_symbol"])
    sector = col(frame, ["industry", "sector", "industry_info"])
    if not symbol:
        raise RuntimeError(f"Universe has no symbol column: {list(frame.columns)}")
    frame["stock"] = frame[symbol].astype(str).str.upper().str.strip()
    frame = frame[~frame.stock.str.contains("NIFTY", na=False)].copy()
    frame["sector"] = frame[sector].astype(str).str.strip() if sector else "UNKNOWN"
    frame["key"] = frame.stock.map(norm)
    frame = frame.drop_duplicates("key")

    master_response = s.get(MASTER_URL, timeout=90)
    master_response.raise_for_status()
    master = pd.read_csv(StringIO(master_response.text), low_memory=False)
    segment = col(master, ["exchange_segment", "exchange_segment_name", "exchange_segment_code", "sem_exm_exch_id"])
    market_segment = col(master, ["sem_segment", "segment"])
    msymbol = col(master, ["symbol", "trading_symbol", "sem_trading_symbol"])
    sid = col(master, ["security_id", "sem_security_id", "sem_smst_security_id"])
    if not all([segment, msymbol, sid]):
        raise RuntimeError(f"Dhan master columns not recognised: {list(master.columns)[:30]}")

    dm = master.copy()
    ex = dm[segment].astype(str).str.upper()
    if segment.lower() == "sem_exm_exch_id":
        dm = dm[ex.eq("NSE")]
        if market_segment:
            dm = dm[dm[market_segment].astype(str).str.upper().isin(["E", "EQUITY", "NSE_EQ"])]
    else:
        dm = dm[ex.eq("NSE_EQ")]
    dm["key"] = dm[msymbol].map(norm)
    dm["security_id"] = pd.to_numeric(dm[sid], errors="coerce")
    dm = dm.dropna(subset=["security_id"]).drop_duplicates("key")[["key", "security_id"]]

    result = frame[["stock", "sector", "key"]].merge(dm, on="key", how="inner")
    result["security_id"] = result.security_id.astype(int)
    result = result[["stock", "sector", "security_id"]].drop_duplicates("stock").sort_values("stock").reset_index(drop=True)
    if len(result) != 150:
        missing = sorted(set(frame.stock) - set(result.stock))
        raise RuntimeError(f"Expected exactly 150 mapped stocks; got {len(result)}. Unmapped: {missing[:20]}")
    return result


def live_quotes(stocks):
    # Convert NumPy/Pandas integer scalars to native Python ints before JSON encoding.
    ids = sorted({int(value) for value in stocks["security_id"].dropna().tolist()})
    response = requests.post(QUOTE_URL, headers=headers(), json={"NSE_EQ": ids}, timeout=60)
    response.raise_for_status()
    out = {}
    for segment, items in (response.json().get("data") or {}).items():
        for security_id, quote in (items or {}).items():
            ohlc = quote.get("ohlc") or {}
            out[(segment, str(security_id))] = {"LTP": quote.get("last_price"), "Today's Open": ohlc.get("open"), "Today's Low": ohlc.get("low"), "Today's High": ohlc.get("high")}
    return out


def previous_ohlc(security_id, start, end):
    payload = {"securityId": str(int(security_id)), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "expiryCode": 0, "oi": False, "fromDate": start, "toDate": end}
    response = requests.post(HISTORY_URL, headers=headers(), json=payload, timeout=30)
    response.raise_for_status()
    data = response.json().get("data") or {}
    if not isinstance(data, dict):
        return {"PDC": None, "PDH": None, "PDL": None, "History Error": "Unexpected historical response"}
    fields = ["timestamp", "close", "high", "low"]
    if not all(field in data for field in fields):
        return {"PDC": None, "PDH": None, "PDL": None, "History Error": f"Missing fields: {', '.join(set(fields) - set(data))}"}
    candles = pd.DataFrame({field: data[field] for field in fields})
    for field in fields:
        candles[field] = pd.to_numeric(candles[field], errors="coerce")
    candles = candles.dropna(subset=fields).sort_values("timestamp")
    if candles.empty:
        return {"PDC": None, "PDH": None, "PDL": None, "History Error": "No historical candle"}
    candle = candles.iloc[-1]
    return {"PDC": float(candle["close"]), "PDH": float(candle["high"]), "PDL": float(candle["low"]), "History Error": None}


@st.cache_data(ttl=86400, show_spinner=False)
def load_history(security_ids, start, end):
    history = {}
    progress = st.progress(0, text="Loading historical data once for today...")
    total = len(security_ids)
    for index, security_id in enumerate(security_ids, 1):
        try:
            history[int(security_id)] = previous_ohlc(security_id, start, end)
        except Exception as error:
            history[int(security_id)] = {"PDC": None, "PDH": None, "PDL": None, "History Error": str(error)}
        progress.progress(index / total, text=f"Historical data: {index}/{total}")
    progress.empty()
    return history


def bias(value):
    if pd.isna(value): return "N/A"
    if value > 1: return "🟢 Bullish"
    if value < 1: return "🔴 Bearish"
    return "⚪ Neutral"


def calculate(df):
    for field in ["LTP", "PDC", "PDH", "PDL"]:
        if field not in df.columns: df[field] = pd.NA
        df[field] = pd.to_numeric(df[field], errors="coerce")
    if "Sector" not in df.columns: df["Sector"] = "UNKNOWN"
    up = df["LTP"].gt(df["PDC"]).groupby(df["Sector"]).transform("sum")
    down = df["LTP"].lt(df["PDC"]).groupby(df["Sector"]).transform("sum")
    ratio = up / down.replace(0, float("nan"))
    ratio = ratio.mask((down == 0) & (up > 0), float("inf"))
    df["Sector A/D Ratio"] = ratio
    df["Sector Bias"] = ratio.apply(bias)
    valid = df["PDC"].gt(0)
    df["PDC to PDH %"] = ((df["PDH"] - df["PDC"]) / df["PDC"] * 100).where(valid)
    df["PDC to PDL %"] = ((df["PDC"] - df["PDL"]) / df["PDC"] * 100).where(valid)
    return df


def render_scanner():
    now = datetime.now(IST)
    stocks = load_universe()
    end = now.date()
    start = end - timedelta(days=30)
    history = load_history(tuple(int(value) for value in stocks["security_id"].dropna().tolist()), start.isoformat(), end.isoformat())
    live = live_quotes(stocks)
    rows = []
    for record in stocks.to_dict("records"):
        security_id = int(record["security_id"])
        row = {"Stock": record["stock"], "Sector": record["sector"], "LTP": None, "Today's Open": None, "Today's Low": None, "Today's High": None, **live.get(("NSE_EQ", str(security_id)), {}), **history.get(security_id, {})}
        rows.append(row)
    data = calculate(pd.DataFrame(rows))
    columns = ["Stock", "Sector", "Sector A/D Ratio", "Sector Bias", "LTP", "PDC", "PDH", "PDL", "PDC to PDH %", "PDC to PDL %"]
    display = data[columns].sort_values(["Sector", "Stock"])
    st.metric("Stocks scanned", len(display))
    st.dataframe(display, use_container_width=True, hide_index=True, column_config={"Sector A/D Ratio": st.column_config.NumberColumn(format="%.2f"), "LTP": st.column_config.NumberColumn(format="%.2f"), "PDC": st.column_config.NumberColumn(format="%.2f"), "PDH": st.column_config.NumberColumn(format="%.2f"), "PDL": st.column_config.NumberColumn(format="%.2f"), "PDC to PDH %": st.column_config.NumberColumn(format="%.2f%%"), "PDC to PDL %": st.column_config.NumberColumn(format="%.2f%%")})
    st.caption(f"Historical data cached for {end.isoformat()}; live quotes refresh every 15 seconds.")


st.title("📈 Nifty Midcap 150 Stock Scanner")
st.caption("Historical OHLC loads once per day; Dhan live LTP refreshes every 15 seconds.")
try:
    if hasattr(st, "fragment"):
        st.fragment(run_every="15s")(render_scanner)()
    else:
        render_scanner()
        st.info("Upgrade Streamlit to use automatic 15-second refresh. Historical data is cached for 24 hours.")
except Exception as error:
    st.error(f"Scanner error: {error}")
