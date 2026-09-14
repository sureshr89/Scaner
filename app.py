import os
import time
from datetime import datetime, time as dt_time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo
from pathlib import Path
import re

import pandas as pd
import requests
import streamlit as st


# ============================================================
# PAGE SETTINGS
# ============================================================

st.set_page_config(
    page_title="Dhan Stock Scanner",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# SETTINGS
# ============================================================

REFRESH_SECONDS = 15

# Historical data is cached for 24 hours.
HISTORY_CACHE_TTL = 86400

# Delay between historical requests during the first load.
HISTORY_REQUEST_DELAY = 1.0

# Small parallel batch size: faster startup without flooding Dhan.
HISTORY_WORKERS = 1

QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"
HISTORY_URL = "https://api.dhan.co/v2/charts/historical"

NSE_URL = (
    "https://www.nseindia.com/api/equity-stockIndices"
    "?index=NIFTY%20500"
)

NIFTY_CSV_URL = (
    "https://www.niftyindices.com/IndexConstituent/"
    "ind_nifty500list.csv"
)

DHAN_MASTER_URL = (
    "https://images.dhan.co/api-data/api-scrip-master.csv"
)

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
            "Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
            "in Streamlit Secrets."
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
    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(value).upper().strip(),
    )


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
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/125 Safari/537.36"
            ),
            "Accept": "application/json,text/csv,*/*",
            "Referer": "https://www.nseindia.com/",
        }
    )

    return session


# ============================================================
# LOAD NIFTY 500 STOCKS
# ============================================================

