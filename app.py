import os
import time
from datetime import datetime, timedelta, time as dtime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Nifty 500 Scanner",
    page_icon="📈",
    layout="wide",
)

IST = ZoneInfo("Asia/Kolkata")

INDEX_NAME = "NIFTY 500"

NSE_URL = (
    "https://www.nseindia.com/api/equity-stockIndices"
    "?index=NIFTY%20500"
)

CSV_URL = (
    "https://www.niftyindices.com/IndexConstituent/"
    "ind_nifty500list.csv"
)

MASTER_URL = (
    "https://images.dhan.co/api-data/api-scrip-master.csv"
)

QUOTE_URL = (
    "https://api.dhan.co/v2/marketfeed/ohlc"
)

HISTORY_URL = (
    "https://api.dhan.co/v2/charts/historical"
)


# ============================================================
# SETTINGS
# ============================================================

QUOTE_TTL_SECONDS = 15
MAX_QUOTE_AGE_SECONDS = 45

# Dhan rate-limit protection
HISTORY_DELAY_SECONDS = 1.25
HISTORY_RETRIES = 3

# Historical update time
HISTORY_UPDATE_HOUR = 9
HISTORY_UPDATE_MINUTE = 0

# Signal settings
MIN_SECTOR_STOCKS = 5

BUY_AD_RATIO = 1.50
SELL_AD_RATIO = 0.67

# Maximum previous-day range allowed for signal
MAX_PREVIOUS_DAY_RANGE_PERCENT = 8.0


# ============================================================
# HELPERS
# ============================================================

def secret(name):
    try:
        return os.getenv(name) or st.secrets.get(name, "")
    except Exception:
        return os.getenv(name, "")


def dhan_headers():
    client_id = secret("DHAN_CLIENT_ID")
    access_token = secret("DHAN_ACCESS_TOKEN")

    if not client_id or not access_token:
        raise RuntimeError(
            "Configure DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
            "in Streamlit Secrets."
        )

    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def request_with_retry(
    method,
    url,
    session=None,
    retries=3,
    **kwargs,
):
    client = session or requests
    last_error = "unknown error"

    for attempt in range(retries):
        try:
            response = client.request(
                method,
                url,
                **kwargs,
            )

            if response.status_code not in {
                429,
                500,
                502,
                503,
                504,
            }:
                return response

            last_error = (
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        except requests.RequestException as exc:
            last_error = str(exc)

        if attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))

    raise RuntimeError(
        f"Request failed after retries: {last_error}"
    )


def column_name(frame, names):
    lookup = {
        str(column).lower().replace(" ", "_"): column
        for column in frame.columns
    }

    for name in names:
        key = name.lower().replace(" ", "_")

        if key in lookup:
            return lookup[key]

    return None


def clean_symbol(value):
    return "".join(
        character
        for character in str(value).upper().strip()
        if character.isalnum()
    )


def empty_history():
    return {
        "PDC": None,
        "PDH": None,
        "PDL": None,
    }


# ============================================================
# LOAD NIFTY 500 UNIVERSE AND DHAN SECURITY IDS
# ============================================================

