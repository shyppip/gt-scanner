"""
미국 증시 일일 데이터 수집 (일일 미증시 보고용) — GitHub Actions에서 매일 미장 마감 후 실행

만드는 파일 (data 브랜치에 최신본만 강제 푸시 → 저장소 용량이 쌓이지 않음):
  prices.csv          최근 약 90거래일 일봉 (date,ticker,open,high,low,close,volume)
  metrics.csv         종목·ETF별 1/5/20일 수익률, SPY 대비 초과, 거래량 배수, RSI14, 20일 고점 대비
  macro.csv           국채(2·10·30년, 10년 실질), DXY, VIX, MOVE, WTI, 브렌트, 금 — 최근값·직전값·변화
  snapshots/YYYY-MM-DD.csv  미국 상장 전 종목 당일 종가·등락률·거래량·시총 (나스닥 스크리너, 최근 40일 보관)
  status.json         생성 시각, 기준 거래일, 출처별 성공/실패, 못 받은 티커

티커 범위: 핵심 ETF + S&P500 + 나스닥100 + watchlist.txt(보고서 후보 원장 종목)
"""
import csv
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "application/json,text/csv,*/*"}
S = requests.Session()
S.headers.update(UA)

ETFS = ("SPY QQQ IWM DIA TLT XLE XLK XLF XLV XLP XLY XLU XLI XLB XLRE XLC "
        "XOP CRAK SMH SOXX IGV CIBR NLR XBI IBB KBE KRE KIE ITA IYZ GLD SLV GDX GDXJ XME ITB JETS EWZ KWEB XRT").split()
MACRO_Y = {"DXY": "DX-Y.NYB", "VIX": "^VIX", "MOVE": "^MOVE", "WTI": "CL=F", "BRENT": "BZ=F", "GOLD": "GC=F",
           "US10Y_YAHOO": "^TNX", "US30Y_YAHOO": "^TYX"}
status = {"ok": {}, "fail": {}, "missing": []}


def note(key, ok):
    d = status["ok" if ok else "fail"]
    d[key] = d.get(key, 0) + 1


def get(url, **kw):
    for i in range(3):
        try:
            r = S.get(url, timeout=25, **kw)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except Exception:
            time.sleep(2 * (i + 1))
    return None


# ───────── 티커 목록 ─────────
def universe():
    tk = set(ETFS)
    r = get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv")
    if r:
        for row in csv.DictReader(io.StringIO(r.text)):
            tk.add(row["Symbol"].strip())
        note("sp500_list", True)
    else:
        note("sp500_list", False)
    r = get("https://api.nasdaq.com/api/quote/list-type/nasdaq100",
            headers={**UA, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    try:
        m = [x["symbol"].strip() for x in r.json()["data"]["data"]["rows"]]
    except Exception:
        m = []
    tk.update(m)
    note("ndx_list", len(m) > 50)
    if os.path.exists("watchlist.txt"):
        for line in open("watchlist.txt", encoding="utf-8"):
            t = line.split("#")[0].strip().upper()
            if t:
                tk.add(t)
    return sorted(tk)


def ysym(t):
    return t.replace(".", "-")


# ───────── 일봉 ─────────
def yahoo_daily(sym, rng="6mo"):
    r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": rng, "interval": "1d"})
    if not r:
        r = get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": rng, "interval": "1d"})
    if not r:
        return None
    try:
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        tz = res["meta"].get("exchangeTimezoneName", "America/New_York")
        off = res["meta"].get("gmtoffset", -14400)
        rows = []
        for i, t in enumerate(ts):
            c = q["close"][i]
            if c is None:
                continue
            d = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
            rows.append((d, q["open"][i], q["high"][i], q["low"][i], c, q["volume"][i] or 0))
        # 장중 진행 중 봉 제외: 마지막 봉의 거래 시간이 끝나지 않았으면 버림
        reg = res["meta"].get("currentTradingPeriod", {}).get("regular", {})
        if rows and reg and time.time() < reg.get("end", 0) and rows[-1][0] == datetime.fromtimestamp(reg["end"] + off, tz=timezone.utc).strftime("%Y-%m-%d"):
            rows = rows[:-1]
        return rows
    except Exception:
        return None


def stooq_daily(t):
    r = get("https://stooq.com/q/d/l/", params={"s": t.lower().replace(".", "-") + ".us", "i": "d"})
    if not r or not r.text.startswith("Date"):
        return None
    rows = []
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            rows.append((row["Date"], float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]), float(row.get("Volume") or 0)))
        except Exception:
            pass
    return rows[-130:] or None


