import os
import time
from datetime import datetime, time as dt_time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo
import re

import pandas as pd
import requests
import streamlit as st


# ============================================================
# PAGE SETTINGS
# ============================================================

st.set_page_config(
    page_title="Dhan Nifty Midcap 150 Scanner",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# SETTINGS
# ============================================================

REFRESH_SECONDS = 15
HISTORY_CACHE_TTL = 86400
HISTORY_REQUEST_DELAY = 0.6
QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"
HISTORY_URL = "https://api.dhan.co/v2/charts/historical"
NSE_URL = (
    "https://www.nseindia.com/api/equity-stockIndices"
    "?index=NIFTY%20MIDCAP%20150"
)
NIFTY_CSV_URL = (
    "https://www.niftyindices.com/IndexConstituent/"
    "ind_niftymidcap150list.csv"
)
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# DHAN CREDENTIALS
# ============================================================

def secret(name):
    try:
        return os.getenv(name) or st.secrets.get(name, "")
    except Exception:
        return os.getenv(name, "")


def headers():
    client_id = secret("DHAN_CLIENT_ID")
    access_token = secret("DHAN_ACCESS_TOKEN")

    if not client_id or not access_token:
        raise RuntimeError(
            "Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets."
        )

    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


# ============================================================
# HELPERS
# ============================================================

def norm(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper().strip())


def first_column(frame, names):
    lookup = {
        str(column).lower().replace(" ", "_"): column
        for column in frame.columns
    }
    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def universe_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/125 Safari/537.36"
            ),
            "Accept": "application/json,text/csv,*/*",
            "Referer": "https://www.nseindia.com/",
        }
    )
    return session


# ============================================================
# LOAD NIFTY MIDCAP 150 STOCKS
# ============================================================

@st.cache_data(ttl=86400, show_spinner=False)
def download_nifty_midcap_150():
    session = universe_session()
    errors = []
    nse = None

    # NSE API first.
    try:
        session.get("https://www.nseindia.com/", timeout=20)
        response = session.get(NSE_URL, timeout=30)
        response.raise_for_status()
        nse = pd.DataFrame(response.json().get("data", []))
    except Exception as exc:
        errors.append(f"NSE API: {exc}")

    # Nifty Indices CSV fallback.
    if nse is None or nse.empty:
        try:
            response = session.get(NIFTY_CSV_URL, timeout=30)
            response.raise_for_status()
            nse = pd.read_csv(StringIO(response.text))
        except Exception as exc:
            errors.append(f"Nifty Midcap 150 CSV: {exc}")

    if nse is None or nse.empty:
        raise RuntimeError("; ".join(errors))

    symbol_col = first_column(nse, ["symbol", "company_symbol"])
    if not symbol_col:
        raise RuntimeError(
            f"No symbol column in Nifty Midcap 150 data: {list(nse.columns)}"
        )

    nse["stock"] = nse[symbol_col].astype(str).str.strip().str.upper()
    nse = nse[~nse["stock"].str.contains("NIFTY", na=False)].copy()

    sector_col = first_column(
        nse,
        ["industry", "sector", "industry_info"],
    )
    nse["sector"] = (
        nse[sector_col].astype(str).str.strip()
        if sector_col
        else "UNKNOWN"
    )
    nse["join_key"] = nse["stock"].map(norm)
    nse = nse.drop_duplicates("join_key")

    # Download Dhan master.
    master_response = session.get(DHAN_MASTER_URL, timeout=90)
    master_response.raise_for_status()
    master = pd.read_csv(StringIO(master_response.text), low_memory=False)

    segment_col = first_column(
        master,
        [
            "exchange_segment",
            "exchange_segment_name",
            "exchange_segment_code",
            "sem_exm_exch_id",
        ],
    )
    market_segment_col = first_column(master, ["sem_segment", "segment"])
    symbol_master_col = first_column(
        master,
        ["symbol", "trading_symbol", "sem_trading_symbol"],
    )
    id_col = first_column(
        master,
        ["security_id", "sem_security_id", "sem_smst_security_id"],
    )

    if not (segment_col and symbol_master_col and id_col):
        raise RuntimeError(
            "Dhan master columns not recognised: "
            f"{list(master.columns)[:25]}"
        )

    dm = master.copy()
    exchange_values = dm[segment_col].astype(str).str.upper()

    if segment_col.lower() == "sem_exm_exch_id":
        dm = dm[exchange_values.eq("NSE")]
        if market_segment_col:
            dm = dm[
                dm[market_segment_col]
                .astype(str)
                .str.upper()
                .isin(["E", "EQUITY", "NSE_EQ"])
            ]
    else:
        dm = dm[exchange_values.eq("NSE_EQ")]

    dm["join_key"] = dm[symbol_master_col].map(norm)
    dm["security_id"] = pd.to_numeric(dm[id_col], errors="coerce")
    dm = (
        dm.dropna(subset=["security_id"])
        .drop_duplicates("join_key")
        [["join_key", "security_id"]]
    )

    result = nse[["stock", "sector", "join_key"]].merge(
        dm,
        on="join_key",
        how="inner",
    )
    result["security_id"] = result["security_id"].astype(int)
    result = (
        result[["stock", "sector", "security_id"]]
        .drop_duplicates("stock")
        .sort_values("stock")
        .reset_index(drop=True)
    )

    if len(result) != 150:
        raise RuntimeError(
            f"Expected 150 Nifty Midcap stocks, but mapped {len(result)} to Dhan."
        )

    return result


