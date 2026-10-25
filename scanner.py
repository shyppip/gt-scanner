"""
Golden Trend 매수신호 스캐너 (업비트 원화마켓 + 바이낸스 USDT 마켓)

규칙 (트레이딩뷰 Golden Trend 지표와 동일)
- 50봉 단순이동평균(SMA)이 200봉 SMA를 아래에서 위로 돌파 = 매수 신호
- 확정된 봉(마감된 봉)에서만 판단. 진행 중인 봉은 쓰지 않음
- 기본 4시간봉

실행할 때마다 '가장 최근에 마감된 봉'에서 막 교차가 일어난 코인만 텔레그램으로 보냄.
4시간마다(봉 마감 직후) 한 번씩 돌리면 같은 신호가 두 번 오지 않음.
"""
import os
import sys
import time
import json
import html
from datetime import datetime, timezone, timedelta

import requests

# ───────── 설정 (환경변수로 바꿀 수 있음) ─────────
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL = os.environ.get("GT_INTERVAL", "4h")          # 4h / 1h / 15m / 1d
FAST = int(os.environ.get("GT_FAST", "50"))
SLOW = int(os.environ.get("GT_SLOW", "200"))
SELL_ALERTS = os.environ.get("GT_SELL_ALERTS", "false").lower() == "true"  # 데드크로스(청산)도 받을지
EXCHANGES = os.environ.get("GT_EXCHANGES", "upbit,binance").split(",")
HEARTBEAT = os.environ.get("GT_HEARTBEAT", "false").lower() == "true"     # 신호 없어도 '검사 완료' 보내기
DRY_RUN = os.environ.get("GT_DRY_RUN", "false").lower() == "true"         # 텔레그램 대신 화면에만 출력

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "golden-trend-scanner/1.0"}
NEED = SLOW + 1          # 교차 판단에 필요한 마감봉 수 (직전봉 SMA200 + 현재봉 SMA200)

UPBIT_UNIT = {"1m": "minutes/1", "15m": "minutes/15", "1h": "minutes/60", "4h": "minutes/240", "1d": "days"}
INTERVAL_SEC = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR", "EURI", "USDE", "XUSD", "BFUSD", "PAXG", "USD1", "RLUSD"}


def log(*a):
    print(datetime.now(KST).strftime("%H:%M:%S"), *a, flush=True)


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            time.sleep(1.0 * (i + 1))
    return None


def sma(vals, n, end):
    """vals[end-n:end] 평균"""
    return sum(vals[end - n:end]) / n


def check_cross(closes):
    """closes = 마감된 봉 종가 리스트(오래된 → 최근). 반환: 'buy' / 'sell' / None, 지표값"""
    if len(closes) < NEED:
        return None, None
    L = len(closes)
    f_now, s_now = sma(closes, FAST, L), sma(closes, SLOW, L)
    f_prev, s_prev = sma(closes, FAST, L - 1), sma(closes, SLOW, L - 1)
    info = {"fast": f_now, "slow": s_now, "close": closes[-1]}
    if f_prev <= s_prev and f_now > s_now:
        return "buy", info
    if f_prev >= s_prev and f_now < s_now:
        return "sell", info
    return None, info


# ───────── 업비트 ─────────
def upbit_markets():
    data = get("https://api.upbit.com/v1/market/all", {"isDetails": "true"})
    out = []
    for m in data:
        if not m["market"].startswith("KRW-"):
            continue
        warn = m.get("market_warning") == "CAUTION" or (m.get("market_event") or {}).get("warning")
        out.append({"code": m["market"], "name": m.get("korean_name", ""), "warn": bool(warn)})
    return out


def upbit_closes(market):
    """마감된 봉만, 오래된 → 최근 순. 업비트는 한 번에 200개까지라 두 번 나눠 받음."""
    unit = UPBIT_UNIT[INTERVAL]
    url = f"https://api.upbit.com/v1/candles/{unit}"
    rows = get(url, {"market": market, "count": 200}) or []
    if len(rows) == 200:
        last_utc = rows[-1]["candle_date_time_utc"]
        more = get(url, {"market": market, "count": 200, "to": last_utc + "Z"}) or []
        rows += more
        time.sleep(0.12)
    # 최신순으로 오므로 뒤집기. 첫 번째(가장 최신)가 진행 중인 봉인지 시간으로 판단
    now = datetime.now(timezone.utc)
    sec = INTERVAL_SEC[INTERVAL]
    closed = []
    seen = set()
    for r in rows:
        t = r["candle_date_time_utc"]
        if t in seen:
            continue
        seen.add(t)
        start = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        if (start + timedelta(seconds=sec)) <= now:
            closed.append((start, float(r["trade_price"])))
    closed.sort(key=lambda x: x[0])
    return [c for _, c in closed], (closed[-1][0] if closed else None)


# ───────── 바이낸스 ─────────
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com"]
_bn_host = None


def bn_get(path, params=None):
    global _bn_host
    hosts = [_bn_host] if _bn_host else BINANCE_HOSTS
    last = None
    for h in hosts:
        try:
            res = get(h + path, params, tries=2)
            _bn_host = h
            return res
        except Exception as e:
            last = e
    raise last


def binance_markets():
    info = bn_get("/api/v3/exchangeInfo", {"permissions": "SPOT"})
    out = []
    for s in info["symbols"]:
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        base = s["baseAsset"]
        if base in STABLES or base.endswith(("UP", "DOWN", "BULL", "BEAR")) and len(base) > 4:
            continue
        out.append({"code": s["symbol"], "name": base, "warn": False})
    return out


