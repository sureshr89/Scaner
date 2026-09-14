import os
import time
import re
from datetime import datetime, time as dt_time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st


st.set_page_config(
    page_title="Dhan Stock Scanner",
    page_icon="📈",
    layout="wide",
)

REFRESH_SECONDS = 60

QUOTE_URL = "https://api.dhan.co/v2/marketfeed/ohlc"
HISTORY_URL = "https://api.dhan.co/v2/charts/historical"
NSE_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
NIFTY_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

IST = ZoneInfo("Asia/Kolkata")


def secret(name):
    return os.getenv(name) or st.secrets.get(name, "")


def headers():
    if not secret("DHAN_ACCESS_TOKEN") or not secret("DHAN_CLIENT_ID"):
        raise RuntimeError(
            "Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets."
        )

    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": secret("DHAN_ACCESS_TOKEN"),
        "client-id": secret("DHAN_CLIENT_ID"),
    }


def norm(value):
    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(value).upper().strip(),
    )


def first_column(frame, names):
    lookup = {
        str(c).lower().replace(" ", "_"): c
        for c in frame.columns
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


@st.cache_data(ttl=86400, show_spinner=False)
def download_nifty500():
    session = universe_session()
    nse = None
    errors = []

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
            f"No symbol column in Nifty data: {list(nse.columns)}"
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

    nse["sector"] = (
        nse[sector_col]
        .astype(str)
        .str.strip()
        if sector_col
        else "UNKNOWN"
    )

    nse["join_key"] = nse["stock"].map(norm)

    nse = nse.drop_duplicates(
        "join_key"
    )

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
    )

    if len(result) < 400:
        raise RuntimeError(
            f"Only {len(result)} stocks mapped from Nifty 500 to Dhan"
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
            f"using only {len(fallback)} local stocks. "
            f"Details: {exc}"
        )

        return fallback


def market_open(now):
    return (
        now.weekday() < 5
        and dt_time(9, 15)
        <= now.time()
        <= dt_time(15, 30)
    )


def dhan_snapshot(stocks):
    response = requests.post(
        QUOTE_URL,
        headers=headers(),
        json={
            "NSE_EQ": sorted(
                {
                    int(x)
                    for x in stocks.security_id
                }
            )
        },
        timeout=60,
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
                "ltp": quote.get("last_price"),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
            }

    return output


@st.cache_data(
    ttl=86400,
    show_spinner=False,
)
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
    start,
    end,
):
    security_ids = [
        int(sid)
        for sid in stocks["security_id"]
    ]

    results = {}

    # Parallel requests make the first load faster.
    # Keep this moderate to avoid API rate limits.
    with ThreadPoolExecutor(
        max_workers=8
    ) as executor:

        futures = {
            executor.submit(
                historical_one,
                sid,
                start,
                end,
            ): sid
            for sid in security_ids
        }

        for future in as_completed(futures):
            sid = futures[future]

            try:
                results[sid] = future.result()

            except Exception as exc:
                results[sid] = {
                    "pdc": None,
                    "pdh": None,
                    "pdl": None,
                    "error": str(exc),
                }

    return results


def build_frame(
    stocks,
    live,
    history,
):
    rows = []

    for record in stocks.to_dict(
        "records"
    ):
        sid = int(
            record["security_id"]
        )

        h = history.get(
            sid,
            {},
        )

        q = live.get(
            (
                "NSE_EQ",
                str(sid),
            ),
            {},
        )

        rows.append(
            {
                "Stock": record["stock"],
                "Sector": record["sector"],
                "LTP": q.get("ltp"),
                "PDC": h.get("pdc"),
                "PDH": h.get("pdh"),
                "PDL": h.get("pdl"),
                "Today's Open": q.get("open"),
                "Today's Low": q.get("low"),
                "Today's High": q.get("high"),
                "History Error": h.get("error"),
            }
        )

    return pd.DataFrame(rows)


def calculate(frame):
    df = frame.copy()

    if df.empty:
        return df

    numeric = [
        "LTP",
        "PDC",
        "PDH",
        "PDL",
        "Today's Open",
        "Today's Low",
        "Today's High",
    ]

    for col in numeric:
        df[col] = pd.to_numeric(
            df[col],
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
        <= 1)
        & df["LTP"].gt(df["PDH"])
        & df["Sector LTP"].gt(
            df["Sector PDC"]
        )
    )

    buy &= (
        df["Today's Low"].lt(
            df["PDH"]
        )
        & df["Today's High"].gt(
            df["PDH"]
        )
        & df["Sector AD"].gt(1)
    )

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
        <= 1)
        & df["LTP"].lt(df["PDL"])
        & df["Sector LTP"].lt(
            df["Sector PDC"]
        )
    )

    sell &= (
        df["Today's High"].gt(
            df["PDL"]
        )
        & df["Today's Low"].lt(
            df["PDL"]
        )
        & df["Sector AD"].lt(1)
    )

    df["Buy Alignment 🟢"] = (
        buy.fillna(False)
    )

    df["Sell Alignment 🔴"] = (
        sell.fillna(False)
    )

    return df


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
        "Distance %",
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

    selected["Distance %"] = distance

    selected = (
        selected
        .sort_values(
            "Distance %"
        )
        .reset_index(
            drop=True
        )
    )

    selected["Rank"] = range(
        1,
        len(selected) + 1,
    )

    return selected[columns]


st.title(
    "📈 Dhan Stock Scanner"
)

st.caption(
    "Nifty 500 universe | Historical PDH/PDL/PDC once daily | "
    "Live Dhan snapshot every 60 seconds"
)

now = datetime.now(IST)

opened = market_open(now)

st.info(
    f"India time: {now:%Y-%m-%d %H:%M:%S IST} | "
    f"{'Market open' if opened else 'Market closed; historical values remain available.'}"
)


try:
    stocks = load_stocks()

    start = (
        now.date()
        - timedelta(days=30)
    ).isoformat()

    end = now.date().isoformat()

    history = historical(
        stocks,
        start,
        end,
    )

    live = (
        flatten(
            dhan_snapshot(
                stocks
            )
        )
        if opened
        else {}
    )

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
            "Live LTP/today OHLC are blank while NSE is closed. "
            "PDC/PDH/PDL are historical Dhan values."
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
        f"Last check: {now:%Y-%m-%d %H:%M:%S IST} | "
        f"Refresh: {REFRESH_SECONDS}s"
    )

except Exception as exc:
    st.error(
        f"Scanner error: {exc}"
    )


time.sleep(
    REFRESH_SECONDS
)

st.rerun()
