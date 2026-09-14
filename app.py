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
    if not secret("DHAN_ACCESS_TOKEN") or not secret("DHAN_CLIENT_ID"):
        raise RuntimeError("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets.")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": secret("DHAN_ACCESS_TOKEN"),
        "client-id": secret("DHAN_CLIENT_ID"),
    }


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
    response = requests.post(
        QUOTE_URL,
        headers=headers(),
        json={"NSE_EQ": sorted({int(x) for x in stocks.security_id})},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def flatten(snapshot):
    output = {}
    for segment, items in (snapshot.get("data") or {}).items():
        for security_id, quote in (items or {}).items():
            ohlc = quote.get("ohlc") or {}
            output[(segment, str(security_id))] = {
                "ltp": quote.get("last_price"),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
            }
    return output


@st.cache_data(ttl=86400, show_spinner=False)
def historical_one(security_id, start, end):
    payload = {
        "securityId": str(int(security_id)),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "expiryCode": 0,
        "oi": False,
        "fromDate": start,
        "toDate": end,
    }
    try:
        response = requests.post(HISTORY_URL, headers=headers(), json=payload, timeout=20)
        if not response.ok:
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": f"security {security_id}: HTTP {response.status_code} {response.text[:250]}",
            }

        body = response.json()
        data = body.get("data") or body if isinstance(body, dict) else {}
        if not isinstance(data, dict):
            return {"pdc": None, "pdh": None, "pdl": None, "error": "Invalid historical response format"}

        fields = ["timestamp", "close", "high", "low"]
        missing = [field for field in fields if field not in data]
        if missing:
            available = ", ".join(sorted(str(key) for key in data.keys())[:20])
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": f"Missing candle fields: {', '.join(missing)}. Received: {available}",
            }

        frame = pd.DataFrame({field: data[field] for field in fields})
        for field in fields:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        frame = frame.dropna(subset=fields).sort_values("timestamp")
        if frame.empty:
            return {"pdc": None, "pdh": None, "pdl": None, "error": "Historical response contained no valid candles"}

        latest = frame.iloc[-1]
        return {
            "pdc": float(latest["close"]),
            "pdh": float(latest["high"]),
            "pdl": float(latest["low"]),
            "error": None,
        }
    except Exception as exc:
        return {"pdc": None, "pdh": None, "pdl": None, "error": str(exc)}


def historical(stocks, now):
    start = (now.date() - timedelta(days=30)).isoformat()
    end = now.date().isoformat()
    return {int(security_id): historical_one(security_id, start, end) for security_id in stocks.security_id}


def build_frame(stocks, live, history):
    rows = []
    for record in stocks.to_dict("records"):
        security_id = int(record["security_id"])
        historical_values = history.get(security_id, {})
        quote = live.get(("NSE_EQ", str(security_id)), {})
        rows.append({
            "Stock": record["stock"],
            "Sector": record["sector"],
            "LTP": quote.get("ltp"),
            "PDC": historical_values.get("pdc"),
            "PDH": historical_values.get("pdh"),
            "PDL": historical_values.get("pdl"),
            "Today's Open": quote.get("open"),
            "Today's Low": quote.get("low"),
            "Today's High": quote.get("high"),
            "History Error": historical_values.get("error"),
        })
    return pd.DataFrame(rows)


