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
# STREAMLIT CONFIG
# ============================================================

st.set_page_config(
    page_title="Dhan Stock Scanner",
    page_icon="📈",
    layout="wide",
)

REFRESH_SECONDS = 15

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
# BASIC HELPERS
# ============================================================

def secret(name):
    """
    Read value from environment variables or Streamlit secrets.
    """
    try:
        return os.getenv(name) or st.secrets.get(name, "")
    except Exception:
        return os.getenv(name, "")


def headers():
    """
    Dhan API headers.
    """
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


def norm(value):
    """
    Normalize stock symbols for matching.
    """
    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(value).upper().strip(),
    )


def first_column(frame, names):
    """
    Find the first matching column name.
    """
    lookup = {
        str(column).lower().replace(" ", "_"): column
        for column in frame.columns
    }

    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]

    return None


def universe_session():
    """
    Session used for NSE and Dhan master downloads.
    """
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

@st.cache_data(
    ttl=86400,
    show_spinner=False,
)
def download_nifty500():
    """
    Download Nifty 500 stocks and map them to Dhan security IDs.
    Cached for one day.
    """

    session = universe_session()

    nse = None
    errors = []

    # --------------------------------------------------------
    # Try NSE API
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Fallback to Nifty CSV
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Identify symbol column
    # --------------------------------------------------------

    symbol_col = first_column(
        nse,
        [
            "symbol",
            "company_symbol",
        ],
    )

    if not symbol_col:
        raise RuntimeError(
            "No symbol column found in Nifty data: "
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

    # --------------------------------------------------------
    # Sector column
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Download Dhan master
    # --------------------------------------------------------

    master_response = session.get(
        DHAN_MASTER_URL,
        timeout=90,
    )

    master_response.raise_for_status()

    master = pd.read_csv(
        StringIO(master_response.text),
        low_memory=False,
    )

    # --------------------------------------------------------
    # Identify Dhan master columns
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Filter NSE Equity
    # --------------------------------------------------------

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
        dm.dropna(
            subset=["security_id"]
        )
        .drop_duplicates("join_key")
        [
            [
                "join_key",
                "security_id",
            ]
        ]
    )

    # --------------------------------------------------------
    # Merge Nifty stocks with Dhan IDs
    # --------------------------------------------------------

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
    """
    Load stocks from online sources.
    Use stocks.csv if online loading fails.
    """

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

        st.warning(
            "Nifty 500 online loading failed. "
            f"Using {len(fallback)} local stocks. "
            f"Details: {exc}"
        )

        return fallback


# ============================================================
# MARKET STATUS
# ============================================================

def market_open(now):
    """
    NSE market timing:
    Monday to Friday, 09:15 to 15:30 IST.
    """

    return (
        now.weekday() < 5
        and dt_time(9, 15)
        <= now.time()
        <= dt_time(15, 30)
    )


# ============================================================
# LIVE DHAN SNAPSHOT
# ============================================================

def dhan_snapshot(stocks):
    """
    Fetch live OHLC snapshot for all stock IDs.
    """

    security_ids = sorted(
        {
            int(value)
            for value in stocks.security_id
        }
    )

    response = requests.post(
        QUOTE_URL,
        headers=headers(),
        json={
            "NSE_EQ": security_ids
        },
        timeout=60,
    )

    response.raise_for_status()

    return response.json()


def flatten(snapshot):
    """
    Convert Dhan response into easy lookup format.
    """

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
                "ltp": quote.get("last_price"),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
            }

    return output


# ============================================================
# HISTORICAL DATA
# ============================================================

@st.cache_data(
    ttl=86400,
    show_spinner=False,
)
def historical_one(
    security_id,
    start,
    end,
):
    """
    Fetch previous historical candle.

    Cached for one day per stock/date range.
    """

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

        if not response.ok:
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:250]}"
                ),
            }

        body = response.json()

        if isinstance(body, dict):
            data = (
                body.get("data")
                or body
            )
        else:
            data = {}

        fields = [
            "timestamp",
            "close",
            "high",
            "low",
        ]

        if (
            not isinstance(data, dict)
            or any(
                field not in data
                for field in fields
            )
        ):
            return {
                "pdc": None,
                "pdh": None,
                "pdl": None,
                "error": (
                    "Missing historical candle fields"
                ),
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
            frame.dropna(
                subset=fields
            )
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
            }

        row = frame.iloc[-1]

        return {
            "pdc": float(row["close"]),
            "pdh": float(row["high"]),
            "pdl": float(row["low"]),
            "error": None,
        }

    except Exception as exc:

        return {
            "pdc": None,
            "pdh": None,
            "pdl": None,
            "error": str(exc),
        }