def load_stocks():
    return download_nifty_midcap_150()


# ============================================================
# MARKET STATUS
# ============================================================

def market_open(now):
    return (
        now.weekday() < 5
        and dt_time(9, 15) <= now.time() <= dt_time(15, 30)
    )


# ============================================================
# LIVE BATCH REQUEST
# ============================================================

def dhan_snapshot(stocks):
    security_ids = sorted({int(value) for value in stocks["security_id"]})
    if not security_ids:
        return {}

    response = requests.post(
        QUOTE_URL,
        headers=headers(),
        json={"NSE_EQ": security_ids},
        timeout=60,
    )

    if response.status_code == 429:
        raise RuntimeError("Dhan live quote rate limit reached. Wait before trying again.")

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


# ============================================================
# HISTORICAL REQUEST
# ============================================================

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
        response = requests.post(
            HISTORY_URL,
            headers=headers(),
            json=payload,
            timeout=30,
        )

        if response.status_code == 429:
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": "HTTP 429: Dhan rate limit reached",
                "rate_limited": True,
            }

        if not response.ok:
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": f"HTTP {response.status_code}: {response.text[:250]}",
                "rate_limited": False,
            }

        body = response.json()
        data = (body.get("data") or body) if isinstance(body, dict) else {}
        fields = ["timestamp", "close", "high", "low"]

        if not isinstance(data, dict) or any(field not in data for field in fields):
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": "Invalid or incomplete historical response",
                "rate_limited": False,
            }

        frame = pd.DataFrame({field: data[field] for field in fields})
        for field in fields:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")

        frame = (
            frame.dropna(subset=fields)
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

        if frame.empty:
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": "No valid historical candles",
                "rate_limited": False,
            }

        latest = frame.iloc[-1]
        return {
            "pdc": float(latest["close"]),
            "pdh": float(latest["high"]),
            "pdl": float(latest["low"]),
            "error": None,
            "rate_limited": False,
        }

    except Exception as exc:
        return {
            "pdc": None,
            "pdh": None,
            "pdl": None,
            "error": str(exc),
            "rate_limited": False,
        }


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def historical(stocks_key, start, end, refresh_key):
    result = {}
    total = len(stocks_key)
    progress = st.progress(0, text="Loading previous-day OHLC for Midcap 150...")

    for completed, security_id in enumerate(stocks_key, start=1):
        result[int(security_id)] = historical_one(security_id, start, end)
        progress.progress(
            completed / total if total else 1.0,
            text=f"Historical data: {completed}/{total}",
        )
        time.sleep(HISTORY_REQUEST_DELAY)

    progress.empty()
    return result


# ============================================================
# BUILD ONE TABLE
# ============================================================

def build_frame(stocks, live, history):
    rows = []
    for record in stocks.to_dict("records"):
        security_id = int(record["security_id"])
        hist = history.get(security_id, {})
        live_data = live.get(("NSE_EQ", str(security_id)), {})
        rows.append(
            {
                "Stock": record["stock"],
                "Sector": record["sector"],
                "LTP": live_data.get("ltp"),
                "PDC": hist.get("pdc"),
                "PDH": hist.get("pdh"),
                "PDL": hist.get("pdl"),
                "Today's Open": live_data.get("open"),
                "Today's Low": live_data.get("low"),
                "Today's High": live_data.get("high"),
                "History Error": hist.get("error"),
            }
        )
    return pd.DataFrame(rows)


