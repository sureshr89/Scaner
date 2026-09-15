import os
import time
from datetime import datetime, timedelta, time as dtime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st


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

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"
HISTORY_URL = "https://api.dhan.co/v2/charts/historical"

QUOTE_TTL_SECONDS = 15
MAX_QUOTE_AGE_SECONDS = 45


def secret(name):
    try:
        return os.getenv(name) or st.secrets.get(name, "")
    except Exception:
        return os.getenv(name, "")


def dhan_headers():
    client = secret("DHAN_CLIENT_ID")
    token = secret("DHAN_ACCESS_TOKEN")

    if not client or not token:
        raise RuntimeError(
            "Configure DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
            "in Streamlit Secrets."
        )

    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client,
    }


def test_dhan_connection():
    """Check whether the current Dhan credentials are accepted."""
    response = requests.get(
        "https://api.dhan.co/v2/profile",
        headers=dhan_headers(),
        timeout=20,
    )

    if response.status_code == 200:
        return True, "Dhan connection successful."

    if response.status_code == 401:
        return False, (
            "Dhan returned 401 Unauthorized. Check the client ID and "
            "access token. Do not add 'Bearer' before the token."
        )

    return False, (
        f"Dhan profile failed with HTTP {response.status_code}: "
        f"{response.text[:300]}"
    )


def request_with_retry(method, url, session=None, **kwargs):
    client = session or requests
    last_error = "unknown error"

    for attempt in range(3):
        try:
            response = client.request(method, url, **kwargs)

            if response.status_code not in {
                429,
                500,
                502,
                503,
                504,
            }:
                return response

            last_error = f"HTTP {response.status_code}"

        except requests.RequestException as exc:
            last_error = str(exc)

        if attempt < 2:
            time.sleep(1.5 * (2**attempt))

    raise RuntimeError(
        f"Request failed after retries: {last_error}"
    )


def column_name(frame, names):
    lookup = {
        str(column).lower().replace(" ", "_"): column
        for column in frame.columns
    }

    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]

    return None


def clean_symbol(value):
    return "".join(
        character
        for character in str(value).upper().strip()
        if character.isalnum()
    )