@st.cache_data(
    ttl=86400,
    show_spinner=False,
)
def load_universe():
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/csv,*/*",
            "Referer": "https://www.nseindia.com/",
        }
    )

    frame = None
    errors = []

    # First attempt: NSE API
    try:
        request_with_retry(
            "GET",
            "https://www.nseindia.com/",
            session=session,
            timeout=20,
        )

        response = request_with_retry(
            "GET",
            NSE_URL,
            session=session,
            timeout=30,
        )

        response.raise_for_status()

        frame = pd.DataFrame(
            response.json().get("data", [])
        )

    except Exception as exc:
        errors.append(f"NSE API: {exc}")

    # Second attempt: Nifty CSV
    if frame is None or frame.empty:
        try:
            response = request_with_retry(
                "GET",
                CSV_URL,
                session=session,
                timeout=30,
            )

            response.raise_for_status()

            if "<html" in response.text[:500].lower():
                raise RuntimeError(
                    "CSV endpoint returned HTML instead of CSV."
                )

            frame = pd.read_csv(
                StringIO(response.text)
            )

        except Exception as exc:
            errors.append(f"Nifty CSV: {exc}")

    if frame is None or frame.empty:
        raise RuntimeError(
            f"Unable to load {INDEX_NAME} universe: "
            + "; ".join(errors)
        )

    symbol_col = column_name(
        frame,
        [
            "symbol",
            "company_symbol",
        ],
    )

    sector_col = column_name(
        frame,
        [
            "industry",
            "sector",
            "industry_info",
        ],
    )

    if not symbol_col:
        raise RuntimeError(
            "Universe has no symbol column. "
            f"Available columns: {list(frame.columns)}"
        )

    frame["stock"] = (
        frame[symbol_col]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    frame = frame[
        ~frame["stock"].str.contains(
            "NIFTY",
            na=False,
        )
    ].copy()

    if sector_col:
        frame["sector"] = (
            frame[sector_col]
            .astype(str)
            .str.strip()
        )
    else:
        frame["sector"] = "UNKNOWN"

    frame["exact_key"] = frame["stock"]
    frame["key"] = frame["stock"].map(clean_symbol)

    # Load Dhan master
    response = request_with_retry(
        "GET",
        MASTER_URL,
        session=session,
        timeout=90,
    )

    response.raise_for_status()

    master = pd.read_csv(
        StringIO(response.text),
        low_memory=False,
    )

    segment_col = column_name(
        master,
        [
            "exchange_segment",
            "exchange_segment_name",
            "exchange_segment_code",
            "sem_exm_exch_id",
        ],
    )

    symbol_master_col = column_name(
        master,
        [
            "symbol",
            "trading_symbol",
            "sem_trading_symbol",
        ],
    )

    security_id_col = column_name(
        master,
        [
            "security_id",
            "sem_security_id",
            "sem_smst_security_id",
        ],
    )

    market_col = column_name(
        master,
        [
            "sem_segment",
            "segment",
        ],
    )

    if not all(
        [
            segment_col,
            symbol_master_col,
            security_id_col,
        ]
    ):
        raise RuntimeError(
            "Dhan master columns not recognised. "
            f"Available columns: {list(master.columns)[:40]}"
        )

    dhan_master = master.copy()

    exchange = (
        dhan_master[segment_col]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    dhan_master = dhan_master[
        exchange.isin(
            {
                "NSE",
                "NSE_EQ",
                "NSE EQUITY",
                "NSE_EQ_CM",
            }
        )
    ].copy()

    if market_col:
        market = (
            dhan_master[market_col]
            .astype(str)
            .str.upper()
            .str.strip()
        )

        dhan_master = dhan_master[
            market.isin(
                {
                    "E",
                    "EQUITY",
                    "NSE_EQ",
                    "NSE EQUITY",
                    "NA",
                    "NAN",
                    "NSE_EQ_CM",
                }
            )
        ].copy()

    dhan_master["security_id"] = pd.to_numeric(
        dhan_master[security_id_col],
        errors="coerce",
    )

    dhan_master = dhan_master.dropna(
        subset=["security_id"]
    ).copy()

    dhan_master["exact_key"] = (
        dhan_master[symbol_master_col]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    dhan_master["key"] = (
        dhan_master[symbol_master_col]
        .map(clean_symbol)
    )

    exact_map = (
        dhan_master
        .drop_duplicates("exact_key")
        [["exact_key", "security_id"]]
    )

    result = frame[
        [
            "stock",
            "sector",
            "exact_key",
            "key",
        ]
    ].merge(
        exact_map,
        on="exact_key",
        how="left",
    )

    fallback_map = (
        dhan_master
        .drop_duplicates("key")
        .set_index("key")["security_id"]
    )

    missing = result["security_id"].isna()

    result.loc[missing, "security_id"] = (
        result.loc[missing, "key"]
        .map(fallback_map)
    )

    result = result.dropna(
        subset=["security_id"]
    ).copy()

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
            f"Only {len(result)} stocks mapped. "
            "Expected at least 400 Nifty 500 stocks."
        )

    return result


# ============================================================
# DHAN LIVE QUOTES
# ============================================================

@st.cache_data(
    ttl=QUOTE_TTL_SECONDS,
    show_spinner=False,
)
def live_quotes(ids):
    response = request_with_retry(
        "POST",
        QUOTE_URL,
        headers=dhan_headers(),
        json={
            "NSE_EQ": sorted(
                {
                    int(value)
                    for value in ids
                }
            )
        },
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()
    output = {}

    for segment, items in (
        payload.get("data") or {}
    ).items():
        for security_id, quote in (
            items or {}
        ).items():

            quote = quote or {}
            ohlc = quote.get("ohlc") or {}

            output[
                (
                    segment,
                    str(security_id),
                )
            ] = {
                "LTP": quote.get("last_price"),
                "Today's Open": ohlc.get("open"),
                "Today's Low": ohlc.get("low"),
                "Today's High": ohlc.get("high"),
            }

    if not output:
        raise RuntimeError(
            f"Dhan returned no quote data: {payload}"
        )

    return output


# ============================================================
# HISTORICAL PREVIOUS-DAY OHLC
# ============================================================

def previous_ohlc(
    security_id,
    start_date,
    end_date,
):
    payload = {
        "securityId": str(int(security_id)),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "expiryCode": 0,
        "oi": False,
        "fromDate": start_date,
        "toDate": end_date,
    }

    response = request_with_retry(
        "POST",
        HISTORY_URL,
        headers=dhan_headers(),
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    raw = response.json().get("data") or {}

    required_fields = [
        "timestamp",
        "close",
        "high",
        "low",
    ]

    if (
        not isinstance(raw, dict)
        or not all(
            field in raw
            for field in required_fields
        )
    ):
        return empty_history()

    candles = pd.DataFrame(
        {
            field: pd.to_numeric(
                raw[field],
                errors="coerce",
            )
            for field in required_fields
        }
    )

    candles = (
        candles
        .dropna()
        .sort_values("timestamp")
    )

    if candles.empty:
        return empty_history()

    timestamp_unit = (
        "ms"
        if candles["timestamp"].abs().max()
        > 10_000_000_000
        else "s"
    )

    candles["trade_date"] = (
        pd.to_datetime(
            candles["timestamp"],
            unit=timestamp_unit,
            utc=True,
            errors="coerce",
        )
        .dt.tz_convert(IST)
        .dt.date
    )

    today = datetime.now(IST).date()

    completed_days = candles[
        candles["trade_date"] < today
    ]

    if completed_days.empty:
        return empty_history()

    previous_day = completed_days.iloc[-1]

    return {
        "PDC": float(previous_day["close"]),
        "PDH": float(previous_day["high"]),
        "PDL": float(previous_day["low"]),
    }


def load_history_sequential(
    ids,
    start_date,
    end_date,
):
    """
    Sequential historical calls avoid Dhan HTTP 429 errors.
    """
    result = {}
    failures = []

    progress = st.progress(
        0,
        text="Loading historical OHLC...",
    )

    total = len(ids)

    for index, security_id in enumerate(ids, start=1):
        try:
            result[int(security_id)] = previous_ohlc(
                security_id,
                start_date,
                end_date,
            )

        except Exception as exc:
            result[int(security_id)] = empty_history()

            failures.append(
                f"{security_id}: {exc}"
            )

        progress.progress(
            index / total,
            text=(
                f"Historical OHLC: "
                f"{index}/{total}"
            ),
        )

        # Important: wait between calls
        if index < total:
            time.sleep(HISTORY_DELAY_SECONDS)

    progress.empty()

    return result, failures


# ============================================================
# DAILY HISTORICAL UPDATE CONTROL
# ============================================================

def history_update_due(now):
    """
    Runs once per weekday at or after 09:00 IST.

    It does not include NSE holidays.
    It excludes Saturday and Sunday.
    """
    if now.weekday() >= 5:
        return False

    return now.time() >= dtime(
        HISTORY_UPDATE_HOUR,
        HISTORY_UPDATE_MINUTE,
    )


def get_history_for_today(
    ids,
    now,
):
    today_key = now.date().isoformat()

    current_key = st.session_state.get(
        "history_date_key"
    )

    current_history = st.session_state.get(
        "history_data"
    )

    current_failures = st.session_state.get(
        "history_failures",
        [],
    )

    manual_refresh = st.session_state.pop(
        "manual_history_refresh",
        False,
    )

    due = history_update_due(now)

    needs_update = (
        current_history is None
        or manual_refresh
        or (
            due
            and current_key != today_key
        )
    )

    if not needs_update:
        return (
            current_history,
            current_failures,
        )

    start_date = (
        now.date() - timedelta(days=30)
    ).isoformat()

    end_date = now.date().isoformat()

    history, failures = load_history_sequential(
        ids,
        start_date,
        end_date,
    )

    st.session_state["history_data"] = history
    st.session_state["history_failures"] = failures
    st.session_state["history_date_key"] = today_key

    return history, failures


# ============================================================
# MARKET STATUS
# ============================================================

def market_open(now):
    return (
        now.weekday() < 5
        and dtime(9, 15)
        <= now.time()
        <= dtime(15, 30)
    )


# ============================================================
# CALCULATIONS AND SIGNALS
# ============================================================

def calculate_signals(
    data,
    live_is_fresh,
):
    numeric_columns = [
        "LTP",
        "PDC",
        "PDH",
        "PDL",
        "Today's Low",
        "Today's High",
    ]

    for column in numeric_columns:
        if column not in data.columns:
            data[column] = pd.NA

        data[column] = pd.to_numeric(
            data[column],
            errors="coerce",
        )

    # --------------------------------------------------------
    # VALID DATA
    # --------------------------------------------------------

    valid_ohlc = (
        data["LTP"].notna()
        & data["PDC"].notna()
        & data["PDH"].notna()
        & data["PDL"].notna()
    )

    # --------------------------------------------------------
    # SECTOR ADVANCE / DECLINE
    # --------------------------------------------------------

    data["Advance"] = (
        valid_ohlc
        & data["LTP"].gt(data["PDC"])
    )

    data["Decline"] = (
        valid_ohlc
        & data["LTP"].lt(data["PDC"])
    )

    data["Unchanged"] = (
        valid_ohlc
        & data["LTP"].eq(data["PDC"])
    )

    sector_advances = (
        data["Advance"]
        .groupby(data["Sector"])
        .transform("sum")
    )

    sector_declines = (
        data["Decline"]
        .groupby(data["Sector"])
        .transform("sum")
    )

    sector_valid_count = (
        valid_ohlc
        .groupby(data["Sector"])
        .transform("sum")
    )

    # Correct A/D calculation:
    #
    # A/D = advancing stocks / declining stocks
    #
    # If declines are zero and advances exist, ratio is infinity.
    # If advances and declines are both zero, ratio is NaN.
    data["Sector A/D Ratio"] = (
        sector_advances
        / sector_declines.replace(
            0,
            float("nan"),
        )
    )

    both_zero = (
        sector_advances.eq(0)
        & sector_declines.eq(0)
    )

    data.loc[
        both_zero,
        "Sector A/D Ratio",
    ] = pd.NA

    data["Sector Advances"] = (
        sector_advances.astype("Int64")
    )

    data["Sector Declines"] = (
        sector_declines.astype("Int64")
    )

    data["Sector Valid Stocks"] = (
        sector_valid_count.astype("Int64")
    )

    # --------------------------------------------------------
    # SECTOR BIAS
    # --------------------------------------------------------

    data["Sector Bias"] = "N/A"

    data.loc[
        data["Sector A/D Ratio"].ge(BUY_AD_RATIO),
        "Sector Bias",
    ] = "🟢 Bullish"

    data.loc[
        data["Sector A/D Ratio"].le(SELL_AD_RATIO),
        "Sector Bias",
    ] = "🔴 Bearish"

    middle_bias = (
        data["Sector A/D Ratio"].gt(SELL_AD_RATIO)
        & data["Sector A/D Ratio"].lt(BUY_AD_RATIO)
    )

    data.loc[
        middle_bias,
        "Sector Bias",
    ] = "⚪ Neutral"

    # --------------------------------------------------------
    # PREVIOUS DAY RANGE
    # --------------------------------------------------------

    data["Previous Day Range %"] = (
        (
            data["PDH"] - data["PDL"]
        )
        / data["PDC"].where(
            data["PDC"].gt(0)
        )
        * 100
    )

    range_is_reasonable = (
        data["Previous Day Range %"]
        <= MAX_PREVIOUS_DAY_RANGE_PERCENT
    )

    # --------------------------------------------------------
    # BREAKOUT CONDITIONS
    # --------------------------------------------------------

    # BUY:
    # Current LTP is above previous-day high.
    # Today's high crossed above previous-day high.
    # Today's low is at or below previous-day high,
    # meaning the breakout level was touched/crossed.
    #
    # This replaces the incorrect condition:
    # Today's Low < PDH without checking the actual breakout.

    buy_breakout = (
        data["LTP"].gt(data["PDH"])
        & data["Today's High"].gt(data["PDH"])
        & data["Today's Low"].le(data["PDH"])
    )

    # SELL:
    # Current LTP is below previous-day low.
    # Today's low crossed below previous-day low.
    # Today's high is at or above previous-day low.

    sell_breakout = (
        data["LTP"].lt(data["PDL"])
        & data["Today's Low"].lt(data["PDL"])
        & data["Today's High"].ge(data["PDL"])
    )

    reliable_sector = (
        sector_valid_count
        >= MIN_SECTOR_STOCKS
    )

    bullish_sector = (
        data["Sector A/D Ratio"]
        >= BUY_AD_RATIO
    )

    bearish_sector = (
        data["Sector A/D Ratio"]
        <= SELL_AD_RATIO
    )

    # --------------------------------------------------------
    # FINAL SIGNALS
    # --------------------------------------------------------

    data["Green Signal"] = (
        valid_ohlc
        & live_is_fresh
        & reliable_sector
        & range_is_reasonable
        & buy_breakout
        & bullish_sector
    )

    data["Red Signal"] = (
        valid_ohlc
        & live_is_fresh
        & reliable_sector
        & range_is_reasonable
        & sell_breakout
        & bearish_sector
    )

    data["Signal"] = ""

    data.loc[
        data["Green Signal"],
        "Signal",
    ] = "🟢 BUY"

    data.loc[
        data["Red Signal"],
        "Signal",
    ] = "🔴 SELL"

    data["Signal Priority"] = (
        data["Signal"]
        .map(
            {
                "🟢 BUY": 0,
                "🔴 SELL": 1,
                "": 2,
            }
        )
        .fillna(3)
    )

    # --------------------------------------------------------
    # EXTRA DIAGNOSTIC COLUMNS
    # --------------------------------------------------------

    data["Distance From PDH %"] = (
        (
            data["LTP"] - data["PDH"]
        )
        / data["PDH"].where(
            data["PDH"].gt(0)
        )
        * 100
    )

    data["Distance From PDL %"] = (
        (
            data["LTP"] - data["PDL"]
        )
        / data["PDL"].where(
            data["PDL"].gt(0)
        )
        * 100
    )

    return data


# ============================================================
# MAIN RENDER FUNCTION
# ============================================================

def render():
    now = datetime.now(IST)

    stocks = load_universe()

    ids = tuple(
        stocks["security_id"]
        .astype(int)
    )

    # Sidebar controls
    st.sidebar.header("Scanner Settings")

    st.sidebar.write(
        f"Historical update: "
        f"{HISTORY_UPDATE_HOUR:02d}:"
        f"{HISTORY_UPDATE_MINUTE:02d} IST"
    )

    st.sidebar.write(
        f"History delay: "
        f"{HISTORY_DELAY_SECONDS:.2f} seconds"
    )

    st.sidebar.write(
        f"Minimum sector stocks: "
        f"{MIN_SECTOR_STOCKS}"
    )

    st.sidebar.write(
        f"BUY A/D ratio: "
        f"{BUY_AD_RATIO:.2f}"
    )

    st.sidebar.write(
        f"SELL A/D ratio: "
        f"{SELL_AD_RATIO:.2f}"
    )

    if st.sidebar.button(
        "📚 Refresh historical OHLC now"
    ):
        st.session_state[
            "manual_history_refresh"
        ] = True

        st.rerun()

    history, history_failures = (
        get_history_for_today(
            ids,
            now,
        )
    )

    history_date_key = st.session_state.get(
        "history_date_key"
    )

    if history_date_key:
        st.caption(
            f"Historical OHLC snapshot: "
            f"{history_date_key}"
        )

    if history_failures:
        with st.expander(
            f"Historical data warnings "
            f"({len(history_failures)})"
        ):
            st.code(
                "\n".join(
                    history_failures[:100]
                )
            )

    # --------------------------------------------------------
    # LIVE QUOTES
    # --------------------------------------------------------

    quote_error = None
    live_is_fresh = False

    if market_open(now):
        try:
            quotes = live_quotes(ids)

            st.session_state[
                "last_live_quotes"
            ] = quotes

            st.session_state[
                "quote_time"
            ] = now

        except Exception as exc:
            quote_error = (
                f"Live quote error: {exc}"
            )

    else:
        quote_error = (
            "Market is closed. "
            "Live signals are disabled."
        )

    live = st.session_state.get(
        "last_live_quotes",
        {},
    )

    quote_time = st.session_state.get(
        "quote_time"
    )

    if quote_time:
        age = max(
            0,
            int(
                (
                    now - quote_time
                ).total_seconds()
            ),
        )

        live_is_fresh = (
            age <= MAX_QUOTE_AGE_SECONDS
            and market_open(now)
        )

        status = (
            "🟢 Fresh"
            if live_is_fresh
            else "⚠️ Stale"
        )

        st.caption(
            f"{status} LTP snapshot: "
            f"{quote_time.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"({age}s old) • refresh target: 15s"
        )

    if quote_error:
        st.warning(quote_error)

    # --------------------------------------------------------
    # BUILD DATAFRAME
    # --------------------------------------------------------

    rows = []

    for record in stocks.to_dict("records"):
        security_id = int(
            record["security_id"]
        )

        quote = live.get(
            (
                "NSE_EQ",
                str(security_id),
            ),
            {},
        )

        previous = history.get(
            security_id,
            empty_history(),
        )

        rows.append(
            {
                "Stock": record["stock"],
                "Sector": record["sector"],
                "LTP": quote.get("LTP"),
                "Today's Open": quote.get(
                    "Today's Open"
                ),
                "Today's Low": quote.get(
                    "Today's Low"
                ),
                "Today's High": quote.get(
                    "Today's High"
                ),
                "PDC": previous.get("PDC"),
                "PDH": previous.get("PDH"),
                "PDL": previous.get("PDL"),
            }
        )

    data = pd.DataFrame(rows)

    data = calculate_signals(
        data,
        live_is_fresh,
    )

    # --------------------------------------------------------
    # METRICS
    # --------------------------------------------------------

    buy_count = int(
        (data["Signal"] == "🟢 BUY").sum()
    )

    sell_count = int(
        (data["Signal"] == "🔴 SELL").sum()
    )

    valid_count = int(
        data[
            [
                "LTP",
                "PDC",
                "PDH",
                "PDL",
            ]
        ]
        .notna()
        .all(axis=1)
        .sum()
    )

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Stocks scanned",
        len(data),
    )

    col2.metric(
        "Valid OHLC",
        valid_count,
    )

    col3.metric(
        "BUY signals",
        buy_count,
    )

    col4.metric(
        "SELL signals",
        sell_count,
    )

    # --------------------------------------------------------
    # DISPLAY
    # --------------------------------------------------------

    columns = [
        "Signal",
        "Stock",
        "Sector",
        "Sector A/D Ratio",
        "Sector Bias",
        "Sector Advances",
        "Sector Declines",
        "Sector Valid Stocks",
        "LTP",
        "Today's Open",
        "Today's Low",
        "Today's High",
        "PDC",
        "PDH",
        "PDL",
        "Previous Day Range %",
        "Distance From PDH %",
        "Distance From PDL %",
    ]

    display_data = (
        data
        .sort_values(
            [
                "Signal Priority",
                "Sector",
                "Stock",
            ],
            kind="stable",
        )
        .reindex(columns=columns)
    )

    st.dataframe(
        display_data,
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "Historical OHLC updates once per weekday "
        "at or after 09:00 IST. "
        "Live LTP refreshes every 15 seconds "
        "during market hours. "
        "Signals require fresh quotes, valid previous-day "
        "OHLC, a reliable sector, and a confirmed breakout."
    )


# ============================================================
# APP ENTRY
# ============================================================

st.title(
    "📈 Nifty 500 Stock Scanner"
)

st.caption(
    "Nifty 500 • Dhan live quotes • "
    "previous-day OHLC • corrected sector A/D ratio • "
    "breakout signals"
)

try:
    if hasattr(st, "fragment"):
        st.fragment(
            run_every="15s"
        )(render)()
    else:
        render()

except Exception as exc:
    st.error(
        f"Scanner error: {exc}"
    )
