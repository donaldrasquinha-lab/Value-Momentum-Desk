#!/usr/bin/env python3
"""
Value x Momentum Desk - automatic NSE scan (no broker login).

1. Universe   : every NSE equity (EQ/BE series) from NSE's public EQUITY_L.csv
2. Fundamentals: TTM EPS, industry, sector, shares, annual EPS history (Yahoo Finance),
                 cached in cache/fundamentals.json and refreshed weekly within a time budget
3. Prices     : 5 years of daily OHLCV for all stocks (Yahoo Finance bulk download)
4. Valuation  : P/E = today's close / TTM EPS
                Industry median P/E = median across the universe (sector fallback)
                Historical avg P/E = mean over past fiscal years of (avg price in FY / FY EPS)
5. Technicals : EMA fast/slow, Supertrend, ADX/+DI/-DI, RSI, ROC, volume ratio
6. Output     : site/data/results.json, site/data/charts.json, site/data/meta.json

Thresholds are NOT applied here - the web page filters live, so you can tune them
without re-running the scan.
"""
import io
import json
import math
import os
import time
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
FUND_FILE = CACHE_DIR / "fundamentals.json"
UNIV_FILE = CACHE_DIR / "universe.json"
OUT_DIR = Path(os.environ.get("OUT_DIR") or ROOT / "site" / "data")


def env_i(k, d):
    return int(os.environ.get(k) or d)


def env_f(k, d):
    return float(os.environ.get(k) or d)


CFG = {
    "emaFast": env_i("EMA_FAST", 50),
    "emaSlow": env_i("EMA_SLOW", 200),
    "stP": env_i("ST_PERIOD", 10),
    "stM": env_f("ST_MULT", 3.0),
    "adxP": env_i("ADX_PERIOD", 14),
    "adxLook": env_i("ADX_LOOK", 3),
    "rocP": env_i("ROC_PERIOD", 20),
    "slope": env_i("SLOPE_BARS", 10),
    "rsiP": 14,
}
FUND_MAX_AGE_DAYS = env_i("FUND_MAX_AGE_DAYS", 7)
FUND_BUDGET_MIN = env_f("FUND_BUDGET_MIN", 35)
LIMIT = env_i("LIMIT", 0)            # >0 = only first N symbols (testing)
BATCH = env_i("PRICE_BATCH", 120)
CHART_BARS = 120
CHART_MAX = 300
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def jload(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def jsave(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False))
    tmp.replace(p)


def rnd(x, d=2):
    if x is None:
        return None
    try:
        x = float(x)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, d)


