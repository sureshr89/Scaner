import os
import time
from datetime import datetime, timedelta, time as dtime
from io import StringIO
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="Nifty 500 Stock Scanner",
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

QUOTE_TTL_SECONDS = 15
HISTORY_TTL_SECONDS = 3600
MAX_QUOTE_AGE_SECONDS = 45


# ============================================================
# SECRETS / DHAN AUTHENTICATION
# ============================================================

def secret(name: str) -> str:
    """
    Read Streamlit Cloud Secrets first, then environment variables.
    """
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""

    if not value:
        value = os.getenv(name, "")

    return str(value).strip()


def dhan_headers() -> dict:
    client_id = secret("DHAN_CLIENT_ID")
    access_token = secret("DHAN_ACCESS_TOKEN")

    if not client_id or not access_token:
        raise RuntimeError(
            "Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN. "
            "Add both values in Streamlit Secrets."
        )

    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": access_token,
        "client-id": client_id,
    }


def test_dhan_connection():
    """
    Test credentials using the Dhan profile endpoint.
    """
    response = requests.get(
        "https://api.dhan.co/v2/profile",
        headers=dhan_headers(),
        timeout=20,
    )

    if response.status_code == 200:
        return True, "Dhan connection successful."

    if response.status_code == 401:
        return False, (
            "Dhan returned 401 Unauthorized. "
            "Check DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN. "
            "Do not add 'Bearer' before the token."
        )

    return False, (
        f"Dhan profile error HTTP {response.status_code}: "
        f"{response.text[:500]}"
    )


# ============================================================
# REQUEST HELPER
# ============================================================

def request_with_retry(
    method,
    url,
    session=None,
    **kwargs,
):
    client = session or requests
    last_error = "unknown error"

    for attempt in range(3):
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

        if attempt < 2:
            time.sleep(1.5 * (2 ** attempt))

    raise RuntimeError(
        f"Request failed after retries: {last_error}"
    )


# ============================================================
# GENERAL HELPERS
# ============================================================

def column_name(frame: pd.DataFrame, names):
    lookup = {
        str(column).lower().strip().replace(" ", "_"): column
        for column in frame.columns
    }

    for name in names:
        key = str(name).lower().strip().replace(" ", "_")

        if key in lookup:
            return lookup[key]

    return None


def clean_symbol(value) -> str:
    return "".join(
        character
        for character in str(value).upper().strip()
        if character.isalnum()
    )


# ============================================================
# LOAD NIFTY 500 UNIVERSE AND DHAN SECURITY IDS
# ============================================================