def binance_closes(symbol):
    rows = bn_get("/api/v3/klines", {"symbol": symbol, "interval": INTERVAL, "limit": NEED + 5})
    now_ms = time.time() * 1000
    closed = [(int(r[0]), float(r[4])) for r in rows if int(r[6]) < now_ms]  # r[6] = 마감 시각
    return [c for _, c in closed], (datetime.fromtimestamp(closed[-1][0] / 1000, timezone.utc) if closed else None)


# ───────── 텔레그램 ─────────
def tg_send(text):
    if DRY_RUN or not TG_TOKEN or not TG_CHAT:
        print("\n----- 텔레그램 메시지 (미발송) -----\n" + text + "\n")
        return
    for chunk in split_msg(text):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=15)
        if not r.ok:
            log("텔레그램 전송 실패:", r.status_code, r.text[:200])
        time.sleep(0.5)


def split_msg(text, limit=3800):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def fmt_price(p, krw):
    if krw:
        return f"{p:,.0f}원" if p >= 100 else f"{p:,.4g}원"
    return f"${p:,.4f}" if p < 1 else f"${p:,.2f}"


def tv_link(ex, code):
    sym = f"UPBIT:{code.replace('KRW-', '')}KRW" if ex == "upbit" else f"BINANCE:{code}"
    tf = {"4h": "240", "1h": "60", "15m": "15", "1d": "D", "1m": "1"}[INTERVAL]
    return f"https://www.tradingview.com/chart/?symbol={sym}&interval={tf}"


# ───────── 메인 ─────────
def scan(ex):
    if ex == "upbit":
        markets, fetch, krw, label = upbit_markets(), upbit_closes, True, "업비트"
    else:
        markets, fetch, krw, label = binance_markets(), binance_closes, False, "바이낸스"
    log(f"{label}: {len(markets)}개 검사 시작")
    hits, errors, short, bar_time = [], 0, 0, None
    for i, m in enumerate(markets):
        try:
            closes, last_bar = fetch(m["code"])
            if last_bar and (bar_time is None or last_bar > bar_time):
                bar_time = last_bar
            sig, info = check_cross(closes)
            if info is None:
                short += 1
            if sig == "buy" or (sig == "sell" and SELL_ALERTS):
                hits.append({**m, "sig": sig, **info, "ex": ex, "krw": krw})
                log(f"  ★ {sig} {m['code']}")
        except Exception as e:
            errors += 1
            if errors <= 3:
                log(f"  오류 {m['code']}: {e}")
        time.sleep(0.11 if ex == "upbit" else 0.05)
    log(f"{label}: 신호 {len(hits)}개, 데이터 부족 {short}개, 오류 {errors}개")
    return {"label": label, "count": len(markets), "hits": hits, "errors": errors, "short": short, "bar": bar_time}


def main():
    started = datetime.now(KST)
    results = []
    for ex in [e.strip() for e in EXCHANGES if e.strip()]:
        try:
            results.append(scan(ex))
        except Exception as e:
            log(f"{ex} 전체 실패: {e}")
            results.append({"label": "업비트" if ex == "upbit" else "바이낸스", "count": 0, "hits": [],
                            "errors": -1, "short": 0, "bar": None, "fail": str(e)[:150]})

    all_hits = [h for r in results for h in r["hits"]]
    lines = []
    if all_hits:
        buys = [h for h in all_hits if h["sig"] == "buy"]
        sells = [h for h in all_hits if h["sig"] == "sell"]
        lines.append(f"🟡 <b>Golden Trend 신호</b> ({INTERVAL}봉)")
        bar = max((r["bar"] for r in results if r["bar"]), default=None)
        if bar:
            b0 = bar.astimezone(KST)
            b1 = b0 + timedelta(seconds=INTERVAL_SEC[INTERVAL])
            lines.append(f"기준봉: {b0:%m/%d %H:%M} ~ {b1:%H:%M} KST (마감 확정)")
        lines.append("")
        for title, group in (("📈 매수 신호 (50선이 200선 위로)", buys), ("📉 청산 신호 (50선이 200선 아래로)", sells)):
            if not group:
                continue
            lines.append(f"<b>{title}</b>")
            for h in group:
                gap = (h["fast"] / h["slow"] - 1) * 100
                warn = " ⚠️유의종목" if h["warn"] else ""
                name = html.escape(h["name"])
                lines.append(f"• [{'업비트' if h['ex'] == 'upbit' else '바이낸스'}] "
                             f"<a href=\"{tv_link(h['ex'], h['code'])}\">{html.escape(h['code'])}</a> {name}{warn}")
                lines.append(f"   종가 {fmt_price(h['close'], h['krw'])} · 50/200 간격 {gap:+.2f}%")
            lines.append("")
    for r in results:
        if r.get("fail"):
            lines.append(f"⚠️ {r['label']} 데이터를 못 받았어요: {html.escape(r['fail'])}")
    if all_hits or HEARTBEAT or any(r.get("fail") for r in results):
        if not all_hits:
            lines.insert(0, f"✅ Golden Trend 검사 완료 ({INTERVAL}봉) — 새 신호 없음")
        summary = " / ".join(f"{r['label']} {r['count']}개" for r in results)
        lines.append(f"<i>{started:%m/%d %H:%M} KST · {summary} 검사</i>")
        lines.append("<i>투자 권유가 아닌 신호 알림입니다.</i>")
        tg_send("\n".join(lines))
    else:
        log("새 신호 없음 — 텔레그램 안 보냄")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        tg_send("✅ Golden Trend 알림 봇 연결 테스트 성공! 이제 신호가 뜨면 여기로 알려드릴게요.")
    else:
        main()
