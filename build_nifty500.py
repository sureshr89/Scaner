"""Build stocks.csv from NSE Nifty 500 data and the Dhan instrument master.

Run locally:
    python build_nifty500.py

No Dhan credentials are required for this step. The script intentionally keeps
unmapped rows so missing Dhan security IDs are visible instead of silently
removing constituents.
"""
from io import StringIO
from pathlib import Path
import re
import requests
import pandas as pd

NSE_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
OUT = Path("stocks.csv")


def get(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Accept": "application/json,text/csv,*/*",
        "Referer": "https://www.nseindia.com/",
    }
    with requests.Session() as session:
        session.headers.update(headers)
        session.get("https://www.nseindia.com/", timeout=30)
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return response


def norm(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value).upper().strip())


def first_column(df, names):
    lookup = {str(c).lower().replace(" ", "_"): c for c in df.columns}
    for name in names:
        if name in lookup:
            return lookup[name]
    return None


def main():
    payload = get(NSE_URL).json()
    records = payload.get("data", [])
    if not records:
        raise RuntimeError("NSE returned no Nifty 500 constituent records")
    nse = pd.DataFrame(records)

    symbol_col = first_column(nse, ["symbol"])
    if not symbol_col:
        raise RuntimeError(f"NSE response has no symbol column: {list(nse.columns)}")
    nse["stock"] = nse[symbol_col].astype(str).str.strip().str.upper()
    nse = nse[~nse["stock"].str.contains("NIFTY", na=False)].copy()

    name_col = first_column(nse, ["company_name", "companyname", "name"])
    sector_col = first_column(nse, ["industry", "sector", "industry_info"])
    nse["company_name"] = nse[name_col].astype(str).str.strip() if name_col else nse["stock"]
    nse["sector"] = nse[sector_col].astype(str).str.strip() if sector_col else "UNKNOWN"
    nse["join_key"] = nse["stock"].map(norm)
    nse = nse.drop_duplicates("join_key")

    master = pd.read_csv(StringIO(get(DHAN_MASTER_URL).text), low_memory=False)
    seg = first_column(master, ["exchange_segment", "exchange_segment_name", "exchange_segment_code"])
    sym = first_column(master, ["symbol", "trading_symbol", "sem_trading_symbol"])
    sid = first_column(master, ["security_id", "sem_security_id"])
    isin = first_column(master, ["isin", "sem_isin"])
    if not (seg and sym and sid):
        raise RuntimeError(f"Unexpected Dhan master columns: {list(master.columns)}")

    dm = master[master[seg].astype(str).str.upper().eq("NSE_EQ")].copy()
    dm["join_key"] = dm[sym].map(norm)
    dm = dm.drop_duplicates("join_key")
    dm["security_id"] = pd.to_numeric(dm[sid], errors="coerce")
    keep = ["join_key", "security_id"] + ([isin] if isin else [])
    dm = dm[keep].rename(columns={isin: "isin"} if isin else {})

    out = nse[["stock", "company_name", "sector", "join_key"]].merge(dm, on="join_key", how="left")
    out["exchange_segment"] = "NSE_EQ"
    out["mapping_status"] = out["security_id"].notna().map({True: "OK", False: "MISSING_DHAN_ID"})
    out["security_id"] = out["security_id"].astype("Int64")
    columns = ["stock", "company_name", "sector", "exchange_segment", "security_id", "mapping_status"]
    if "isin" in out.columns:
        columns.append("isin")
    out[columns].sort_values("stock").to_csv(OUT, index=False)

    print(f"Wrote {len(out)} Nifty 500 rows to {OUT}")
    print(f"Mapped to Dhan: {(out['mapping_status'] == 'OK').sum()}")
    print(f"Missing Dhan IDs: {(out['mapping_status'] != 'OK').sum()}")


if __name__ == "__main__":
    main()
