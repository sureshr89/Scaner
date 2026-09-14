import os
import time
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Dhan Stock Scanner", page_icon="📈", layout="wide")
REFRESH_SECONDS=15
QUOTE_URL="https://api.dhan.co/v2/marketfeed/ohlc"
HISTORY_URL="https://api.dhan.co/v2/charts/historical"
IST=ZoneInfo("Asia/Kolkata")

def secret(n): return os.getenv(n) or st.secrets.get(n,"")
def headers():
    if not secret("DHAN_ACCESS_TOKEN") or not secret("DHAN_CLIENT_ID"): raise RuntimeError("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Streamlit Secrets.")
    return {"Accept":"application/json","Content-Type":"application/json","access-token":secret("DHAN_ACCESS_TOKEN"),"client-id":secret("DHAN_CLIENT_ID")}
def load_stocks():
    df=pd.read_csv("stocks.csv"); req={"stock","sector","security_id"}
    missing=req-set(df.columns)
    if missing: raise ValueError(f"stocks.csv missing columns: {sorted(missing)}")
    df["security_id"]=pd.to_numeric(df["security_id"],errors="coerce")
    return df.dropna(subset=["stock","sector","security_id"]).copy()
def market_open(now): return now.weekday()<5 and dt_time(9,15)<=now.time()<=dt_time(15,30)
def dhan_snapshot(stocks):
    r=requests.post(QUOTE_URL,headers=headers(),json={"NSE_EQ":sorted({int(x) for x in stocks.security_id})},timeout=20); r.raise_for_status(); return r.json()
def flatten(s):
    out={}
    for seg,items in (s.get("data") or {}).items():
        for sid,p in (items or {}).items():
            o=p.get("ohlc") or {}; out[(seg,str(sid))]={"ltp":p.get("last_price"),"open":o.get("open"),"high":o.get("high"),"low":o.get("low")}
    return out
@st.cache_data(ttl=86400,show_spinner=False)
def historical_one(sid,start,end):
    payload={"securityId":str(int(sid)),"exchangeSegment":"NSE_EQ","instrument":"EQUITY","expiryCode":0,"oi":False,"fromDate":start,"toDate":end}
    try:
        r=requests.post(HISTORY_URL,headers=headers(),json=payload,timeout=20)
        if not r.ok: return {"pdc":None,"pdh":None,"pdl":None,"error":f"security {sid}: HTTP {r.status_code} {r.text[:200]}"}
        body=r.json()
        d=body.get("data",body) if isinstance(body,dict) else {}
        if not isinstance(d,dict): return {"pdc":None,"pdh":None,"pdl":None,"error":"Invalid response"}
        cols=["timestamp","close","high","low"]
        if any(c not in d for c in cols): return {"pdc":None,"pdh":None,"pdl":None,"error":"Missing candle fields"}
        x=pd.DataFrame({c:d[c] for c in cols})
        for c in cols: x[c]=pd.to_numeric(x[c],errors="coerce")
        x=x.dropna(subset=cols)
        if x.empty: return {"pdc":None,"pdh":None,"pdl":None,"error":"No valid candles"}
        x=x.sort_values("timestamp")
        row=x.iloc[-1]
        return {"pdc":float(row["close"]),"pdh":float(row["high"]),"pdl":float(row["low"]),"error":None}
    except Exception as e: return {"pdc":None,"pdh":None,"pdl":None,"error":str(e)}
def historical(stocks,now):
    start=(now.date()-timedelta(days=30)).isoformat(); end=now.date().isoformat(); result={}
    for sid in stocks.security_id: result[int(sid)]=historical_one(sid,start,end)
    return result
def build_frame(stocks,live,hist):
    rows=[]
    for r in stocks.to_dict("records"):
        sid=int(r["security_id"]); h=hist.get(sid,{}) ; q=live.get(("NSE_EQ",str(sid)),{})
        rows.append({"Stock":r["stock"],"Sector":r["sector"],"LTP":q.get("ltp"),"PDC":h.get("pdc"),"PDH":h.get("pdh"),"PDL":h.get("pdl"),"Today's Open":q.get("open"),"Today's Low":q.get("low"),"Today's High":q.get("high"),"History Error":h.get("error")})
    return pd.DataFrame(rows)