@st.cache_data(ttl=86400, show_spinner=False)
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
        errors.append(f"NSE: {exc}")

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
                    "CSV endpoint returned HTML"
                )

            frame = pd.read_csv(
                StringIO(response.text)
            )

        except Exception as exc:
            errors.append(f"CSV: {exc}")

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
            f"Universe has no symbol column: "
            f"{list(frame.columns)}"
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
            sid_col,
        ]
    ):
        raise RuntimeError(
            "Dhan master columns not recognised: "
            f"{list(master.columns)[:30]}"
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

    if market_col:
        market = (
            dm[market_col]
            .astype(str)
            .str.upper()
            .str.strip()
        )

        dm = dm[
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

    exact = dm.drop_duplicates(
        "exact_key"
    )[["exact_key", "security_id"]]

    result = frame[
        [
            "stock",
            "sector",
            "exact_key",
            "key",
        ]
    ].merge(
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
            f"Only {len(result)} stocks mapped; "
            "expected at least 400 for Nifty 500"
        )

    return result


@st.cache_data(
    ttl=QUOTE_TTL_SECONDS,
    show_spinner=False,
)
def live_quotes(ids):
    """Fetch live NSE equity quotes from Dhan."""
    clean_ids = sorted({int(value) for value in ids if pd.notna(value)})

    if not clean_ids:
        raise RuntimeError("No Dhan security IDs were found.")

    response = request_with_retry(
        "POST",
        QUOTE_URL,
        headers=dhan_headers(),
        json={"NSE_EQ": clean_ids},
        timeout=60,
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Dhan returned 401 Unauthorized. Check DHAN_CLIENT_ID and "
            "DHAN_ACCESS_TOKEN in Streamlit Secrets."
        )

    response.raise_for_status()

    payload = response.json()
    segments = payload.get("data") or {}

    if not isinstance(segments, dict):
        raise RuntimeError(
            f"Unexpected Dhan quote response: {str(payload)[:500]}"
        )

    output = {}

    for segment, items in segments.items():
        if not isinstance(items, dict):
            continue

        for sid, quote in items.items():
            quote = quote or {}
            ohlc = quote.get("ohlc") or {}

            output[(str(segment), str(sid))] = {
                "LTP": quote.get("last_price"),
                "Today's Open": ohlc.get("open"),
                "Today's Low": ohlc.get("low"),
                "Today's High": ohlc.get("high"),
            }

    if not output:
        raise RuntimeError(
            f"Dhan returned no quote data: {str(payload)[:500]}"
        )

    return output


def previous_ohlc(sid, start, end):
    payload = {
        "securityId": str(int(sid)),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "expiryCode": 0,
        "oi": False,
        "fromDate": start,
        "toDate": end,
    }

    response = request_with_retry(
        "POST",
        HISTORY_URL,
        headers=dhan_headers(),
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    payload_response = response.json()
    raw = payload_response.get("data") or {}

    fields = [
        "timestamp",
        "close",
        "high",
        "low",
    ]

    if not isinstance(raw, dict):
        raise RuntimeError(
            f"Historical response data is not an object: "
            f"{str(payload_response)[:500]}"
        )

    missing_fields = [
        field for field in fields if field not in raw
    ]

    if missing_fields:
        raise RuntimeError(
            f"Historical response missing {missing_fields}: "
            f"{str(payload_response)[:500]}"
        )

    candles = pd.DataFrame(
        {
            field: pd.to_numeric(
                raw[field],
                errors="coerce",
            )
            for field in fields
        }
    ).dropna().sort_values("timestamp")

    if candles.empty:
        return {
            "PDC": None,
            "PDH": None,
            "PDL": None,
        }

    unit = (
        "ms"
        if candles["timestamp"].abs().max()
        > 10_000_000_000
        else "s"
    )

    candles["trade_date"] = (
        pd.to_datetime(
            candles["timestamp"],
            unit=unit,
            utc=True,
            errors="coerce",
        )
        .dt.tz_convert(IST)
        .dt.date
    )

    today = datetime.now(IST).date()

    completed = candles[
        candles["trade_date"] < today
    ]

    if completed.empty:
        return {
            "PDC": None,
            "PDH": None,
            "PDL": None,
        }

    candle = completed.iloc[-1]

    return {
        "PDC": float(candle["close"]),
        "PDH": float(candle["high"]),
        "PDL": float(candle["low"]),
    }


@st.cache_data(ttl=86400, show_spinner=False)
def load_history(ids, start, end):
    result = {}
    failures = []

    for sid in ids:
        try:
            result[int(sid)] = previous_ohlc(
                sid,
                start,
                end,
            )

        except Exception as exc:
            result[int(sid)] = {
                "PDC": None,
                "PDH": None,
                "PDL": None,
            }

            failures.append(
                f"{sid}: {type(exc).__name__}: {exc}"
            )

    return result, failures


def calculate(data, live_is_fresh):
    numeric = [
        "LTP",
        "PDC",
        "PDH",
        "PDL",
        "Today's Low",
        "Today's High",
    ]

    for field in numeric:
        data[field] = pd.to_numeric(
            data.get(
                field,
                pd.Series(
                    index=data.index,
                    dtype=float,
                ),
            ),
            errors="coerce",
        )

    valid = (
        data["LTP"].notna()
        & data["PDC"].notna()
        & data["PDH"].notna()
        & data["PDL"].notna()
    )

    up = (
        valid
        & data["LTP"].gt(data["PDC"])
    ).groupby(
        data["Sector"]
    ).transform("sum")

    down = (
        valid
        & data["LTP"].lt(data["PDC"])
    ).groupby(
        data["Sector"]
    ).transform("sum")

    count = valid.groupby(
        data["Sector"]
    ).transform("sum")

    ratio = up.div(
        down.where(
            down.ne(0),
            1,
        )
    )

    ratio = ratio.mask(
        (up == 0) & (down == 0),
        pd.NA,
    )

    data["Sector A/D Ratio"] = ratio

    data["Sector Bias"] = ratio.map(
        lambda value:
            "🟢 Bullish"
            if pd.notna(value) and value > 1
            else "🔴 Bearish"
            if pd.notna(value) and value < 1
            else "⚪ Neutral"
            if pd.notna(value)
            else "N/A"
    )

    good = data["PDC"].gt(0)

    data["PDC to PDH %"] = (
        (
            data["PDH"] - data["PDC"]
        )
        / data["PDC"]
        * 100
    ).where(good)

    data["PDC to PDL %"] = (
        (
            data["PDC"] - data["PDL"]
        )
        / data["PDC"]
        * 100
    ).where(good)

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

    reliable_sector = count >= 5

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

    data["Signal Priority"] = data[
        "Signal"
    ].map(
        {
            "🟢 BUY": 0,
            "🔴 SELL": 1,
            "": 2,
        }
    ).fillna(3)

    return data


def market_open(now):
    return (
        now.weekday() < 5
        and dtime(9, 15)
        <= now.time()
        <= dtime(15, 30)
    )


def render():
    now = datetime.now(IST)

    stocks = load_universe()

    ids = tuple(
        stocks["security_id"].astype(int)
    )

    try:
        history, failures = load_history(
            ids,
            (
                now.date() - timedelta(days=30)
            ).isoformat(),
            now.date().isoformat(),
        )
    except Exception as exc:
        history = {}
        failures = [
            f"History loader failed: {type(exc).__name__}: {exc}"
        ]

    if failures:
        st.error(
            f"Previous-day OHLC failed for {len(failures)} stocks. "
            "PDC, PDH, PDL and signals may be blank."
        )

        with st.expander(
            f"Historical data warnings ({len(failures)})"
        ):
            st.code(
                "\n".join(failures[:50])
            )

    quote_error = None
    live_is_fresh = False

    if market_open(now):
        try:
            # Validate credentials before making the quote request.
            if not secret("DHAN_CLIENT_ID") or not secret("DHAN_ACCESS_TOKEN"):
                raise RuntimeError(
                    "Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in Streamlit Secrets."
                )

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
            "Market is closed; live signals are disabled."
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
                **{
                    key: quote.get(key)
                    for key in [
                        "LTP",
                        "Today's Open",
                        "Today's Low",
                        "Today's High",
                    ]
                },
                **{
                    key: old.get(key)
                    for key in [
                        "PDC",
                        "PDH",
                        "PDL",
                    ]
                },
            }
        )

    data = calculate(
        pd.DataFrame(rows),
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
        len(data),
    )

    st.dataframe(
        display_data,
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "Nifty 500 LTP refreshes every 15 seconds "
        "during market hours. Signals require fresh "
        "quotes and at least 5 valid stocks in the sector."
    )


st.title(
    "📈 Nifty 500 Stock Scanner"
)

st.caption(
    "Nifty 500 • Dhan live quotes • "
    "previous-day OHLC • sector breadth"
)

with st.sidebar:
    st.subheader("Data controls")

    if st.button("🔄 Clear cache and refresh", use_container_width=True):
        st.cache_data.clear()
        st.session_state.pop("last_live_quotes", None)
        st.session_state.pop("quote_time", None)
        st.rerun()

    if st.button("🔐 Test Dhan connection", use_container_width=True):
        try:
            with st.spinner("Testing Dhan credentials..."):
                connected, message = test_dhan_connection()

            if connected:
                st.success(message)
            else:
                st.error(message)
        except Exception as exc:
            st.error(f"Connection test error: {type(exc).__name__}: {exc}")

    st.caption(
        "If live data shows 401 Unauthorized, update "
        "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets."
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
