import os
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Dhan Stock Scanner", page_icon="📈", layout="wide")

REFRESH_SECONDS = 15
QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"


def get_secret(name: str) -> str:
    return os.getenv(name) or st.secrets.get(name, "")


def load_stocks() -> pd.DataFrame:
    """Load stock universe from stocks.csv and validate required columns."""
    path = "stocks.csv"
    if not os.path.exists(path):
        raise FileNotFoundError("stocks.csv not found. Add your Dhan stock universe first.")

    df = pd.read_csv(path)
    required = {"stock", "sector", "security_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"stocks.csv missing columns: {sorted(missing)}")

    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce")
    df = df.dropna(subset=["stock", "sector", "security_id"]).copy()
    if df.empty:
        raise ValueError("stocks.csv contains no valid stock rows.")
    return df


def load_reference_data() -> pd.DataFrame:
    """Optional reference columns used by the scanner's rules.

    Add these columns to reference.csv:
    stock,pdh,pdl,sector_ltp,sector_pdc,sector_ad
    """
    path = "reference.csv"
    if not os.path.exists(path):
        return pd.DataFrame(columns=["stock", "pdh", "pdl", "sector_ltp", "sector_pdc", "sector_ad"])

    ref = pd.read_csv(path)
    for col in ["pdh", "pdl", "sector_ltp", "sector_pdc", "sector_ad"]:
        if col in ref.columns:
            ref[col] = pd.to_numeric(ref[col], errors="coerce")
    return ref


def dhan_snapshot(stock_df: pd.DataFrame) -> dict:
    """Exactly one Dhan OHLC API request for the whole stock universe."""
    token = get_secret("DHAN_ACCESS_TOKEN")
    client_id = get_secret("DHAN_CLIENT_ID")
    if not token or not client_id:
        raise RuntimeError("Set DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID in Streamlit secrets or environment variables.")

    security_ids = sorted(set(int(x) for x in stock_df["security_id"]))
    payload = {"NSE_EQ": security_ids}

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
    }

    response = requests.post(QUOTE_URL, headers=headers, json=payload, timeout=10)
    response.raise_for_status()
    body = response.json()
    if body.get("status") not in (None, "success"):
        raise RuntimeError(str(body))
    return body


def flatten(snapshot: dict) -> dict[tuple[str, str], dict[str, float]]:
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