@st.cache_data(
    ttl=86400,
    show_spinner=False,
)
def historical(
    stocks,
    history_date,
):
    """
    Fetch historical data once per date.

    IMPORTANT:
    history_date is a date string, not datetime.now().
    Therefore the cache remains valid during the day.
    """

    start_date = (
        history_date
        - timedelta(days=30)
    ).isoformat()

    end_date = (
        history_date
        .isoformat()
    )

    history = {}

    stock_ids = list(
        stocks.security_id
    )

    total = len(stock_ids)

    progress = st.progress(
        0,
        text=(
            "Loading historical data... "
            f"0/{total}"
        ),
    )

    for index, security_id in enumerate(
        stock_ids,
        start=1,
    ):

        history[
            int(security_id)
        ] = historical_one(
            security_id,
            start_date,
            end_date,
        )

        # Small pause to reduce Dhan HTTP 429 risk.
        time.sleep(0.15)

        progress.progress(
            index / total,
            text=(
                "Loading historical data... "
                f"{index}/{total}"
            ),
        )

    progress.empty()

    return history


# ============================================================
# BUILD DATAFRAME
# ============================================================

def build_frame(
    stocks,
    live,
    history,
):
    """
    Combine stock list, live prices and historical data.
    """

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
                "LTP": live_data.get("ltp"),
                "PDC": historical_data.get("pdc"),
                "PDH": historical_data.get("pdh"),
                "PDL": historical_data.get("pdl"),
                "Today's Open": live_data.get("open"),
                "Today's Low": live_data.get("low"),
                "Today's High": live_data.get("high"),
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
    """
    Calculate sector strength and buy/sell conditions.
    """

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

    # Sector averages
    df["Sector LTP"] = (
        df.groupby("Sector")["LTP"]
        .transform("mean")
    )

    df["Sector PDC"] = (
        df.groupby("Sector")["PDC"]
        .transform("mean")
    )

    # Sector advance/decline
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

    # Sector percentage change
    df["Sector % from PDC"] = (
        (
            df["Sector LTP"]
            - df["Sector PDC"]
        )
        / df["Sector PDC"]
        * 100
    )

    # --------------------------------------------------------
    # BUY CONDITION
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # SELL CONDITION
    # --------------------------------------------------------

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

def table(
    df,
    kind,
):
    """
    Return sorted buy or sell watchlist.
    """

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
        selected.assign(
            _distance=distance
        )
        .sort_values("_distance")
    )

    selected["Rank"] = range(
        1,
        len(selected) + 1,
    )

    return selected[columns]


# ============================================================
# MAIN APP
# ============================================================

st.title(
    "📈 Dhan Stock Scanner"
)

st.caption(
    "Nifty 500 universe | "
    "Historical PDH/PDL/PDC once daily | "
    "Live Dhan snapshot every 15 seconds"
)

now = datetime.now(IST)

opened = market_open(now)

st.info(
    f"India time: {now:%Y-%m-%d %H:%M:%S IST} | "
    f"{'Market open' if opened else 'Market closed; historical values remain available.'}"
)


try:

    # --------------------------------------------------------
    # Load stocks
    # --------------------------------------------------------

    stocks = load_stocks()

    # --------------------------------------------------------
    # IMPORTANT CACHE FIX
    #
    # Pass date only, not full datetime.
    # This prevents historical reload every 15 seconds.
    # --------------------------------------------------------

    history = historical(
        stocks,
        now.date(),
    )

    # --------------------------------------------------------
    # Fetch live data only when market is open
    # --------------------------------------------------------

    if opened:
        live = flatten(
            dhan_snapshot(stocks)
        )
    else:
        live = {}

    # --------------------------------------------------------
    # Build and calculate
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Metrics
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Historical errors
    # --------------------------------------------------------

    if not data.empty:

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
                )

    # --------------------------------------------------------
    # Market closed warning
    # --------------------------------------------------------

    if not opened:

        st.warning(
            "Live LTP/today OHLC are blank while NSE is closed. "
            "PDC/PDH/PDL are historical Dhan values."
        )

    # --------------------------------------------------------
    # Buy watchlist
    # --------------------------------------------------------

    st.subheader(
        "🟢 BUY WATCHLIST"
    )

    st.dataframe(
        buys,
        use_container_width=True,
        hide_index=True,
    )

    # --------------------------------------------------------
    # Sell watchlist
    # --------------------------------------------------------

    st.subheader(
        "🔴 SELL WATCHLIST"
    )

    st.dataframe(
        sells,
        use_container_width=True,
        hide_index=True,
    )

    # --------------------------------------------------------
    # All stocks
    # --------------------------------------------------------

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
# AUTO REFRESH
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
