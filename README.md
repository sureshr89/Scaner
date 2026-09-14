# Scaner

## Build the Nifty 500 universe

You do **not** need to prepare a CSV manually. Run:

```bash
pip install pandas requests
python build_nifty500.py
```

The script reads the current Nifty 500 constituent records from NSE and maps them to the Dhan instrument master. It writes `stocks.csv` with:

- `stock`
- `company_name`
- `sector`
- `exchange_segment`
- `security_id`
- `mapping_status`
- `isin` when available

Rows with missing Dhan IDs are retained as `MISSING_DHAN_ID` so they can be reviewed rather than silently discarded.

NSE publishes the Nifty 500 constituent list through its official Nifty 500 page: https://www.nseindia.com/static/products-services/indices-nifty500-index

## Dhan credentials

Live LTP data must be fetched using your own Dhan account credentials. Keep them local, for example in Streamlit secrets or environment variables; do not commit them to GitHub.