def build_frame(stocks: pd.DataFrame, prices: dict, reference: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rec in stocks.to_dict("records"):
        px = prices.get(("NSE_EQ", str(int(rec["security_id"]))))
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

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if not reference.empty and "stock" in reference.columns:
        keep = [c for c in ["stock", "pdh", "pdl", "sector_ltp", "sector_pdc", "sector_ad"] if c in reference.columns]
        ref = reference[keep].copy()
        df = df.merge(ref, left_on="Stock", right_on="stock", how="left").drop(columns=["stock"], errors="ignore")

    for col in ["pdh", "pdl", "sector_ltp", "sector_pdc", "sector_ad"]:
        if col not in df.columns:
            df[col] = float("nan")

    return df


def calculate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    # Ranking metrics: smallest percentage distance between PDC and PDH/PDL first.
    df["Buy Gap %"] = ((df["pdh"] - df["PDC"]) / df["PDC"]) * 100
    df["Sell Gap %"] = ((df["PDC"] - df["pdl"]) / df["PDC"]) * 100
    df["Sector % from PDC"] = ((df["sector_ltp"] - df["sector_pdc"]) / df["sector_pdc"]) * 100

    # User-specified buy rules.
    df["Buy Alignment"] = (
        (df["pdh"].notna())
        & (df["sector_ltp"].notna())
        & (df["sector_pdc"].notna())
        & (df["sector_ad"].notna())
        & (df["LTP"] > df["pdh"])
        & (df["sector_ltp"] > df["sector_pdc"])
        & (df["Today's Low"] < df["pdh"])
        & (df["Today's High"] > df["pdh"])
        & (df["sector_ad"] > 1)
    )

    # Mirror sell rules using PDL.
    df["Sell Alignment"] = (
        (df["pdl"].notna())
        & (df["sector_ltp"].notna())
        & (df["sector_pdc"].notna())
        & (df["sector_ad"].notna())
        & (df["LTP"] < df["pdl"])
        & (df["sector_ltp"] < df["sector_pdc"])
        & (df["Today's High"] > df["pdl"])
        & (df["Today's Low"] < df["pdl"])
        & (df["sector_ad"] < 1)
    )
    return df


def make_buy_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "Rank", "Stock", "Sector", "LTP", "PDC", "PDH", "Today's Open",
        "Today's Low", "Today's High", "Sector LTP", "Sector PDC",
        "Sector % from PDC", "Sector AD", "Buy Alignment"
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = df[df["Buy Alignment"]].sort_values("Buy Gap %", ascending=True).copy()
    out["PDH"] = out["pdh"]
    out["Sector LTP"] = out["sector_ltp"]
    out["Sector PDC"] = out["sector_pdc"]
    out["Sector AD"] = out["sector_ad"]
    out["Buy Alignment"] = "🟢 BUY"
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out[[
        "Rank", "Stock", "Sector", "LTP", "PDC", "PDH", "Today's Open",
        "Today's Low", "Today's High", "Sector LTP", "Sector PDC",
        "Sector % from PDC", "Sector AD", "Buy Alignment"
    ]]


def make_sell_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "Rank", "Stock", "Sector", "LTP", "PDC", "PDL", "Today's Open",
        "Today's Low", "Today's High", "Sector LTP", "Sector PDC",
        "Sector % from PDC", "Sector AD", "Sell Alignment"
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = df[df["Sell Alignment"]].sort_values("Sell Gap %", ascending=True).copy()
    out["PDL"] = out["pdl"]
    out["Sector LTP"] = out["sector_ltp"]
    out["Sector PDC"] = out["sector_pdc"]
    out["Sector AD"] = out["sector_ad"]
    out["Sell Alignment"] = "🔴 SELL"
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out[[
        "Rank", "Stock", "Sector", "LTP", "PDC", "PDL", "Today's Open",
        "Today's Low", "Today's High", "Sector LTP", "Sector PDC",
        "Sector % from PDC", "Sector AD", "Sell Alignment"
    ]]


def color_alignment(row: pd.Series):
    if row.astype(str).str.contains("🟢 BUY").any():
        return ["background-color: #d9f7df"] * len(row)
    if row.astype(str).str.contains("🔴 SELL").any():
        return ["background-color: #ffd9d9"] * len(row)
    return [""] * len(row)


st.title("📈 Dhan Buy / Sell Scanner")
st.caption("One Dhan snapshot every 15 seconds → one dataframe → all calculations/rankings from that same snapshot")

try:
    stocks = load_stocks()
    reference = load_reference_data()

    snapshot = dhan_snapshot(stocks)  # exactly one Dhan request per rerun
    prices = flatten(snapshot)
    data = calculate(build_frame(stocks, prices, reference))

    m1, m2, m3 = st.columns(3)
    m1.metric("Stocks", len(data))
    m2.metric("🟢 Buy", int(data["Buy Alignment"].sum()) if not data.empty else 0)
    m3.metric("🔴 Sell", int(data["Sell Alignment"].sum()) if not data.empty else 0)

    st.subheader("🟢 BUY WATCHLIST")
    buys = make_buy_table(data)
    st.dataframe(buys.style.apply(color_alignment, axis=1), use_container_width=True)

    st.subheader("🔴 SELL WATCHLIST")
    sells = make_sell_table(data)
    st.dataframe(sells.style.apply(color_alignment, axis=1), use_container_width=True)

    with st.expander("All scanned stocks"):
        st.dataframe(data, use_container_width=True)

    st.caption(f"Last Dhan fetch: {datetime.now():%Y-%m-%d %H:%M:%S} | Refresh: {REFRESH_SECONDS}s")

except Exception as exc:
    st.error(str(exc))

# One Streamlit rerun performs one market-data request.
time.sleep(REFRESH_SECONDS)
st.rerun()
