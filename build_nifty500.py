"""Build stocks.csv from the NSE Nifty 500 constituent CSV and Dhan instrument master.

Usage:
    python build_nifty500.py

The script does not need Dhan credentials. It creates a stock universe with
NSE symbol, company name, sector, ISIN and Dhan security_id where available.
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
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,application/json"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r


def main():
    # NSE endpoint returns the current Nifty 500 constituent records.
    nse = get(NSE_URL).json()
    records = nse.get("data", [])
    if not records:
        raise RuntimeError("NSE returned no Nifty 500 records")
    constituents = pd.DataFrame(records)
    symbol_col = "symbol"
    name_col = "meta" if "meta" in constituents.columns else None
    constituents = constituents.rename(columns={symbol_col: "stock"})
    constituents["stock"] = constituents["stock"].astype(str).str.strip()
    constituents = constituents[~constituents["stock"].str.contains("NIFTY", case=False, na=False)]

    # Dhan master contains exchange segment, symbol, ISIN and security ID.
    master = pd.read_csv(StringIO(get(DHAN_MASTER_URL).text), low_memory=False)
    cols = {c.lower(): c for c in master.columns}
    def find(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None
    seg = find("exchange_segment", "exchange segment")
    sym = find("symbol", "trading_symbol", "sem_trading_symbol")
    sid = find("security_id", "sem_security_id")
    isin = find("isin", "sem_isin")
    if not (seg and sym and sid):
        raise RuntimeError(f"Unexpected Dhan master columns: {list(master.columns)}")
    dm = master[master[seg].astype(str).str.upper().eq("NSE_EQ")].copy()
    dm["stock"] = dm[sym].astype(str).str.upper().str.strip()
    dm = dm.drop_duplicates("stock")
    keep = ["stock", sid]
    if isin: keep.append(isin)
    dm = dm[keep].rename(columns={sid: "security_id", **({isin: "isin"} if isin else {})})

    out = constituents[["stock"]].drop_duplicates().merge(dm, on="stock", how="left")
    # NSE API may expose industry/sector under different names.
    sector_candidates = [c for c in ["industry", "sector", "industryInfo"] if c in constituents.columns]
    if sector_candidates:
        out = out.merge(constituents[["stock", sector_candidates[0]]].rename(columns={sector_candidates[0]: "sector"}), on="stock", how="left")
    else:
        out["sector"] = "UNKNOWN"
    out["company_name"] = out["stock"]
    out["security_id"] = pd.to_numeric(out["security_id"], errors="coerce")
    out = out.dropna(subset=["security_id"]).copy()
    out["security_id"] = out["security_id"].astype(int)
    out = out[["stock", "company_name", "sector", "security_id"] + (["isin"] if "isin" in out else [])]
    out.to_csv(OUT, index=False)
    print(f"Wrote {len(out)} stocks to {OUT}")
    print(f"Missing Dhan IDs: {len(constituents) - len(out)}")


if __name__ == "__main__":
    main()
