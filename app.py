import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Dhan Stock Scanner", page_icon="📈", layout="wide")

REFRESH_SECONDS = 15
QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"
IST = ZoneInfo("Asia/Kolkata")


def get_secret(name: str) -> str:
    return os.getenv(name) or st.secrets.get(name, "")


def india_now() -> datetime:
    return datetime.now(IST)


def market_status(now: datetime) -> tuple[bool, str]:
    # NSE regular session: Monday-Friday, 09:15-15:30 IST.
    if now.weekday() >= 5:
        return False, "Market is closed today because it is Saturday/Sunday."
    if now.time() < dt_time(9, 15):
        return False, "Market has not opened yet. NSE regular trading starts at 09:15 IST."
    if now.time() > dt_time(15, 30):
        return False, "NSE regular trading has ended for today at 15:30 IST."
    return True, "NSE regular market hours are active."


def load_stocks() -> pd.DataFrame:
    df = pd.read_csv("stocks.csv")
    required = {"stock", "sector", "security_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"stocks.csv missing columns: {sorted(missing)}")
    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    df = df.dropna(subset=["stock", "sector", "security_id"]).copy()
    if df.empty:
        raise ValueError("stocks.csv contains no valid rows")
    return df


def dhan_snapshot(stock_df: pd.DataFrame) -> dict:
    token = get_secret("DHAN_ACCESS_TOKEN")
    client_id = get_secret("DHAN_CLIENT_ID")
    if not token or not client_id:
        raise RuntimeError("Dhan secrets are missing. Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets.")

    ids = sorted({int(x) for x in stock_df["security_id"]})
    response = requests.post(
        QUOTE_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
            "client-id": client_id,
        },
        json={"NSE_EQ": ids},
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") not in (None, "success"):
        raise RuntimeError(f"Dhan API response: {body}")
    return body


def flatten(snapshot: dict) -> dict:
    result = {}
    data = snapshot.get("data") or {}
    for segment, instruments in data.items():
        if not isinstance(instruments, dict):
            continue
        for security_id, packet in instruments.items():
            if not isinstance(packet, dict):
                continue
            o = packet.get("ohlc") or {}
            result[(segment, str(security_id))] = {
                "ltp": packet.get("last_price"),
                "open": o.get("open"),
                "high": o.get("high"),
                "low": o.get("low"),
                "pdc": o.get("close"),
            }
    return result


def build_frame(stocks: pd.DataFrame, prices: dict) -> pd.DataFrame:
    rows = []
    for rec in stocks.to_dict("records"):
        key = ("NSE_EQ", str(int(rec["security_id"])))
        px = prices.get(key)
        if not px:
            continue
        rows.append({
            "Stock": rec["stock"],
            "Sector": rec["sector"],
            "LTP": px["ltp"],
            "PDC": px["pdc"],
            "Today's Open": px["open"],
            "Today's Low": px["low"],
            "Today's High": px["high"],
        })
    return pd.DataFrame(rows)


def calculate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    for col in ["pdh", "pdl", "sector_ltp", "sector_pdc", "sector_ad"]:
        df[col] = float("nan")
    df["Buy Alignment"] = False
    df["Sell Alignment"] = False
    return df


def empty_table(kind: str) -> pd.DataFrame:
    level = "PDH" if kind == "buy" else "PDL"
    alignment = "Buy Alignment" if kind == "buy" else "Sell Alignment"
    return pd.DataFrame(columns=[
        "Rank", "Stock", "Sector", "LTP", "PDC", level, "Today's Open",
        "Today's Low", "Today's High", "Sector LTP", "Sector PDC",
        "Sector % from PDC", "Sector AD", alignment,
    ])


st.title("📈 Dhan Buy / Sell Scanner")
st.caption("One Dhan snapshot every 15 seconds → one dataframe → all calculations/rankings from that same snapshot")

now_ist = india_now()
open_now, status_text = market_status(now_ist)
st.info(f"India time: {now_ist:%Y-%m-%d %H:%M:%S IST} | {status_text}")

try:
    stocks = load_stocks()

    if not open_now:
        m1, m2, m3 = st.columns(3)
        m1.metric("Stocks received", 0)
        m2.metric("🟢 Buy", 0)
        m3.metric("🔴 Sell", 0)
        st.warning("No live quotes are requested while the NSE regular market is closed. The scanner will try again automatically.")
        snapshot = {"data": {}}
        data = pd.DataFrame()
    else:
        snapshot = dhan_snapshot(stocks)
        prices = flatten(snapshot)
        data = calculate(build_frame(stocks, prices))

        m1, m2, m3 = st.columns(3)
        m1.metric("Stocks received", len(data))
        m2.metric("🟢 Buy", 0)
        m3.metric("🔴 Sell", 0)

        if data.empty:
            st.warning("Dhan returned no matching NSE_EQ instruments for this request.")
            st.info(f"Requested security IDs: {', '.join(str(int(x)) for x in stocks['security_id'])}")
            st.info(f"Dhan response data segments: {list((snapshot.get('data') or {}).keys())}")
            st.info("If this remains empty during market hours, the Dhan security IDs or API permissions need correction.")
        else:
            st.success(f"Received {len(data)} stock quotes from Dhan.")

    st.subheader("🟢 BUY WATCHLIST")
    st.dataframe(empty_table("buy"), use_container_width=True)

    st.subheader("🔴 SELL WATCHLIST")
    st.dataframe(empty_table("sell"), use_container_width=True)

    with st.expander("All scanned stocks"):
        st.dataframe(data, use_container_width=True)

    st.caption(f"Last check: {now_ist:%Y-%m-%d %H:%M:%S IST} | Refresh: {REFRESH_SECONDS}s")

except Exception as exc:
    st.error(f"Scanner error: {exc}")

# Refresh loop.
time.sleep(REFRESH_SECONDS)
st.rerun()