# ───────── 지표 계산 ─────────
def rsi14(cl):
    if len(cl) < 15:
        return None
    g = l = 0.0
    for i in range(1, 15):
        d = cl[i] - cl[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g /= 14
    l /= 14
    for i in range(15, len(cl)):
        d = cl[i] - cl[i - 1]
        g = (g * 13 + max(d, 0)) / 14
        l = (l * 13 + max(-d, 0)) / 14
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def ret(cl, n):
    return (cl[-1] / cl[-1 - n] - 1) * 100 if len(cl) > n and cl[-1 - n] else None


def metrics(t, rows, spy):
    cl = [r[4] for r in rows]
    vol = [r[5] for r in rows]
    out = {"ticker": t, "last_date": rows[-1][0], "close": round(cl[-1], 4)}
    for n in (1, 5, 20):
        v = ret(cl, n)
        out[f"r{n}d_pct"] = None if v is None else round(v, 2)
        s = spy.get(n)
        out[f"r{n}d_vs_spy_pp"] = None if v is None or s is None else round(v - s, 2)
    v20 = vol[-21:-1]
    out["volume"] = int(vol[-1])
    out["vol20_avg"] = int(sum(v20) / len(v20)) if len(v20) == 20 else None
    out["vol_x"] = round(vol[-1] / out["vol20_avg"], 2) if out["vol20_avg"] else None
    r = rsi14(cl)
    out["rsi14"] = None if r is None else round(r, 1)
    hi = max(r_[2] or r_[4] for r_ in rows[-20:])
    out["high20"] = round(hi, 4)
    out["from_high20_pct"] = round((cl[-1] / hi - 1) * 100, 2)
    return out


# ───────── 거시 ─────────
def treasury(kind):
    """kind: daily_treasury_yield_curve / daily_treasury_real_yield_curve → [(date, {col:val})] 오래된→최근"""
    out = []
    now = datetime.now(KST)
    for y in sorted({now.year, (now - timedelta(days=20)).year}):
        r = get(f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all",
                params={"type": kind, "field_tdr_date_value": y, "page": "", "_format": "csv"})
        if not r:
            continue
        for row in csv.DictReader(io.StringIO(r.text)):
            try:
                d = datetime.strptime(row["Date"], "%m/%d/%Y").strftime("%Y-%m-%d")
                out.append((d, row))
            except Exception:
                pass
    out.sort()
    note("treasury_" + ("real" if "real" in kind else "nominal"), bool(out))
    return out


def snapshot():
    r = get("https://api.nasdaq.com/api/screener/stocks", params={"tableonly": "true", "limit": "10000", "download": "true"},
            headers={**UA, "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    try:
        rows = r.json()["data"]["rows"]
    except Exception:
        note("nasdaq_snapshot", False)
        return []
    note("nasdaq_snapshot", True)
    f = lambda s: float(re.sub(r"[^0-9.\-]", "", s or "") or "nan")
    out = []
    for x in rows:
        try:
            out.append({"ticker": x["symbol"].strip().replace("/", "."), "close": f(x["lastsale"]), "chg_pct": f(x["pctchange"]),
                        "volume": int(f(x["volume"])) if x.get("volume") else "", "market_cap": f(x.get("marketCap")), "name": x.get("name", "")[:60]})
        except Exception:
            pass
    return out


def wcsv(path, rows, cols):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None or (isinstance(r.get(k), float) and math.isnan(r.get(k))) else r.get(k)) for k in cols})