# ---------------------------------------------------------------- universe
def get_universe():
    s = requests.Session()
    s.headers.update(UA)
    for url in ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                "https://archives.nseindia.com/content/equities/EQUITY_L.csv"):
        try:
            r = s.get(url, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip().upper() for c in df.columns]
            df = df[df["SERIES"].astype(str).str.strip().isin(["EQ", "BE"])]
            uni = [{"s": str(x["SYMBOL"]).strip(),
                    "n": str(x.get("NAME OF COMPANY", "")).strip(),
                    "isin": str(x.get("ISIN NUMBER", "")).strip()} for x in df.to_dict("records")]
            if len(uni) > 500:
                jsave(UNIV_FILE, uni)
                log(f"universe: {len(uni)} NSE equities from EQUITY_L")
                return uni
        except Exception as e:
            log("universe source failed:", url, e)
    try:  # fallback: Nifty Total Market (~750)
        r = s.get("https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv", timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        uni = [{"s": str(x["Symbol"]).strip(), "n": str(x["Company Name"]).strip(),
                "ind": str(x.get("Industry", "")).strip(), "isin": str(x.get("ISIN Code", "")).strip()}
               for x in df.to_dict("records")]
        if uni:
            log(f"universe: {len(uni)} from Nifty Total Market (fallback)")
            return uni
    except Exception as e:
        log("index fallback failed:", e)
    cached = jload(UNIV_FILE, [])
    if cached:
        log(f"universe: {len(cached)} from cache (NSE unreachable)")
        return cached
    raise SystemExit("Could not load the NSE stock list")


# ---------------------------------------------------------------- fundamentals
def fetch_fund(sym, today):
    t = yf.Ticker(sym + ".NS")
    info = t.info or {}
    if not (info.get("trailingEps") is not None or info.get("industry") or info.get("sharesOutstanding")):
        raise ValueError("empty info")
    eh = []
    try:
        inc = t.income_stmt
        if inc is not None and not inc.empty:
            for row in ("Diluted EPS", "Basic EPS"):
                if row in inc.index:
                    for col, v in inc.loc[row].items():
                        if pd.notna(v):
                            eh.append([pd.Timestamp(col).strftime("%Y-%m-%d"), round(float(v), 4)])
                    break
    except Exception:
        pass
    return {
        "ts": today,
        "n": info.get("longName") or info.get("shortName"),
        "ind": info.get("industry"),
        "sec": info.get("sector"),
        "eps": info.get("trailingEps"),
        "sh": info.get("sharesOutstanding"),
        "mc": info.get("marketCap"),
        "eh": sorted(eh),
    }


def refresh_fundamentals(uni):
    fund = jload(FUND_FILE, {})
    today = dt.date.today()
    today_s = today.isoformat()

    def age(rec):
        try:
            return (today - dt.date.fromisoformat(rec["ts"])).days
        except Exception:
            return 9999

    syms = [u["s"] for u in uni]
    stale = [s for s in syms if age(fund.get(s, {})) >= FUND_MAX_AGE_DAYS]
    stale.sort(key=lambda s: age(fund.get(s, {})), reverse=True)  # missing first, then oldest
    log(f"fundamentals: {len(syms) - len(stale)} fresh, {len(stale)} to refresh, budget {FUND_BUDGET_MIN:.0f} min")
    deadline = time.time() + FUND_BUDGET_MIN * 60
    ok = streak = storms = 0
    for i, s in enumerate(stale):
        if time.time() > deadline:
            log("fundamentals: time budget reached, rest continues next run")
            break
        try:
            rec = fetch_fund(s, today_s)
            old = fund.get(s, {})
            merged = {**old, **{k: v for k, v in rec.items() if v not in (None, [])}}
            merged["ts"] = today_s
            merged.pop("f", None)
            fund[s] = merged
            ok += 1
            streak = 0
        except Exception:
            rec = fund.setdefault(s, {})
            rec["f"] = rec.get("f", 0) + 1
            if rec["f"] >= 3:          # delisted / no data: try again next week
                rec["ts"] = today_s
                rec["f"] = 0
            streak += 1
            if streak >= 12:
                storms += 1
                log("fundamentals: many failures in a row (throttled?) - cooling off 90s")
                time.sleep(90)
                streak = 0
                if storms >= 4:
                    log("fundamentals: giving up for this run")
                    break
        time.sleep(0.35)
        if (i + 1) % 50 == 0:
            jsave(FUND_FILE, fund)
            log(f"fundamentals: {i + 1}/{len(stale)} processed, {ok} ok")
    jsave(FUND_FILE, fund)
    log(f"fundamentals: refreshed {ok}")
    return fund


# ---------------------------------------------------------------- prices
def download_prices(syms):
    data = {}
    tick = [s + ".NS" for s in syms]
    for i in range(0, len(tick), BATCH):
        batch = tick[i:i + BATCH]
        df = None
        for att in range(3):
            try:
                df = yf.download(batch, period="5y", interval="1d", group_by="ticker",
                                 auto_adjust=False, actions=False, threads=True,
                                 progress=False, timeout=30)
                if df is not None and not df.empty:
                    break
            except Exception as e:
                log("price batch error:", e)
            time.sleep(15 * (att + 1))
        if df is None or df.empty:
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        for t in batch:
            try:
                d = df[t] if multi else df
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
                d = d[d["Close"] > 0]
                if len(d) >= 60:
                    idx = pd.to_datetime(d.index)
                    if idx.tz is not None:
                        idx = idx.tz_localize(None)
                    d = d.copy()
                    d.index = idx.normalize()
                    data[t[:-3]] = d
            except Exception:
                pass
        log(f"prices: {min(i + BATCH, len(tick))}/{len(tick)} requested, {len(data)} usable")
        time.sleep(1.5)
    return data


# ---------------------------------------------------------------- indicators
def ema(a, p):
    o = np.full(len(a), np.nan)
    if len(a) < p:
        return o
    o[p - 1] = a[:p].mean()
    k = 2 / (p + 1)
    for i in range(p, len(a)):
        o[i] = a[i] * k + o[i - 1] * (1 - k)
    return o


def rma(a, p):
    o = np.full(len(a), np.nan)
    ok = np.where(~np.isnan(a))[0]
    if not len(ok):
        return o
    i0 = ok[0]
    if len(a) - i0 < p:
        return o
    prev = a[i0:i0 + p].mean()
    o[i0 + p - 1] = prev
    for i in range(i0 + p, len(a)):
        prev = (prev * (p - 1) + a[i]) / p
        o[i] = prev
    return o


def true_range(h, l, c):
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    return tr


def supertrend(h, l, c, p, m):
    atr = rma(true_range(h, l, c), p)
    n = len(c)
    dr = np.full(n, np.nan)
    st = np.full(n, np.nan)
    ub = lb = None
    d = 1
    for i in range(n):
        if np.isnan(atr[i]):
            continue
        hl2 = (h[i] + l[i]) / 2
        bu, bl = hl2 + m * atr[i], hl2 - m * atr[i]
        if ub is None:
            ub, lb = bu, bl
            d = 1 if c[i] >= hl2 else -1
        else:
            nlb = bl if (bl > lb or c[i - 1] < lb) else lb
            nub = bu if (bu < ub or c[i - 1] > ub) else ub
            if d == -1 and c[i] > nub:
                d = 1
            elif d == 1 and c[i] < nlb:
                d = -1
            lb, ub = nlb, nub
        dr[i] = d
        st[i] = lb if d == 1 else ub
    return dr, st


def adx_calc(h, l, c, p):
    n = len(c)
    pdm = np.full(n, np.nan)
    mdm = np.full(n, np.nan)
    tr = np.full(n, np.nan)
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    pdm[1:] = np.where((up > dn) & (up > 0), up, 0.0)
    mdm[1:] = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr[1:] = true_range(h, l, c)[1:]
    s_tr, s_p, s_m = rma(tr, p), rma(pdm, p), rma(mdm, p)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * s_p / s_tr
        mdi = 100 * s_m / s_tr
        tot = pdi + mdi
        dx = np.where(tot > 0, 100 * np.abs(pdi - mdi) / tot, 0.0)
    dx[np.isnan(s_tr)] = np.nan
    return rma(dx, p), pdi, mdi


def rsi(c, p):
    d = np.diff(c, prepend=np.nan)
    g = np.maximum(d, 0)
    ls = np.maximum(-d, 0)
    ag, al = rma(g, p), rma(ls, p)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(al == 0, 100.0, 100 - 100 / (1 + ag / al))
    out[np.isnan(ag)] = np.nan
    return out


def analyze(d):
    P = CFG
    d = d.iloc[-450:]
    h, l, c, v = (d[k].to_numpy(float) for k in ("High", "Low", "Close", "Volume"))
    n = len(c)
    L = n - 1
    if n < max(P["emaFast"] + P["slope"] + 1, P["rocP"] + 5, 60):
        return None
    ef, es = ema(c, P["emaFast"]), ema(c, P["emaSlow"])
    dr, st = supertrend(h, l, c, P["stP"], P["stM"])
    ax, pdi, mdi = adx_calc(h, l, c, P["adxP"])
    rs = rsi(c, P["rsiP"])
    flip = None
    if dr[L] == 1:
        for i in range(L, 0, -1):
            if np.isnan(dr[i - 1]):
                break
            if dr[i - 1] == -1:
                flip = L - i
                break
    v20 = np.nanmean(v[-21:-1]) if n > 21 else np.nan
    row = {
        "ef": rnd(ef[L]), "es": rnd(es[L]),
        "fr": bool(not np.isnan(ef[L - P["slope"]]) and ef[L] > ef[L - P["slope"]]),
        "stk": bool(not np.isnan(es[L]) and ef[L] > es[L]),
        "dir": None if np.isnan(dr[L]) else int(dr[L]),
        "flip": flip,
        "sl": rnd(st[L]), "slp": rnd((c[L] - st[L]) / c[L] * 100, 1),
        "adx": rnd(ax[L], 1), "adxL": rnd(ax[L - P["adxLook"]], 1), "adx1": rnd(ax[L - 1], 1),
        "pdi": rnd(pdi[L], 1), "mdi": rnd(mdi[L], 1),
        "rsi": rnd(rs[L], 1),
        "roc": rnd((c[L] / c[L - P["rocP"]] - 1) * 100, 1),
        "vr": rnd(v[L] / v20, 2) if v20 and v20 > 0 else None,
    }
    k = min(CHART_BARS, n)
    series = {
        "t": [x.strftime("%Y-%m-%d") for x in d.index[-k:]],
        "o": [rnd(x) for x in d["Open"].to_numpy(float)[-k:]],
        "h": [rnd(x) for x in h[-k:]], "l": [rnd(x) for x in l[-k:]], "c": [rnd(x) for x in c[-k:]],
        "st": [rnd(x) for x in st[-k:]], "d": [None if np.isnan(x) else int(x) for x in dr[-k:]],
        "f": [rnd(x) for x in ef[-k:]], "w": [rnd(x) for x in es[-k:]],
        "a": [rnd(x, 1) for x in ax[-k:]], "p": [rnd(x, 1) for x in pdi[-k:]], "m": [rnd(x, 1) for x in mdi[-k:]],
    }
    return row, series


def hist_pe(d, eh):
    """Average of yearly P/E where P = mean close over the fiscal year and E = that year's EPS."""
    pes = []
    closes = d["Close"]
    for date_s, eps in eh or []:
        if eps is None or eps <= 0:
            continue
        end = pd.Timestamp(date_s)
        w = closes[(closes.index > end - pd.Timedelta(days=365)) & (closes.index <= end)]
        if len(w) < 150:
            continue
        pe = float(w.mean()) / eps
        if 0 < pe <= 200:
            pes.append(pe)
    return (rnd(np.mean(pes)), len(pes)) if len(pes) >= 2 else (None, len(pes))


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    uni = get_universe()
    if LIMIT:
        uni = uni[:LIMIT]
    fund = refresh_fundamentals(uni)
    prices = download_prices([u["s"] for u in uni])
    if not prices:
        raise SystemExit("No price data downloaded - Yahoo may be blocking; try again later")
    mkt_date = max(d.index[-1] for d in prices.values())

    recs, series = [], {}
    for u in uni:
        s = u["s"]
        f = fund.get(s, {})
        rec = {"s": s, "n": f.get("n") or u.get("n") or s, "ind": f.get("ind") or u.get("ind") or "",
               "sec": f.get("sec") or "", "eps": rnd(f.get("eps"))}
        d = prices.get(s)
        if d is not None:
            c = float(d["Close"].iloc[-1])
            rec["c"] = rnd(c)
            rec["chg"] = rnd((c / float(d["Close"].iloc[-2]) - 1) * 100) if len(d) > 1 else None
            rec["date"] = d.index[-1].strftime("%Y-%m-%d")
            eps = f.get("eps")
            rec["pe"] = rnd(c / eps) if eps and eps > 0 else None
            rec["loss"] = bool(eps is not None and eps <= 0)
            rec["hpe"], rec["hpeN"] = hist_pe(d, f.get("eh"))
            if f.get("sh"):
                rec["mc"] = round(c * f["sh"] / 1e7)
            elif f.get("mc"):
                rec["mc"] = round(f["mc"] / 1e7)
            if (mkt_date - d.index[-1]).days > 7:
                rec["stale"] = True
            else:
                a = analyze(d)
                if a:
                    rec.update(a[0])
                    series[s] = a[1]
        recs.append(rec)

    # industry median P/E (sector fallback)
    by_ind, by_sec = {}, {}
    for r in recs:
        pe = r.get("pe")
        if pe and 0 < pe <= 300:
            if r["ind"]:
                by_ind.setdefault(r["ind"], []).append(pe)
            if r["sec"]:
                by_sec.setdefault(r["sec"], []).append(pe)
    med_ind = {k: float(np.median(v)) for k, v in by_ind.items() if len(v) >= 4}
    med_sec = {k: float(np.median(v)) for k, v in by_sec.items() if len(v) >= 6}
    for r in recs:
        if r["ind"] in med_ind:
            r["im"], r["imSrc"], r["imN"] = rnd(med_ind[r["ind"]]), "industry", len(by_ind[r["ind"]])
        elif r["sec"] in med_sec:
            r["im"], r["imSrc"], r["imN"] = rnd(med_sec[r["sec"]]), "sector", len(by_sec[r["sec"]])

    # charts for the most interesting names (loaded lazily by the page)
    def interest(r):
        sc = 0.0
        if r.get("pe") and r.get("im"):
            sc += max(0, 1 - r["pe"] / r["im"])
        if r.get("pe") and r.get("hpe"):
            sc += max(0, 1 - r["pe"] / r["hpe"])
        if r.get("dir") == 1 and r.get("flip") is not None and r["flip"] <= 15:
            sc += 1 - r["flip"] / 16
        if r.get("adx") and r.get("adxL") and r["adx"] > r["adxL"]:
            sc += 0.3
        return sc
    cand = sorted((r for r in recs if r["s"] in series), key=interest, reverse=True)[:CHART_MAX]
    charts = {r["s"]: series[r["s"]] for r in cand}

    now = dt.datetime.now(IST)
    meta = {
        "generated": now.isoformat(timespec="seconds"),
        "marketDate": mkt_date.strftime("%Y-%m-%d"),
        "universe": len(uni),
        "withPrices": len(prices),
        "withEps": sum(1 for r in recs if r.get("eps") is not None),
        "withHistPE": sum(1 for r in recs if r.get("hpe")),
        "withTech": len(series),
        "charts": len(charts),
        "cfg": CFG,
        "seconds": round(time.time() - t0),
    }
    jsave(OUT_DIR / "results.json", {"meta": meta, "stocks": recs})
    jsave(OUT_DIR / "charts.json", charts)
    jsave(OUT_DIR / "meta.json", meta)
    log("done:", json.dumps(meta))


if __name__ == "__main__":
    main()