def calculate(df):
    if df.empty:return df
    df=df.copy()
    nums=["LTP","PDC","PDH","PDL","Today's Open","Today's Low","Today's High"]
    for c in nums: df[c]=pd.to_numeric(df[c],errors="coerce")
    df["Sector LTP"]=df.groupby("Sector")["LTP"].transform("mean")
    df["Sector PDC"]=df.groupby("Sector")["PDC"].transform("mean")
    up=df["LTP"].gt(df["PDC"]).fillna(False).groupby(df["Sector"]).transform("sum")
    down=df["LTP"].lt(df["PDC"]).fillna(False).groupby(df["Sector"]).transform("sum")
    df["Sector AD"]=up/down.replace(0,float("nan"))
    df["Sector % from PDC"]=(df["Sector LTP"]-df["Sector PDC"])/df["Sector PDC"]*100
    buy=(df["PDC"].notna()&df["PDH"].notna()&df["LTP"].notna()&df["Sector LTP"].notna()&df["Sector PDC"].notna())
    buy &= ((df["PDH"]-df["PDC"])/df["PDC"]*100<=1)
    buy &= df["LTP"].gt(df["PDH"])&df["Sector LTP"].gt(df["Sector PDC"])
    buy &= df["Today's Low"].lt(df["PDH"])&df["Today's High"].gt(df["PDH"])&df["Sector AD"].gt(1)
    sell=(df["PDC"].notna()&df["PDL"].notna()&df["LTP"].notna()&df["Sector LTP"].notna()&df["Sector PDC"].notna())
    sell &= ((df["PDC"]-df["PDL"])/df["PDC"]*100<=1)
    sell &= df["LTP"].lt(df["PDL"])&df["Sector LTP"].lt(df["Sector PDC"])
    sell &= df["Today's High"].gt(df["PDL"])&df["Today's Low"].lt(df["PDL"])&df["Sector AD"].lt(1)
    df["Buy Alignment 🟢"]=buy.fillna(False); df["Sell Alignment 🔴"]=sell.fillna(False); return df
def table(df,kind):
    level="PDH" if kind=="buy" else "PDL"; align="Buy Alignment 🟢" if kind=="buy" else "Sell Alignment 🔴"
    cols=["Rank","Stock","Sector","LTP","PDC",level,"Today's Open","Today's Low","Today's High","Sector LTP","Sector PDC","Sector % from PDC","Sector AD",align]
    if df.empty:return pd.DataFrame(columns=cols)
    x=df[df[align].fillna(False)].copy()
    if x.empty:return pd.DataFrame(columns=cols)
    d=(x["PDH"]-x["PDC"])/x["PDC"]*100 if kind=="buy" else (x["PDC"]-x["PDL"])/x["PDC"]*100
    x=x.assign(_distance=d).sort_values("_distance"); x["Rank"]=range(1,len(x)+1); return x[cols]

st.title("📈 Dhan Stock Scanner"); st.caption("Historical PDH/PDL/PDC once daily + one live Dhan snapshot every 15 seconds")
now=datetime.now(IST); opened=market_open(now); st.info(f"India time: {now:%Y-%m-%d %H:%M:%S IST} | {'Market open' if opened else 'Market closed; historical values remain available.'}")
try:
    stocks=load_stocks(); hist=historical(stocks,now); live=flatten(dhan_snapshot(stocks)) if opened else {}; data=calculate(build_frame(stocks,live,hist)); buys=table(data,"buy"); sells=table(data,"sell")
    a,b,c=st.columns(3); a.metric("Stocks",len(data)); b.metric("🟢 Buy",len(buys)); c.metric("🔴 Sell",len(sells))
    errs=data.loc[data["History Error"].notna(),["Stock","History Error"]]
    if not errs.empty:
        with st.expander("Historical API details"): st.dataframe(errs,use_container_width=True)
    if not opened: st.warning("Live LTP/today OHLC are blank while NSE is closed. PDC/PDH/PDL are historical Dhan values.")
    st.subheader("🟢 BUY WATCHLIST"); st.dataframe(buys,use_container_width=True)
    st.subheader("🔴 SELL WATCHLIST"); st.dataframe(sells,use_container_width=True)
    with st.expander("All scanned stocks"): st.dataframe(data,use_container_width=True)
    st.caption(f"Last check: {now:%Y-%m-%d %H:%M:%S IST} | Refresh: {REFRESH_SECONDS}s")
except Exception as e: st.error(f"Scanner error: {e}")
time.sleep(REFRESH_SECONDS); st.rerun()
