import os
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Dhan Stock Scanner", page_icon="📈", layout="wide")
REFRESH_SECONDS = 15
QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"
HISTORY_URL = "https://api.dhan.co/v2/charts/historical"
IST = ZoneInfo("Asia/Kolkata")


def secret(name):
    return os.getenv(name) or st.secrets.get(name, "")


def headers():
    token, client = secret("DHAN_ACCESS_TOKEN"), secret("DHAN_CLIENT_ID")
    if not token or not client:
        raise RuntimeError("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets.")
    return {"Accept": "application/json", "Content-Type": "application/json", "access-token": token, "client-id": client}


def load_stocks():
    df = pd.read_csv("stocks.csv")
    required = {"stock", "sector", "security_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"stocks.csv missing columns: {sorted(missing)}")
    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    return df.dropna(subset=["stock", "sector", "security_id"]).copy()


def market_open(now):
    return now.weekday() < 5 and dt_time(9, 15) <= now.time() <= dt_time(15, 30)


def dhan_snapshot(stocks):
    ids = sorted({int(x) for x in stocks.security_id})
    r = requests.post(QUOTE_URL, headers=headers(), json={"NSE_EQ": ids}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("status") not in (None, "success"):
        raise RuntimeError(body)
    return body


def flatten(snapshot):
    out = {}
    for segment, instruments in (snapshot.get("data") or {}).items():
        if not isinstance(instruments, dict):
            continue
        for sid, packet in instruments.items():
            o = packet.get("ohlc") or {}
            out[(segment, str(sid))] = {"ltp": packet.get("last_price"), "open": o.get("open"), "high": o.get("high"), "low": o.get("low")}
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def historical_one(security_id, from_date, to_date):
    payload = {"securityId": str(int(security_id)), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "expiryCode": 0, "oi": False, "fromDate": from_date, "toDate": to_date}
    r = requests.post(HISTORY_URL, headers=headers(), json=payload, timeout=20)
    r.raise_for_status()
    body = r.json()
    data = body.get("data", body)
    if not isinstance(data, dict) or not data.get("timestamp"):
        return {"pdc": None, "pdh": None, "pdl": None}
    rows = pd.DataFrame(data)
    rows["date"] = pd.to_datetime(rows["timestamp"], unit="s", utc=True).dt.tz_convert(IST).dt.date
    rows = rows.sort_values("date")
    if len(rows) < 2:
        return {"pdc": None, "pdh": None, "pdl": None}
    prev = rows.iloc[-2]
    return {"pdc": float(prev["close"]), "pdh": float(prev["high"]), "pdl": float(prev["low"])}


def historical(stocks, now):
    start = (now.date() - timedelta(days=14)).isoformat()
    end = (now.date() + timedelta(days=1)).isoformat()
    result = {}
    for sid in stocks.security_id:
        result[int(sid)] = historical_one(sid, start, end)
    return result


def build_frame(stocks, live, hist):
    rows = []
    for rec in stocks.to_dict("records"):
        sid = int(rec["security_id"])
        h, q = hist.get(sid, {}), live.get(("NSE_EQ", str(sid)), {})
        rows.append({"Stock": rec["stock"], "Sector": rec["sector"], "LTP": q.get("ltp"), "PDC": h.get("pdc"), "PDH": h.get("pdh"), "PDL": h.get("pdl"), "Today's Open": q.get("open"), "Today's Low": q.get("low"), "Today's High": q.get("high")})
    return pd.DataFrame(rows)


def calculate(df):
    if df.empty:
        return df
    df = df.copy()
    df["Sector LTP"] = df.groupby("Sector")["LTP"].transform("mean")
    df["Sector PDC"] = df.groupby("Sector")["PDC"].transform("mean")
    up = df["LTP"].gt(df["PDC"]).groupby(df["Sector"]).transform("sum")
    down = df["LTP"].lt(df["PDC"]).groupby(df["Sector"]).transform("sum")
    df["Sector AD"] = up / down.replace(0, float("nan"))
    df["Sector % from PDC"] = (df["Sector LTP"] - df["Sector PDC"]) / df["Sector PDC"] * 100
    buy = df["PDC"].notna() & df["PDH"].notna() & df["LTP"].notna() & df["Sector LTP"].notna() & df["PDC"].gt(0)
    buy &= ((df["PDH"] - df["PDC"]) / df["PDC"] * 100 <= 1)
    buy &= df["LTP"].gt(df["PDH"]) & df["Sector LTP"].gt(df["Sector PDC"])
    buy &= df["Today's Low"].lt(df["PDH"]) & df["Today's High"].gt(df["PDH"]) & df["Sector AD"].gt(1)
    sell = df["PDC"].notna() & df["PDL"].notna() & df["LTP"].notna() & df["PDC"].gt(0)
    sell &= ((df["PDC"] - df["PDL"]) / df["PDC"] * 100 <= 1)
    sell &= df["LTP"].lt(df["PDL"]) & df["Sector LTP"].lt(df["Sector PDC"])
    sell &= df["Today's High"].gt(df["PDL"]) & df["Today's Low"].lt(df["PDL"]) & df["Sector AD"].lt(1)
    df["Buy Alignment 🟢"] = buy
    df["Sell Alignment 🔴"] = sell
    return df


def table(df, kind):
    if df.empty:
        return pd.DataFrame(columns=["Rank", "Stock", "Sector", "LTP", "PDC", "PDH" if kind == "buy" else "PDL", "Today's Open", "Today's Low", "Today's High", "Sector LTP", "Sector PDC", "Sector % from PDC", "Sector AD", "Buy Alignment 🟢" if kind == "buy" else "Sell Alignment 🔴"])
    level = "PDH" if kind == "buy" else "PDL"
    align = "Buy Alignment 🟢" if kind == "buy" else "Sell Alignment 🔴"
    x = df[df[align]].copy()
    x["Rank"] = range(1, len(x) + 1)
    cols = ["Rank", "Stock", "Sector", "LTP", "PDC", level, "Today's Open", "Today's Low", "Today's High", "Sector LTP", "Sector PDC", "Sector % from PDC", "Sector AD", align]
    return x[cols]


st.title("📈 Dhan Buy / Sell Scanner")
st.caption("Historical PDH/PDL/PDC once daily + one live Dhan snapshot every 15 seconds")
now = datetime.now(IST)
open_now = market_open(now)
st.info(f"India time: {now:%Y-%m-%d %H:%M:%S IST} | {'Market open' if open_now else 'Market closed; historical values remain available.'}")

try:
    stocks = load_stocks()
    hist = historical(stocks, now)
    live = flatten(dhan_snapshot(stocks)) if open_now else {}
    data = calculate(build_frame(stocks, live, hist))
    buys = table(data, "buy")
    sells = table(data, "sell")
    a, b, c = st.columns(3)
    a.metric("Stocks", len(data)); b.metric("🟢 Buy", len(buys)); c.metric("🔴 Sell", len(sells))
    if not open_now:
        st.warning("Live LTP/today OHLC are blank while NSE is closed. PDC/PDH/PDL are historical Dhan values.")
    st.subheader("🟢 BUY WATCHLIST"); st.dataframe(buys, use_container_width=True)
    st.subheader("🔴 SELL WATCHLIST"); st.dataframe(sells, use_container_width=True)
    with st.expander("All scanned stocks"):
        st.dataframe(data, use_container_width=True)
    st.caption(f"Last check: {now:%Y-%m-%d %H:%M:%S IST} | Refresh: {REFRESH_SECONDS}s")
except Exception as exc:
    st.error(f"Scanner error: {exc}")

time.sleep(REFRESH_SECONDS)
st.rerun()