def main():
    os.makedirs(OUT, exist_ok=True)
    tickers = universe()
    print("tickers", len(tickers), flush=True)
    hist = {}
    for i, t in enumerate(tickers):
        rows = yahoo_daily(ysym(t))
        src = "yahoo"
        if not rows:
            rows = stooq_daily(t)
            src = "stooq"
        if rows and len(rows) >= 2:
            hist[t] = rows[-90:]
            note(src, True)
        else:
            status["missing"].append(t)
            note("daily", False)
        time.sleep(0.15)
        if i % 100 == 0:
            print(i, t, len(hist), flush=True)
    spy = hist.get("SPY")
    if not spy:
        raise SystemExit("SPY 일봉을 못 받음 — 중단")
    base_date = spy[-1][0]
    scl = [r[4] for r in spy]
    spyr = {n: ret(scl, n) for n in (1, 5, 20)}

    # prices.csv
    with open(f"{OUT}/prices.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "ticker", "open", "high", "low", "close", "volume"])
        for t in sorted(hist):
            for d, o, h, l, c, v in hist[t]:
                w.writerow([d, t, "" if o is None else round(o, 4), "" if h is None else round(h, 4), "" if l is None else round(l, 4), round(c, 4), int(v)])

    # metrics.csv
    ms = []
    for t, rows in hist.items():
        m = metrics(t, rows, spyr)
        m["type"] = "etf" if t in ETFS else "stock"
        m["stale"] = "" if m["last_date"] == base_date else "Y"
        ms.append(m)
    cols = ["ticker", "type", "last_date", "stale", "close", "r1d_pct", "r5d_pct", "r20d_pct", "r1d_vs_spy_pp", "r5d_vs_spy_pp",
            "r20d_vs_spy_pp", "volume", "vol20_avg", "vol_x", "rsi14", "high20", "from_high20_pct"]
    wcsv(f"{OUT}/metrics.csv", sorted(ms, key=lambda x: x["ticker"]), cols)

    # macro.csv
    mac = []
    tr = treasury("daily_treasury_yield_curve")
    if len(tr) >= 2:
        (d1, a), (d0, b) = tr[-1], tr[-2]
        for col, name in (("2 Yr", "US2Y"), ("10 Yr", "US10Y"), ("30 Yr", "US30Y"), ("3 Mo", "US3M")):
            try:
                v1, v0 = float(a[col]), float(b[col])
                mac.append({"item": name, "date": d1, "value": v1, "prev_date": d0, "prev_value": v0, "change": round((v1 - v0) * 100, 1), "unit": "% (change=bp)", "source": "US Treasury Par Yield Curve"})
            except Exception:
                pass
        try:
            mac.append({"item": "US2s10s", "date": d1, "value": round(float(a["10 Yr"]) - float(a["2 Yr"]), 2), "prev_date": d0,
                        "prev_value": round(float(b["10 Yr"]) - float(b["2 Yr"]), 2), "unit": "%p", "source": "US Treasury"})
        except Exception:
            pass
    rr = treasury("daily_treasury_real_yield_curve")
    if len(rr) >= 2:
        (d1, a), (d0, b) = rr[-1], rr[-2]
        try:
            v1, v0 = float(a["10 YR"] if "10 YR" in a else a["10 Yr"]), float(b["10 YR"] if "10 YR" in b else b["10 Yr"])
            mac.append({"item": "US10Y_REAL", "date": d1, "value": v1, "prev_date": d0, "prev_value": v0, "change": round((v1 - v0) * 100, 1), "unit": "% (change=bp)", "source": "US Treasury Par Real Yield Curve"})
        except Exception:
            pass
    for name, sym in MACRO_Y.items():
        rows = yahoo_daily(sym, "3mo")
        if rows and len(rows) >= 2:
            note("macro_yahoo", True)
            v1, v0 = rows[-1][4], rows[-2][4]
            if sym in ("^TNX", "^TYX"):
                v1, v0 = v1 / 10 if v1 > 20 else v1, v0 / 10 if v0 > 20 else v0
            mac.append({"item": name, "date": rows[-1][0], "value": round(v1, 4), "prev_date": rows[-2][0], "prev_value": round(v0, 4),
                        "change": round((v1 / v0 - 1) * 100, 2) if v0 else None, "unit": "level (change=%)", "source": "Yahoo Finance " + sym})
        else:
            note("macro_yahoo", False)
        time.sleep(0.2)
    wcsv(f"{OUT}/macro.csv", mac, ["item", "date", "value", "prev_date", "prev_value", "change", "unit", "source"])

    # 전 종목 스냅샷 (나스닥 스크리너는 직전 정규장 종가 기준)
    snap = snapshot()
    snap_date = None
    if snap:
        # 스크리너는 갱신이 늦을 수 있음 → 일봉 종가와 맞춰 실제 날짜를 찾는다
        votes = {}
        for x in snap:
            rows = hist.get(x["ticker"])
            if not rows or not x["close"] or x["close"] != x["close"]:
                continue
            for d, *_, c, _v in rows[-3:]:
                if abs(c - x["close"]) < 0.006:
                    votes[d] = votes.get(d, 0) + 1
        if votes:
            snap_date = max(votes, key=votes.get)
        status["snapshot_vote"] = votes
    if snap and snap_date:
        os.makedirs(f"{OUT}/snapshots", exist_ok=True)
        wcsv(f"{OUT}/snapshots/{snap_date}.csv", snap, ["ticker", "close", "chg_pct", "volume", "market_cap", "name"])
        keep = sorted(os.listdir(f"{OUT}/snapshots"))[-40:]
        for fn in os.listdir(f"{OUT}/snapshots"):
            if fn not in keep:
                os.remove(f"{OUT}/snapshots/{fn}")

    status.update({"generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "base_trading_date_et": base_date,
                   "tickers_requested": len(tickers), "tickers_ok": len(hist), "snapshot_rows": len(snap), "snapshot_date": snap_date,
                   "stale_tickers": [m["ticker"] for m in ms if m["stale"]][:50]})
    json.dump(status, open(f"{OUT}/status.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in status.items() if k != "missing"}, ensure_ascii=False))
    print("missing", status["missing"][:60])


if __name__ == "__main__":
    main()