def calculate(frame):
    df = frame.copy()
    if df.empty:
        return df

    numeric_columns = [
        "LTP", "PDC", "PDH", "PDL",
        "Today's Open", "Today's Low", "Today's High",
    ]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    # Sector breadth = number of stocks above PDC / number below PDC.
    up = (
        df["LTP"].gt(df["PDC"]).fillna(False)
        .groupby(df["Sector"])
        .transform("sum")
    )
    down = (
        df["LTP"].lt(df["PDC"]).fillna(False)
        .groupby(df["Sector"])
        .transform("sum")
    )
    df["Sector A/D Ratio"] = up / down.replace(0, float("nan"))

    valid_pdc = df["PDC"].gt(0)
    df["PDC to PDH %"] = (
        (df["PDH"] - df["PDC"]) / df["PDC"] * 100
    ).where(valid_pdc)
    df["PDC to PDL %"] = (
        (df["PDC"] - df["PDL"]) / df["PDC"] * 100
    ).where(valid_pdc)

    return df


# ============================================================
# APP UI
# ============================================================

st.title("📈 Nifty Midcap 150 Stock Scanner")
st.caption(
    "Nifty Midcap 150 universe | One table | "
    "Sector A/D Ratio | LTP, PDC, PDH, PDL | "
    "PDC→PDH and PDC→PDL percentages"
)

now = datetime.now(IST)
opened = market_open(now)

st.info(
    f"India time: {now:%Y-%m-%d %H:%M:%S IST} | "
    f"{'Market open' if opened else 'Market closed; LTP/today OHLC may be blank.'}"
)

if now.weekday() < 5 and now.time() >= dt_time(9, 0):
    refresh_key = now.date().isoformat()
else:
    refresh_key = "pre-open-" + now.date().isoformat()


try:
    stocks = load_stocks()

    # Most recent completed weekday for previous-day candle values.
    end_date = now.date() - timedelta(days=1)
    while end_date.weekday() >= 5:
        end_date -= timedelta(days=1)

    start = (end_date - timedelta(days=10)).isoformat()
    end = end_date.isoformat()

    stocks_key = tuple(
        sorted(int(value) for value in stocks["security_id"].tolist())
    )

    history = historical(stocks_key, start, end, refresh_key)

    if opened:
        live = flatten(dhan_snapshot(stocks))
    else:
        live = {}

    data = calculate(build_frame(stocks, live, history))

    # Keep exactly one requested table.
    display_columns = [
        "Stock",
        "Sector",
        "Sector A/D Ratio",
        "LTP",
        "PDC",
        "PDH",
        "PDL",
        "PDC to PDH %",
        "PDC to PDL %",
    ]

    display = data[display_columns].copy()
    display = display.sort_values(["Sector", "Stock"], na_position="last")

    a, b, c = st.columns(3)
    a.metric("Stocks scanned", len(display))
    b.metric("Stocks above PDC", int(data["LTP"].gt(data["PDC"]).sum()))
    c.metric("Stocks below PDC", int(data["LTP"].lt(data["PDC"]).sum()))

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Sector A/D Ratio": st.column_config.NumberColumn(
                format="%.2f"
            ),
            "LTP": st.column_config.NumberColumn(format="%.2f"),
            "PDC": st.column_config.NumberColumn(format="%.2f"),
            "PDH": st.column_config.NumberColumn(format="%.2f"),
            "PDL": st.column_config.NumberColumn(format="%.2f"),
            "PDC to PDH %": st.column_config.NumberColumn(format="%.2f%%"),
            "PDC to PDL %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

    with st.expander("Historical API details"):
        errors = data.loc[
            data["History Error"].notna(),
            ["Stock", "History Error"],
        ]
        if errors.empty:
            st.success("No historical API errors.")
        else:
            st.dataframe(errors, use_container_width=True, hide_index=True)

    if not opened:
        st.warning(
            "Live LTP/today OHLC are blank while NSE is closed. "
            "PDC/PDH/PDL are the most recent completed daily candle."
        )

    st.caption(
        f"Last check: {now:%Y-%m-%d %H:%M:%S IST} | Refresh: {REFRESH_SECONDS}s"
    )

except Exception as exc:
    st.error(f"Scanner error: {exc}")


# ============================================================
# LIVE REFRESH
# ============================================================

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="dhan_live_refresh")
except ImportError:
    time.sleep(REFRESH_SECONDS)
    st.rerun()