@st.cache_data(ttl=86400, show_spinner=False)
def load_universe():
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/120 Safari/537.36"
            ),
            "Accept": "application/json,text/csv,*/*",
            "Referer": "https://www.nseindia.com/",
        }
    )

    frame = None
    errors = []

    # First try NSE API
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

    # Fallback to Nifty CSV
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
                    "Nifty CSV endpoint returned HTML."
                )

            frame = pd.read_csv(
                StringIO(response.text)
            )

        except Exception as exc:
            errors.append(f"Nifty CSV: {exc}")

    if frame is None or frame.empty:
        raise RuntimeError(
            "Unable to load Nifty 500 universe. "
            + " | ".join(errors)
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
            "No stock symbol column found. "
            f"Columns: {list(frame.columns)}"
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

    # Load Dhan master file
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

    sid_col = column_name(
        master,
        [
            "security_id",
            "sem_security_id",
            "sem_smst_security_id",
        ],
    )

    if not all(
        [
            segment_col,
            symbol_master_col,
            sid_col,
        ]
    ):
        raise RuntimeError(
            "Dhan master columns not recognised. "
            f"Columns: {list(master.columns)[:40]}"
        )

    dm = master.copy()

    exchange = (
        dm[segment_col]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    dm = dm[
        exchange.isin(
            {
                "NSE",
                "NSE_EQ",
                "NSE EQUITY",
                "NSE_EQ_CM",
            }
        )
    ].copy()

    dm["security_id"] = pd.to_numeric(
        dm[sid_col],
        errors="coerce",
    )

    dm = dm.dropna(
        subset=["security_id"]
    ).copy()

    dm["exact_key"] = (
        dm[symbol_master_col]
        .astype(str)
        .str.upper()
        .str.strip()
    )

    dm["key"] = dm[symbol_master_col].map(
        clean_symbol
    )

    exact = (
        dm.drop_duplicates("exact_key")
        [["exact_key", "security_id"]]
    )

    result = frame.merge(
        exact,
        on="exact_key",
        how="left",
    )

    fallback = (
        dm.drop_duplicates("key")
        .set_index("key")["security_id"]
    )

    missing = result["security_id"].isna()

    result.loc[missing, "security_id"] = (
        result.loc[missing, "key"].map(fallback)
    )

    result = result.dropna(
        subset=["security_id"]
    ).copy()

    result["security_id"] = (
        result["security_id"].astype(int)
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
            "Expected at least 400 for Nifty 500."
        )

    return result


# ============================================================
# LIVE QUOTES
# ============================================================

@st.cache_data(
    ttl=QUOTE_TTL_SECONDS,
    show_spinner=False,
)
def live_quotes(ids):
    clean_ids = sorted(
        {
            int(value)
            for value in ids
            if pd.notna(value)
        }
    )

    if not clean_ids:
        raise RuntimeError(
            "No Dhan security IDs found."
        )

    response = request_with_retry(
        "POST",
        QUOTE_URL,
        headers=dhan_headers(),
        json={
            "NSE_EQ": clean_ids,
        },
        timeout=60,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Dhan quote API returned 401 Unauthorized."
        )

    response.raise_for_status()

    payload = response.json()
    segments = payload.get("data") or {}

    if not isinstance(segments, dict):
        raise RuntimeError(
            "Unexpected Dhan quote response: "
            f"{str(payload)[:500]}"
        )

    output = {}

    for segment, items in segments.items():
        if not isinstance(items, dict):
            continue

        for sid, quote in items.items():
            quote = quote or {}
            ohlc = quote.get("ohlc") or {}

            output[
                (
                    str(segment),
                    str(sid),
                )
            ] = {
                "LTP": quote.get("last_price"),
                "Today's Open": ohlc.get("open"),
                "Today's Low": ohlc.get("low"),
                "Today's High": ohlc.get("high"),
            }

    if not output:
        raise RuntimeError(
            "Dhan returned no live quote data."
        )

    return output


# ============================================================
# PREVIOUS-DAY OHLC
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

    if response.status_code == 401:
        raise RuntimeError(
            "Historical API returned 401 Unauthorized."
        )

    if response.status_code == 403:
        raise RuntimeError(
            "Historical API returned 403 Forbidden. "
            "Dhan Data APIs may not be enabled."
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Historical API HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    body = response.json()

    if not isinstance(body, dict):
        raise RuntimeError(
            f"Unexpected historical response: {body}"
        )

    # Dhan may return candle arrays directly or under data.
    raw = body.get("data")

    if not isinstance(raw, dict):
        raw = body

    timestamps = (
        raw.get("timestamp")
        or raw.get("start_Time")
        or raw.get("startTime")
    )

    closes = raw.get("close")
    highs = raw.get("high")
    lows = raw.get("low")

    if not all(
        isinstance(value, list)
        for value in [
            timestamps,
            closes,
            highs,
            lows,
        ]
    ):
        raise RuntimeError(
            "Historical response does not contain candle arrays. "
            f"Response: {str(body)[:700]}"
        )

    length = min(
        len(timestamps),
        len(closes),
        len(highs),
        len(lows),
    )

    if length == 0:
        raise RuntimeError(
            f"No candles returned for security ID {security_id}."
        )

    candles = pd.DataFrame(
        {
            "timestamp": timestamps[:length],
            "close": closes[:length],
            "high": highs[:length],
            "low": lows[:length],
        }
    )

    for column in [
        "timestamp",
        "close",
        "high",
        "low",
    ]:
        candles[column] = pd.to_numeric(
            candles[column],
            errors="coerce",
        )

    candles = candles.dropna()

    if candles.empty:
        raise RuntimeError(
            f"No valid candles for security ID {security_id}."
        )

    # Detect seconds vs milliseconds
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

    # Use the latest completed trading day before today.
    completed = candles[
        candles["trade_date"] < today
    ].copy()

    if completed.empty:
        raise RuntimeError(
            f"No previous trading-day candle for "
            f"security ID {security_id}."
        )

    previous_date = completed["trade_date"].max()

    previous_day = completed[
        completed["trade_date"] == previous_date
    ]

    if previous_day.empty:
        raise RuntimeError(
            f"Previous-day candle unavailable for "
            f"security ID {security_id}."
        )

    candle = previous_day.iloc[-1]

    return {
        "PDC": float(candle["close"]),
        "PDH": float(candle["high"]),
        "PDL": float(candle["low"]),
    }


# ============================================================
# LOAD HISTORY
# ============================================================

@st.cache_data(
    ttl=HISTORY_TTL_SECONDS,
    show_spinner=False,
)
def load_history(
    ids,
    start_date,
    end_date,
):
    result = {}
    failures = []

    # Limited concurrency prevents the page from taking too long.
    with ThreadPoolExecutor(max_workers=5) as executor:
        jobs = {
            executor.submit(
                previous_ohlc,
                sid,
                start_date,
                end_date,
            ): int(sid)
            for sid in ids
        }

        for future in as_completed(jobs):
            sid = jobs[future]

            try:
                result[sid] = future.result()

            except Exception as exc:
                result[sid] = {
                    "PDC": None,
                    "PDH": None,
                    "PDL": None,
                }

                failures.append(
                    f"Security ID {sid}: "
                    f"{type(exc).__name__}: {exc}"
                )

    return result, failures


# ============================================================
# CALCULATIONS
# ============================================================

def calculate(
    data: pd.DataFrame,
    live_is_fresh: bool,
):
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
        if column not in data.columns:
            data[column] = pd.NA

        data[column] = pd.to_numeric(
            data[column],
            errors="coerce",
        )

    valid = (
        data["LTP"].notna()
        & data["PDC"].notna()
        & data["PDH"].notna()
        & data["PDL"].notna()
    )

    above_pdc = (
        valid
        & data["LTP"].gt(data["PDC"])
    )

    below_pdc = (
        valid
        & data["LTP"].lt(data["PDC"])
    )

    up_count = (
        above_pdc
        .groupby(data["Sector"])
        .transform("sum")
    )

    down_count = (
        below_pdc
        .groupby(data["Sector"])
        .transform("sum")
    )

    valid_count = (
        valid
        .groupby(data["Sector"])
        .transform("sum")
    )

    # Sector A/D ratio = advancing stocks / declining stocks.
    data["Sector A/D Ratio"] = (
        up_count
        / down_count.where(
            down_count.ne(0),
            1,
        )
    )

    data["Sector A/D Ratio"] = (
        data["Sector A/D Ratio"]
        .where(
            valid_count.gt(0),
            pd.NA,
        )
    )

    def sector_bias(value):
        if pd.isna(value):
            return "N/A"

        if value > 1:
            return "🟢 Bullish"

        if value < 1:
            return "🔴 Bearish"

        return "⚪ Neutral"

    data["Sector Bias"] = (
        data["Sector A/D Ratio"]
        .apply(sector_bias)
    )

    data["PDC to PDH %"] = (
        (
            data["PDH"] - data["PDC"]
        )
        / data["PDC"]
        * 100
    ).where(
        data["PDC"].gt(0)
    )

    data["PDC to PDL %"] = (
        (
            data["PDC"] - data["PDL"]
        )
        / data["PDC"]
        * 100
    ).where(
        data["PDC"].gt(0)
    )

    tight_up = data[
        "PDC to PDH %"
    ].between(
        0,
        0.15,
        inclusive="both",
    )

    tight_down = data[
        "PDC to PDL %"
    ].between(
        0,
        0.15,
        inclusive="both",
    )

    reliable_sector = valid_count.ge(5)

    data["Green Signal"] = (
        valid
        & live_is_fresh
        & reliable_sector
        & data["Today's High"].gt(data["PDH"])
        & data["Today's Low"].lt(data["PDH"])
        & data["LTP"].gt(data["PDH"])
        & data["Sector A/D Ratio"].gt(1)
        & tight_up
    )

    data["Red Signal"] = (
        valid
        & live_is_fresh
        & reliable_sector
        & data["Today's Low"].lt(data["PDL"])
        & data["Today's High"].gt(data["PDL"])
        & data["LTP"].lt(data["PDL"])
        & data["Sector A/D Ratio"].lt(1)
        & tight_down
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

    return data


# ============================================================
# MARKET HOURS
# ============================================================

def market_open(now):
    return (
        now.weekday() < 5
        and dtime(9, 15)
        <= now.time()
        <= dtime(15, 30)
    )


# ============================================================
# MAIN RENDER
# ============================================================

def render():
    now = datetime.now(IST)

    stocks = load_universe()

    ids = tuple(
        stocks["security_id"]
        .astype(int)
        .tolist()
    )

    st.info(
        f"Loaded {len(stocks)} Nifty 500 stocks."
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
                f"Live quote error: "
                f"{type(exc).__name__}: {exc}"
            )

    else:
        quote_error = (
            "Market is closed. Live signals are disabled."
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
            f"({age}s old)"
        )

    if quote_error:
        st.warning(quote_error)

    # --------------------------------------------------------
    # PREVIOUS DAY OHLC
    # --------------------------------------------------------

    load_ohlc = st.session_state.get(
        "load_previous_ohlc",
        True,
    )

    history = {}
    failures = []

    if load_ohlc:
        with st.spinner(
            "Loading previous-day OHLC for Nifty 500..."
        ):
            try:
                history, failures = load_history(
                    ids,
                    (
                        now.date()
                        - timedelta(days=15)
                    ).isoformat(),
                    now.date().isoformat(),
                )

            except Exception as exc:
                failures = [
                    f"History loader error: "
                    f"{type(exc).__name__}: {exc}"
                ]

        if failures:
            st.error(
                f"Previous-day OHLC failed for "
                f"{len(failures)} stocks."
            )

            with st.expander(
                "Show historical API errors"
            ):
                st.code(
                    "\n".join(
                        failures[:50]
                    )
                )

    else:
        st.info(
            "Previous-day OHLC is disabled."
        )

    # --------------------------------------------------------
    # BUILD TABLE
    # --------------------------------------------------------

    rows = []

    for record in stocks.to_dict("records"):
        sid = int(record["security_id"])

        quote = live.get(
            (
                "NSE_EQ",
                str(sid),
            ),
            {},
        )

        old = history.get(
            sid,
            {},
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

                "PDC": old.get("PDC"),
                "PDH": old.get("PDH"),
                "PDL": old.get("PDL"),
            }
        )

    data = pd.DataFrame(rows)

    data = calculate(
        data,
        live_is_fresh,
    )

    columns = [
        "Signal",
        "Stock",
        "Sector",
        "Sector A/D Ratio",
        "Sector Bias",
        "LTP",
        "Today's Open",
        "Today's Low",
        "Today's High",
        "PDC",
        "PDH",
        "PDL",
        "PDC to PDH %",
        "PDC to PDL %",
    ]

    display_data = (
        data.sort_values(
            [
                "Signal Priority",
                "Sector",
                "Stock",
            ],
            kind="stable",
        )
        .reindex(columns=columns)
    )

    st.metric(
        "Stocks scanned",
        len(display_data),
    )

    st.dataframe(
        display_data,
        use_container_width=True,
        hide_index=True,
        height=700,
    )

    st.caption(
        "Nifty 500 • Dhan live quotes • "
        "previous-day OHLC • sector breadth"
    )


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:
    st.subheader("Data controls")

    if st.button(
        "🔄 Clear cache and refresh",
        use_container_width=True,
    ):
        st.cache_data.clear()

        st.session_state.pop(
            "last_live_quotes",
            None,
        )

        st.session_state.pop(
            "quote_time",
            None,
        )

        st.rerun()

    if st.button(
        "🔐 Test Dhan connection",
        use_container_width=True,
    ):
        try:
            with st.spinner(
                "Testing Dhan credentials..."
            ):
                connected, message = (
                    test_dhan_connection()
                )

            if connected:
                st.success(message)
            else:
                st.error(message)

        except Exception as exc:
            st.error(
                f"Connection test error: "
                f"{type(exc).__name__}: {exc}"
            )

    st.checkbox(
        "Load previous-day OHLC",
        value=True,
        key="load_previous_ohlc",
        help=(
            "Loads PDC, PDH, PDL and sector A/D ratios. "
            "This makes historical requests for all stocks."
        ),
    )

    st.caption(
        "Use Streamlit Secrets named "
        "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN."
    )


# ============================================================
# APP HEADER AND RUN
# ============================================================

st.title(
    "📈 Nifty 500 Stock Scanner"
)

st.caption(
    "Nifty 500 • Dhan live quotes • "
    "previous-day OHLC • sector breadth"
)

try:
    render()

except Exception as exc:
    st.error(
        f"Scanner error: "
        f"{type(exc).__name__}: {exc}"
    )

    with st.expander(
        "Show full error"
    ):
        st.exception(exc)
