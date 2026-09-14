# Scaner — Nifty Midcap 150

Streamlit scanner for the **Nifty Midcap 150** universe using Dhan market data.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Cloud

1. Open Streamlit Community Cloud.
2. Create a new app from this repository.
3. Select branch `main` and file `app.py`.
4. In **Settings → Secrets**, add:

```toml
DHAN_CLIENT_ID = "your_dhan_client_id"
DHAN_ACCESS_TOKEN = "your_dhan_access_token"
```

5. Deploy or reboot the app.

Never commit Dhan credentials to GitHub.

## Output

The app displays one table containing:

- Stock
- Sector
- Sector A/D Ratio — stocks above PDC divided by stocks below PDC
- LTP
- PDC — previous completed daily close
- PDH — previous completed daily high
- PDL — previous completed daily low
- PDC to PDH %
- PDC to PDL %

The app refreshes live quotes every 15 seconds and caches historical data for 24 hours. When the market is closed, LTP can be blank while PDC/PDH/PDL remain available.

## Audit notes

- Universe is explicitly validated to map exactly 150 constituents to Dhan.
- Historical requests are sequential and rate-limit aware.
- Historical API errors are exposed in the UI.
- The scanner does not place trades; it is informational only.