@st.cache_data(ttl=86400, show_spinner=False)
def download_nifty500():
    session = universe_session()

    nse = None
    errors = []

    # Try NSE API first.
    try:
        session.get(
            "https://www.nseindia.com/",
            timeout=20,
        )

        response = session.get(
            NSE_URL,
            timeout=30,
        )

        response.raise_for_status()

        nse = pd.DataFrame(
            response.json().get("data", [])
        )

    except Exception as exc:
        errors.append(f"NSE API: {exc}")

    # Fallback to Nifty CSV.
    if nse is None or nse.empty:
        try:
            response = session.get(
                NIFTY_CSV_URL,
                timeout=30,
            )

            response.raise_for_status()

            nse = pd.read_csv(
                StringIO(response.text)
            )

        except Exception as exc:
            errors.append(f"Nifty CSV: {exc}")

    if nse is None or nse.empty:
        raise RuntimeError(
            "; ".join(errors)
        )

    symbol_col = first_column(
        nse,
        [
            "symbol",
            "company_symbol",
        ],
    )

    if not symbol_col:
        raise RuntimeError(
            "No symbol column in Nifty data: "
            f"{list(nse.columns)}"
        )

    nse["stock"] = (
        nse[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    nse = nse[
        ~nse["stock"].str.contains(
            "NIFTY",
            na=False,
        )
    ].copy()

    sector_col = first_column(
        nse,
        [
            "industry",
            "sector",
            "industry_info",
        ],
    )

    if sector_col:
        nse["sector"] = (
            nse[sector_col]
            .astype(str)
            .str.strip()
        )
    else:
        nse["sector"] = "UNKNOWN"

    nse["join_key"] = nse["stock"].map(norm)

    nse = nse.drop_duplicates(
        "join_key"
    )

    # Download Dhan master.
    master_response = session.get(
        DHAN_MASTER_URL,
        timeout=90,
    )

    master_response.raise_for_status()

    master = pd.read_csv(
        StringIO(master_response.text),
        low_memory=False,
    )

    segment_col = first_column(
        master,
        [
            "exchange_segment",
            "exchange_segment_name",
            "exchange_segment_code",
            "sem_exm_exch_id",
        ],
    )

    market_segment_col = first_column(
        master,
        [
            "sem_segment",
            "segment",
        ],
    )

    symbol_master_col = first_column(
        master,
        [
            "symbol",
            "trading_symbol",
            "sem_trading_symbol",
        ],
    )

    id_col = first_column(
        master,
        [
            "security_id",
            "sem_security_id",
            "sem_smst_security_id",
        ],
    )

    if not (
        segment_col
        and symbol_master_col
        and id_col
    ):
        raise RuntimeError(
            "Dhan master columns not recognised: "
            f"{list(master.columns)[:25]}"
        )

    dm = master.copy()

    exchange_values = (
        dm[segment_col]
        .astype(str)
        .str.upper()
    )

    if segment_col.lower() == "sem_exm_exch_id":
        dm = dm[
            exchange_values.eq("NSE")
        ]

        if market_segment_col:
            dm = dm[
                dm[market_segment_col]
                .astype(str)
                .str.upper()
                .isin(
                    [
                        "E",
                        "EQUITY",
                        "NSE_EQ",
                    ]
                )
            ]

    else:
        dm = dm[
            exchange_values.eq("NSE_EQ")
        ]

    dm["join_key"] = (
        dm[symbol_master_col]
        .map(norm)
    )

    dm["security_id"] = pd.to_numeric(
        dm[id_col],
        errors="coerce",
    )

    dm = (
        dm
        .dropna(subset=["security_id"])
        .drop_duplicates("join_key")
        [["join_key", "security_id"]]
    )

    result = nse[
        [
            "stock",
            "sector",
            "join_key",
        ]
    ].merge(
        dm,
        on="join_key",
        how="inner",
    )

    result["security_id"] = (
        result["security_id"]
        .astype(int)
    )

    result = (
        result[
            [
                "stock",
                "sector",
                "security_id",
            ]
        ]
        .drop_duplicates("stock")
        .sort_values("stock")
        .reset_index(drop=True)
    )

    if len(result) < 400:
        raise RuntimeError(
            f"Only {len(result)} stocks mapped "
            "from Nifty 500 to Dhan"
        )

    return result


def load_stocks():
    try:
        return download_nifty500()

    except Exception as exc:
        fallback = pd.read_csv(
            "stocks.csv"
        )

        fallback["security_id"] = pd.to_numeric(
            fallback["security_id"],
            errors="coerce",
        )

        fallback = fallback.dropna(
            subset=[
                "stock",
                "sector",
                "security_id",
            ]
        ).copy()

        st.error(
            "Nifty 500 loading failed; "
            f"using only {len(fallback)} "
            f"local stocks. Details: {exc}"
        )

        return fallback


# ============================================================
# MARKET STATUS
# ============================================================

def market_open(now):
    return (
        now.weekday() < 5
        and dt_time(9, 15)
        <= now.time()
        <= dt_time(15, 30)
    )


# ============================================================
# ONE LIVE BATCH REQUEST
# ============================================================

def dhan_snapshot(stocks):
    security_ids = sorted(
        {
            int(value)
            for value in stocks["security_id"]
        }
    )

    if not security_ids:
        return {}

    payload = {
        "NSE_EQ": security_ids
    }

    response = requests.post(
        QUOTE_URL,
        headers=headers(),
        json=payload,
        timeout=60,
    )

    if response.status_code == 429:
        raise RuntimeError(
            "Dhan live quote rate limit reached. "
            "Wait before trying again."
        )

    response.raise_for_status()

    return response.json()


def flatten(snapshot):
    output = {}

    for segment, items in (
        snapshot.get("data") or {}
    ).items():

        for security_id, quote in (
            items or {}
        ).items():

            ohlc = quote.get("ohlc") or {}

            output[
                (
                    segment,
                    str(security_id),
                )
            ] = {
                "ltp": quote.get(
                    "last_price"
                ),
                "open": ohlc.get(
                    "open"
                ),
                "high": ohlc.get(
                    "high"
                ),
                "low": ohlc.get(
                    "low"
                ),
            }

    return output


# ============================================================
# ONE HISTORICAL REQUEST
# ============================================================

def historical_one(
    security_id,
    start,
    end,
):
    payload = {
        "securityId": str(
            int(security_id)
        ),
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
                "error": (
                    "HTTP 429: Dhan rate limit reached"
                ),
                "rate_limited": True,
            }

        if not response.ok:
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:250]}"
                ),
                "rate_limited": False,
            }

        body = response.json()

        data = (
            body.get("data") or body
            if isinstance(body, dict)
            else {}
        )

        fields = [
            "timestamp",
            "close",
            "high",
            "low",
        ]

        if not isinstance(data, dict):
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": (
                    "Invalid historical response"
                ),
                "rate_limited": False,
            }

        if any(
            field not in data
            for field in fields
        ):
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": (
                    "Missing historical candle fields"
                ),
                "rate_limited": False,
            }

        frame = pd.DataFrame(
            {
                field: data[field]
                for field in fields
            }
        )

        for field in fields:
            frame[field] = pd.to_numeric(
                frame[field],
                errors="coerce",
            )

        frame = (
            frame
            .dropna(subset=fields)
            .sort_values("timestamp")
        )

        if frame.empty:
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": (
                    "No valid historical candles"
                ),
                "rate_limited": False,
            }

        row = frame.iloc[-1]

        return {
            "pdc": float(row["close"]),
            "pdh": float(row["high"]),
            "pdl": float(row["low"]),
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


# ============================================================
# HISTORICAL DATA
#
# This function is cached for 24 hours.
# It will NOT execute again on every 15-second rerun.
# ============================================================

@st.cache_data(
    ttl=HISTORY_CACHE_TTL,
    show_spinner=False,
)
def historical(stocks_key, start, end, refresh_key):
    """Load historical values sequentially to avoid Dhan HTTP 429 errors."""
    result = {}
    total = len(stocks_key)
    progress = st.progress(0, text="Loading historical data slowly to respect Dhan limits...")

    for completed, security_id in enumerate(stocks_key, start=1):
        values = historical_one(security_id, start, end)
        result[int(security_id)] = values

        progress.progress(
            completed / total if total else 1.0,
            text=f"Historical data: {completed}/{total}",
        )

        # Never burst requests. This is intentionally slow and safe.
        time.sleep(HISTORY_REQUEST_DELAY)

    progress.empty()
    return result


# ============================================================
# BUILD DATAFRAME
# ============================================================

def build_frame(
    stocks,
    live,
    history,
):
    rows = []

    for record in stocks.to_dict(
        "records"
    ):
        security_id = int(
            record["security_id"]
        )

        historical_data = history.get(
            security_id,
            {},
        )

        live_data = live.get(
            (
                "NSE_EQ",
                str(security_id),
            ),
            {},
        )

        rows.append(
            {
                "Stock": record["stock"],
                "Sector": record["sector"],
                "LTP": live_data.get(
                    "ltp"
                ),
                "PDC": historical_data.get(
                    "pdc"
                ),
                "PDH": historical_data.get(
                    "pdh"
                ),
                "PDL": historical_data.get(
                    "pdl"
                ),
                "Today's Open": live_data.get(
                    "open"
                ),
                "Today's Low": live_data.get(
                    "low"
                ),
                "Today's High": live_data.get(
                    "high"
                ),
                "History Error": historical_data.get(
                    "error"
                ),
            }
        )

    return pd.DataFrame(rows)


# ============================================================
# CALCULATIONS
# ============================================================

def calculate(frame):
    df = frame.copy()

    if df.empty:
        return df

    numeric_columns = [
        "LTP",
        "PDC",
        "PDH",
        "PDL",
        "Today's Open",
        "Today's Low",
        "Today's High",
    ]

    for column in numeric_columns:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df["Sector LTP"] = (
        df.groupby("Sector")["LTP"]
        .transform("mean")
    )

    df["Sector PDC"] = (
        df.groupby("Sector")["PDC"]
        .transform("mean")
    )

    up = (
        df["LTP"]
        .gt(df["PDC"])
        .fillna(False)
        .groupby(df["Sector"])
        .transform("sum")
    )

    down = (
        df["LTP"]
        .lt(df["PDC"])
        .fillna(False)
        .groupby(df["Sector"])
        .transform("sum")
    )

    df["Sector AD"] = (
        up
        / down.replace(
            0,
            float("nan"),
        )
    )

    df["Sector % from PDC"] = (
        (
            df["Sector LTP"]
            - df["Sector PDC"]
        )
        / df["Sector PDC"]
        * 100
    )

    # BUY CONDITION
    buy = (
        df["PDC"].gt(0)
        & df["PDH"].notna()
        & df["LTP"].notna()
        & df["Sector LTP"].notna()
        & df["Sector PDC"].notna()
    )

    buy &= (
        (
            (
                df["PDH"]
                - df["PDC"]
            )
            / df["PDC"]
            * 100
        )
        <= 1
    )

    buy &= (
        df["LTP"]
        .gt(df["PDH"])
    )

    buy &= (
        df["Sector LTP"]
        .gt(df["Sector PDC"])
    )

    buy &= (
        df["Today's Low"]
        .lt(df["PDH"])
    )

    buy &= (
        df["Today's High"]
        .gt(df["PDH"])
    )

    buy &= (
        df["Sector AD"]
        .gt(1)
    )

    # SELL CONDITION
    sell = (
        df["PDC"].gt(0)
        & df["PDL"].notna()
        & df["LTP"].notna()
        & df["Sector LTP"].notna()
        & df["Sector PDC"].notna()
    )

    sell &= (
        (
            (
                df["PDC"]
                - df["PDL"]
            )
            / df["PDC"]
            * 100
        )
        <= 1
    )

    sell &= (
        df["LTP"]
        .lt(df["PDL"])
    )

    sell &= (
        df["Sector LTP"]
        .lt(df["Sector PDC"])
    )

    sell &= (
        df["Today's High"]
        .gt(df["PDL"])
    )

    sell &= (
        df["Today's Low"]
        .lt(df["PDL"])
    )

    sell &= (
        df["Sector AD"]
        .lt(1)
    )

    df["Buy Alignment 🟢"] = (
        buy.fillna(False)
    )

    df["Sell Alignment 🔴"] = (
        sell.fillna(False)
    )

    return df


# ============================================================
# BUY / SELL TABLE
# ============================================================

def table(df, kind):
    level = (
        "PDH"
        if kind == "buy"
        else "PDL"
    )

    alignment = (
        "Buy Alignment 🟢"
        if kind == "buy"
        else "Sell Alignment 🔴"
    )

    columns = [
        "Rank",
        "Stock",
        "Sector",
        "LTP",
        "PDC",
        level,
        "Today's Open",
        "Today's Low",
        "Today's High",
        "Sector LTP",
        "Sector PDC",
        "Sector % from PDC",
        "Sector AD",
        "Distance %",
        alignment,
    ]

    if df.empty:
        return pd.DataFrame(
            columns=columns
        )

    selected = df[
        df[alignment].fillna(False)
    ].copy()

    if selected.empty:
        return pd.DataFrame(
            columns=columns
        )

    if kind == "buy":
        distance = (
            (
                selected["PDH"]
                - selected["PDC"]
            )
            / selected["PDC"]
            * 100
        )
    else:
        distance = (
            (
                selected["PDC"]
                - selected["PDL"]
            )
            / selected["PDC"]
            * 100
        )

    selected = (
        selected
        .assign(
            _distance=distance,
            **{
                "Distance %": distance
            },
        )
        .sort_values("_distance")
    )

    selected["Rank"] = range(
        1,
        len(selected) + 1,
    )

    return selected[columns]


# ============================================================
# APP UI
# ============================================================

st.title(
    "📈 Dhan Stock Scanner"
)

st.caption(
    "Nifty 500 universe | "
    "Historical PDH/PDL/PDC cached once daily | "
    "Live Dhan snapshot every 15 seconds"
)

now = datetime.now(IST)

# Change the cache key once per trading day at 09:00 IST.
# This refreshes historical values once and leaves live data independent.
if now.weekday() < 5 and now.time() >= dt_time(9, 0):
    refresh_key = now.date().isoformat()
else:
    refresh_key = "pre-open-" + now.date().isoformat()

opened = market_open(now)

st.info(
    f"India time: "
    f"{now:%Y-%m-%d %H:%M:%S IST} | "
    f"{'Market open' if opened else 'Market closed; historical values remain available.'}"
)


# ============================================================
# MAIN APP
# ============================================================

try:
    stocks = load_stocks()

    # Use the most recent completed weekday.
    # This prevents today's incomplete candle being used as PDC/PDH/PDL.
    end_date = now.date() - timedelta(days=1)
    while end_date.weekday() >= 5:
        end_date -= timedelta(days=1)

    start = (
        end_date
        - timedelta(days=30)
    ).isoformat()

    end = end_date.isoformat()

    # Stable tuple used by Streamlit cache.
    # Historical function will run once per cache period.
    stocks_key = tuple(
        sorted(
            int(value)
            for value in stocks[
                "security_id"
            ].tolist()
        )
    )

    # This is cached for 24 hours.
    # It will NOT run every 15 seconds.
    history = historical(
        stocks_key,
        start,
        end,
        refresh_key,
    )

    # This is the only Dhan request repeated
    # every 15 seconds.
    if opened:
        live_snapshot = dhan_snapshot(
            stocks
        )

        live = flatten(
            live_snapshot
        )
    else:
        live = {}

    data = calculate(
        build_frame(
            stocks,
            live,
            history,
        )
    )

    buys = table(
        data,
        "buy",
    )

    sells = table(
        data,
        "sell",
    )

    a, b, c = st.columns(3)

    a.metric(
        "Stocks scanned",
        len(data),
    )

    b.metric(
        "🟢 Buy",
        len(buys),
    )

    c.metric(
        "🔴 Sell",
        len(sells),
    )

    errors = data.loc[
        data["History Error"].notna(),
        [
            "Stock",
            "History Error",
        ],
    ]

    if not errors.empty:
        with st.expander(
            "Historical API details"
        ):
            st.dataframe(
                errors,
                use_container_width=True,
                hide_index=True,
            )

    if not opened:
        st.warning(
            "Live LTP/today OHLC are blank "
            "while NSE is closed. "
            "PDC/PDH/PDL are historical "
            "Dhan values."
        )

    st.subheader(
        "🟢 BUY WATCHLIST"
    )

    st.dataframe(
        buys,
        use_container_width=True,
        hide_index=True,
    )

    st.subheader(
        "🔴 SELL WATCHLIST"
    )

    st.dataframe(
        sells,
        use_container_width=True,
        hide_index=True,
    )

    with st.expander(
        "All scanned stocks"
    ):
        st.dataframe(
            data,
            use_container_width=True,
            hide_index=True,
        )

    st.caption(
        f"Last check: "
        f"{now:%Y-%m-%d %H:%M:%S IST} | "
        f"Refresh: {REFRESH_SECONDS}s"
    )

except Exception as exc:
    st.error(
        f"Scanner error: {exc}"
    )


# ============================================================
# REFRESH ONLY LIVE DATA
# ============================================================

try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(
        interval=REFRESH_SECONDS * 1000,
        key="dhan_live_refresh",
    )
except ImportError:
    time.sleep(REFRESH_SECONDS)
    st.rerun()