def calculate(frame):
    if frame.empty:
        return frame
    df = frame.copy()
    numeric_columns = ["LTP", "PDC", "PDH", "PDL", "Today's Open", "Today's Low", "Today's High"]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["Sector LTP"] = df.groupby("Sector")["LTP"].transform("mean")
    df["Sector PDC"] = df.groupby("Sector")["PDC"].transform("mean")
    up = df["LTP"].gt(df["PDC"]).fillna(False).groupby(df["Sector"]).transform("sum")
    down = df["LTP"].lt(df["PDC"]).fillna(False).groupby(df["Sector"]).transform("sum")
    df["Sector AD"] = up / down.replace(0, float("nan"))
    df["Sector % from PDC"] = (df["Sector LTP"] - df["Sector PDC"]) / df["Sector PDC"] * 100

    buy = df["PDC"].notna() & df["PDH"].notna() & df["LTP"].notna() & df["Sector LTP"].notna() & df["Sector PDC"].notna()
    buy &= df["PDC"].gt(0) & ((df["PDH"] - df["PDC"]) / df["PDC"] * 100 <= 1)
    buy &= df["LTP"].gt(df["PDH"]) & df["Sector LTP"].gt(df["Sector PDC"])
    buy &= df["Today's Low"].lt(df["PDH"]) & df["Today's High"].gt(df["PDH"]) & df["Sector AD"].gt(1)

    sell = df["PDC"].notna() & df["PDL"].notna() & df["LTP"].notna() & df["Sector LTP"].notna() & df["Sector PDC"].notna()
    sell &= df["PDC"].gt(0) & ((df["PDC"] - df["PDL"]) / df["PDC"] * 100 <= 1)
    sell &= df["LTP"].lt(df["PDL"]) & df["Sector LTP"].lt(df["Sector PDC"])
    sell &= df["Today's High"].gt(df["PDL"]) & df["Today's Low"].lt(df["PDL"]) & df["Sector AD"].lt(1)

    df["Buy Alignment 🟢"] = buy.fillna(False)
    df["Sell Alignment 🔴"] = sell.fillna(False)
    return df


def table(df, kind):
    level = "PDH" if kind == "buy" else "PDL"
    alignment = "Buy Alignment 🟢" if kind == "buy" else "Sell Alignment 🔴"
    columns = ["Rank", "Stock", "Sector", "LTP", "PDC", level, "Today's Open", "Today's Low", "Today's High", "Sector LTP", "Sector PDC", "Sector % from PDC", "Sector AD", alignment]
    if df.empty:
        return pd.DataFrame(columns=columns)
    selected = df[df[alignment].fillna(False)].copy()
    if selected.empty:
        return pd.DataFrame(columns=columns)
    distance = (selected["PDH"] - selected["PDC"]) / selected["PDC"] * 100 if kind == "buy" else (selected["PDC"] - selected["PDL"]) / selected["PDC"] * 100
    selected = selected.assign(_distance=distance).sort_values("_distance")
    selected["Rank"] = range(1, len(selected) + 1)
    return selected[columns]


st.title("📈 Dhan Stock Scanner")
st.caption("Historical PDH/PDL/PDC once daily + one live Dhan snapshot every 15 seconds")
now = datetime.now(IST)
opened = market_open(now)
st.info(f"India time: {now:%Y-%m-%d %H:%M:%S IST} | {'Market open' if opened else 'Market closed; historical values remain available.'}")

try:
    stocks = load_stocks()
    history = historical(stocks, now)
    live = flatten(dhan_snapshot(stocks)) if opened else {}
    data = calculate(build_frame(stocks, live, history))
    buys = table(data, "buy")
    sells = table(data, "sell")

    first, second, third = st.columns(3)
    first.metric("Stocks", len(data))
    second.metric("🟢 Buy", len(buys))
    third.metric("🔴 Sell", len(sells))

    errors = data.loc[data["History Error"].notna(), ["Stock", "History Error"]]
    if not errors.empty:
        with st.expander("Historical API details"):
            st.dataframe(errors, use_container_width=True)
    if not opened:
        st.warning("Live LTP/today OHLC are blank while NSE is closed. PDC/PDH/PDL are historical Dhan values.")

    st.subheader("🟢 BUY WATCHLIST")
    st.dataframe(buys, use_container_width=True)
    st.subheader("🔴 SELL WATCHLIST")
    st.dataframe(sells, use_container_width=True)
    with st.expander("All scanned stocks"):
        st.dataframe(data, use_container_width=True)
    st.caption(f"Last check: {now:%Y-%m-%d %H:%M:%S IST} | Refresh: {REFRESH_SECONDS}s")
except Exception as exc:
    st.error(f"Scanner error: {exc}")

time.sleep(REFRESH_SECONDS)
st.rerun()
