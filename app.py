import os
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Dhan Stock Scanner", page_icon="📈", layout="wide")
REFRESH_SECONDS = 15
QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"

# Put your stock universe here: one row per stock.
# security_id must be the Dhan NSE_EQ security ID.
STOCKS = pd.DataFrame([
    {"stock": "RELIANCE", "sector": "ENERGY", "security_id": 1333},
    {"stock": "HDFCBANK", "sector": "BANK", "security_id": 1333},
])

# Optional sector index/proxy configuration.
# Replace 0 with the correct Dhan security ID for each sector proxy you use.
SECTOR_PROXIES = {
    "ENERGY": 0,
    "BANK": 0,
}


def get_secret(name: str) -> str:
    return os.getenv(name) or st.secrets.get(name, "")


def dhan_snapshot(stock_df: pd.DataFrame) -> dict:
    token = get_secret("DHAN_ACCESS_TOKEN")
    client_id = get_secret("DHAN_CLIENT_ID")
    if not token or not client_id:
        raise RuntimeError("Set DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID in environment variables or Streamlit secrets.")

    securities = {"NSE_EQ": [int(x) for x in stock_df["security_id"].dropna().unique()]}
    sector_ids = [int(x) for x in SECTOR_PROXIES.values() if int(x) > 0]
    if sector_ids:
        securities["NSE_EQ"] = sorted(set(securities["NSE_EQ"] + sector_ids))

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }
    r = requests.post(QUOTE_URL, headers=headers, json=securities, timeout=10)
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") not in (None, "success"):
        raise RuntimeError(str(payload))
    return payload


def flatten(snapshot: dict) -> dict:
    result = {}
    for segment, instruments in snapshot.get("data", {}).items():
        for security_id, packet in instruments.items():
            o = packet.get("ohlc", {}) or {}
            result[(segment, str(security_id))] = {
                "ltp": float(packet.get("last_price", float("nan"))),
                "open": float(o.get("open", float("nan"))),
                "high": float(o.get("high", float("nan"))),
                "low": float(o.get("low", float("nan"))),
                "pdc": float(o.get("close", float("nan"))),
            }
    return result


def build_frame(stock_df: pd.DataFrame, prices: dict) -> pd.DataFrame:
    rows = []
    for rec in stock_df.to_dict("records"):
        p = prices.get(("NSE_EQ", str(int(rec["security_id"]))))
        if not p:
            continue
        rows.append({
            "Stock": rec["stock"],
            "Sector": rec["sector"],
            "LTP": p["ltp"],
            "PDC": p["pdc"],
            "Today's Open": p["open"],
            "Today's Low": p["low"],
            "Today's High": p["high"],
            "PDH": float("nan"),
            "PDL": float("nan"),
            "Sector LTP": float("nan"),
            "Sector PDC": float("nan"),
            "Sector % from PDC": float("nan"),
            "Sector AD": float("nan"),
        })
    return pd.DataFrame(rows)


def calculate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    # Rank metric: closest previous close to previous high/low.
    df["Buy Gap %"] = ((df["PDH"] - df["PDC"]) / df["PDC"]) * 100
    df["Sell Gap %"] = ((df["PDC"] - df["PDL"]) / df["PDC"]) * 100

    df["Buy Alignment"] = (
        (df["LTP"] > df["PDH"])
        & (df["Sector LTP"] > df["Sector PDC"])
        & (df["Today's Low"] < df["PDH"])
        & (df["Today's High"] > df["PDH"])
        & (df["Sector AD"] > 1)
    )

    df["Sell Alignment"] = (
        (df["LTP"] < df["PDL"])
        & (df["Sector LTP"] < df["Sector PDC"])
        & (df["Today's Low"] < df["PDL"])
        & (df["Today's High"] > df["PDL"])
        & (df["Sector AD"] < 1)
    )
    return df


def buy_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["Stock", "Sector", "LTP", "PDC", "PDH", "Today's Open", "Today's Low", "Today's High", "Sector LTP", "Sector PDC", "Sector % from PDC", "Sector AD", "Buy Alignment"]
    out = df[df["Buy Alignment"]].sort_values("Buy Gap %", ascending=True).copy()
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out[["Rank"] + cols]


def sell_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["Stock", "Sector", "LTP", "PDC", "PDL", "Today's Open", "Today's Low", "Today's High", "Sector LTP", "Sector PDC", "Sector % from PDC", "Sector AD", "Sell Alignment"]
    out = df[df["Sell Alignment"]].sort_values("Sell Gap %", ascending=True).copy()
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out[["Rank"] + cols]


def highlight_alignment(row: pd.Series):
    flag = "Buy Alignment" if "Buy Alignment" in row.index else "Sell Alignment"
    ok = bool(row.get(flag, False))
    if ok:
        return ["background-color: #d9f7df" if flag.startswith("Buy") else "background-color: #ffd9d9"] * len(row)
    return [""] * len(row)

st.title("📈 Dhan Buy / Sell Scanner")
st.caption("One Dhan market-data snapshot every 15 seconds; all calculations use that same snapshot.")

try:
    snapshot = dhan_snapshot(STOCKS)
    prices = flatten(snapshot)
    data = calculate(build_frame(STOCKS, prices))

    c1, c2, c3 = st.columns(3)
    c1.metric("Stocks returned", len(data))
    c2.metric("Buy aligned", int(data["Buy Alignment"].sum()) if not data.empty else 0)
    c3.metric("Sell aligned", int(data["Sell Alignment"].sum()) if not data.empty else 0)

    st.subheader("🟢 Buy Watchlist")
    buys = buy_table(data) if not data.empty else pd.DataFrame()
    if buys.empty:
        st.info("No buy-aligned stock in this snapshot.")
    else:
        st.dataframe(buys.style.apply(highlight_alignment, axis=1), use_container_width=True)

    st.subheader("🔴 Sell Watchlist")
    sells = sell_table(data) if not data.empty else pd.DataFrame()
    if sells.empty:
        st.info("No sell-aligned stock in this snapshot.")
    else:
        st.dataframe(sells.style.apply(highlight_alignment, axis=1), use_container_width=True)

    st.caption(f"Last fetch: {datetime.now():%Y-%m-%d %H:%M:%S} | Refreshing every {REFRESH_SECONDS}s")

except Exception as e:
    st.error(str(e))

# One rerun = one Dhan request. Do not put additional Dhan calls in calculation/display functions.
time.sleep(REFRESH_SECONDS)
st.rerun()
