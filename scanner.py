"""
Golden Trend 매수신호 스캐너 (업비트 원화마켓 + 바이낸스 USDT 마켓)

규칙 (트레이딩뷰 Golden Trend 지표와 동일)
- 20봉 지수이동평균(EMA)이 200봉 EMA 위로 올라간 상승 구간에서, MACD 히스토그램 > 0 그리고 종가 > 일목 구름이 처음 맞는 봉 = 매수 신호
  (트레이딩뷰 GT1-S·GT2 올인원과 같은 규칙, 2026-10-27 변경. 청산은 20선이 200선 아래로)
- 확정된 봉(마감된 봉)에서만 판단. 진행 중인 봉은 쓰지 않음

실행 방법
- python scanner.py          → 4시간봉 검사. 막 마감된 봉에서 새 신호가 난 코인만 알림 (4시간마다)
- python scanner.py report   → 정기 리포트 (KST 00시·09시)
                               ① 4시간봉 새 신호(09시처럼 4시간봉이 막 마감된 때만)
                               ② 1시간봉 새 신호
                               ③ 4시간봉 골든크로스 임박 코인 목록
                               ④ 검사 요약 (신호가 없어도 항상 보냄)
- python scanner.py test     → 텔레그램 연결 테스트
"""
import os
import sys
import math
import time
import html
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

# ───────── 설정 (환경변수로 바꿀 수 있음) ─────────
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL = os.environ.get("GT_INTERVAL", "4h")          # 기본 검사 봉: 4h / 1h / 15m / 1d
FAST = int(os.environ.get("GT_FAST", "20"))
SLOW = int(os.environ.get("GT_SLOW", "200"))
SELL_ALERTS = os.environ.get("GT_SELL_ALERTS", "false").lower() == "true"  # 데드크로스(청산)도 받을지
EXCHANGES = os.environ.get("GT_EXCHANGES", "upbit,binance").split(",")
HEARTBEAT = os.environ.get("GT_HEARTBEAT", "true").lower() == "true"     # 소식 없어도 매시간 한 통 보내기
DRY_RUN = os.environ.get("GT_DRY_RUN", "false").lower() == "true"         # 텔레그램 대신 화면에만 출력
NEAR_GAP = float(os.environ.get("GT_NEAR_GAP", "1.5"))     # 임박 목록: 20선이 200선 아래 몇 % 이내
NEAR_BARS = int(os.environ.get("GT_NEAR_BARS", "12"))      # 임박 목록: 지금 속도면 몇 봉 안에 닿을지
NEAR_MAX = int(os.environ.get("GT_NEAR_MAX", "10"))        # 임박 목록: 거래소별 최대 개수
TREND_TOP = int(os.environ.get("GT_TREND_TOP", "120"))     # 15분봉 추세전환 검사: 거래대금 상위 몇 개까지 (거래소별)
TREND_BARS = int(os.environ.get("GT_TREND_BARS", "5"))     # 15분봉 추세전환: 최근 몇 봉 안의 교차를 볼지 (5봉 = 75분)
STRONG = int(os.environ.get("GT_STRONG", "80"))            # 신호 강도 🟢 기준 (% 이상)
MIDDLE = int(os.environ.get("GT_MIDDLE", "50"))            # 신호 강도 🟡 기준 (% 이상), 그 아래는 🔴

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "golden-trend-scanner/1.1"}
NEED = SLOW * 2          # EMA200이 트레이딩뷰 값과 거의 같아지려면 400봉 이상 필요

UPBIT_UNIT = {"1m": "minutes/1", "15m": "minutes/15", "1h": "minutes/60", "4h": "minutes/240", "1d": "days"}
INTERVAL_SEC = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
TV_TF = {"4h": "240", "1h": "60", "15m": "15", "1d": "D", "1m": "1"}
LABEL = {"upbit": "업비트", "binance": "바이낸스"}
EX_ICON = {"upbit": "[업]", "binance": "[바]"}   # 거래소 표시: [업] 업비트 · [바] 바이낸스

STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR", "EURI", "USDE", "XUSD", "BFUSD", "PAXG", "USD1", "RLUSD"}


def log(*a):
    print(datetime.now(KST).strftime("%H:%M:%S"), *a, flush=True)


class RateLimit:
    """여러 스레드가 함께 써도 초당 요청 수를 넘지 않게 막는 장치"""
    def __init__(self, per_sec):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


LIMITS = {"upbit": RateLimit(8), "binance": RateLimit(20)}


def get(url, params=None, tries=4):
    lim = LIMITS["upbit"] if "upbit" in url else LIMITS["binance"]
    for i in range(tries):
        lim.wait()
        try:
            r = requests.get(url, params=params, headers=UA, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1.0 * (i + 1))
    return None


def ema_list(vals, n):
    """트레이딩뷰 ta.ema와 같은 지수이동평균 (첫 값에서 시작)"""
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


class Bars(list):
    """종가 목록 + 고가·저가 (일목 구름 계산용)"""
    highs = lows = None


def buy_filter_ok(closes):
    """매수 필터 ok(i): 봉 i에서 [MACD(12,26,9) 히스토그램 > 0 그리고 종가 > 일목 구름]. 고가·저가가 없으면 MACD만"""
    m = [x - y for x, y in zip(ema_list(closes, 12), ema_list(closes, 26))]
    hist = [x - y for x, y in zip(m, ema_list(m, 9))]
    hs, ls = getattr(closes, "highs", None), getattr(closes, "lows", None)

    def ok(i):
        if hist[i] <= 0:
            return False
        if not (hs and ls):
            return True
        j = i - 25                                       # 구름은 25봉 앞으로 그려짐
        if j < 51:
            return False
        mid = lambda n: (max(hs[j - n + 1:j + 1]) + min(ls[j - n + 1:j + 1])) / 2
        return closes[i] > max((mid(9) + mid(26)) / 2, mid(52))
    return ok


def rsi(closes, n=14):
    """와일더 방식 RSI (트레이딩뷰 ta.rsi와 같은 계산)"""
    if len(closes) <= n:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def analyze(closes, vols=None):
    """closes = 마감된 봉 종가(오래된 → 최근). 교차 여부·임박 정보·RSI·거래량 배수를 함께 계산"""
    if len(closes) < NEED:
        return None
    L = len(closes)
    EF, ES = ema_list(closes, FAST), ema_list(closes, SLOW)
    f_now, s_now, f_prev, s_prev = EF[-1], ES[-1], EF[-2], ES[-2]
    sig = None
    if f_now > s_now:
        # 상승 구간에서 매수 필터가 처음 맞는 봉 = 매수 (GT1-S와 같음)
        ok = buy_filter_ok(closes)
        if ok(L - 1):
            j = L - 2
            while j >= SLOW and EF[j] > ES[j] and not ok(j):
                j -= 1
            if j < SLOW or not (EF[j] > ES[j]):
                sig = "buy"
    elif f_prev >= s_prev and f_now < s_now:
        sig = "sell"
    gap = (f_now / s_now - 1) * 100
    closing = (f_now - f_prev) - (s_now - s_prev)     # 한 봉 동안 두 선 간격이 좁혀진 양 (+면 20선이 따라붙는 중)
    eta = (s_now - f_now) / closing if (f_now < s_now and closing > 0) else None
    vol_x = None
    if vols and len(vols) >= 21:
        base = sum(vols[-21:-1]) / 20
        vol_x = vols[-1] / base if base > 0 else None
    return {"sig": sig, "fast": f_now, "slow": s_now, "close": closes[-1], "gap": gap, "eta": eta,
            "rsi": rsi(closes[-150:]), "vol_x": vol_x, "fast_up": f_now > f_prev}


def light(a):
    """한 봉 기준 신호등: 🟢 20선이 200선 위 / 🟡 아래지만 교차 임박 / 🔴 아래 / ⚪ 데이터 부족"""
    if a is None:
        return "⚪"
    if a["fast"] > a["slow"]:
        return "🟢"
    if a["eta"] is not None and a["gap"] >= -NEAR_GAP and a["eta"] <= NEAR_BARS:
        return "🟡"
    return "🔴"


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


def upbit_closes(market, iv):
    """마감된 봉만, 오래된 → 최근 순. 업비트는 한 번에 200개까지라 두 번 나눠 받음."""
    url = f"https://api.upbit.com/v1/candles/{UPBIT_UNIT[iv]}"
    rows = get(url, {"market": market, "count": 200}) or []
    for _ in range(2):                                   # EMA200 정확도를 위해 최대 600봉
        if len(rows) % 200 or not rows:
            break
        more = get(url, {"market": market, "count": 200, "to": rows[-1]["candle_date_time_utc"] + "Z"}) or []
        if not more:
            break
        rows += more
    now = datetime.now(timezone.utc)
    sec = INTERVAL_SEC[iv]
    closed, seen = [], set()
    for r in rows:
        t = r["candle_date_time_utc"]
        if t in seen:
            continue
        seen.add(t)
        start = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        if start + timedelta(seconds=sec) <= now:      # 진행 중인 봉 제외
            closed.append((start, float(r["trade_price"]), float(r["candle_acc_trade_volume"]),
                           float(r["high_price"]), float(r["low_price"])))
    closed.sort(key=lambda x: x[0])
    cl = Bars(x[1] for x in closed)
    cl.highs, cl.lows = [x[3] for x in closed], [x[4] for x in closed]
    return cl, [x[2] for x in closed], (closed[-1][0] if closed else None)


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
        if base in STABLES or (base.endswith(("UP", "DOWN", "BULL", "BEAR")) and len(base) > 4):
            continue
        out.append({"code": s["symbol"], "name": base, "warn": False})
    return out


def binance_closes(symbol, iv):
    rows = bn_get("/api/v3/klines", {"symbol": symbol, "interval": iv, "limit": 600})
    now_ms = time.time() * 1000
    closed = [(int(r[0]), float(r[4]), float(r[5]), float(r[2]), float(r[3])) for r in rows if int(r[6]) < now_ms]  # r[6] = 마감 시각
    cl = Bars(x[1] for x in closed)
    cl.highs, cl.lows = [x[3] for x in closed], [x[4] for x in closed]
    return (cl, [x[2] for x in closed],
            datetime.fromtimestamp(closed[-1][0] / 1000, timezone.utc) if closed else None)


def upbit_top(markets, n):
    """업비트 원화마켓 24시간 거래대금 상위 n개"""
    codes = [m["code"] for m in markets]
    vol = {}
    for i in range(0, len(codes), 100):
        for t in get("https://api.upbit.com/v1/ticker", {"markets": ",".join(codes[i:i + 100])}) or []:
            vol[t["market"]] = float(t.get("acc_trade_price_24h") or 0)
    return sorted(markets, key=lambda m: -vol.get(m["code"], 0))[:n]


def binance_top(markets, n):
    """바이낸스 USDT 마켓 24시간 거래대금(USDT) 상위 n개"""
    vol = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in bn_get("/api/v3/ticker/24hr") or []}
    return sorted(markets, key=lambda m: -vol.get(m["code"], 0))[:n]


def trend_state(a):
    """4시간봉 큰 추세 판정: 상승 추세 / 하락 추세 / 횡보 유지"""
    if a is None:
        return "⚪", "데이터 부족"
    if a["fast"] > a["slow"] and a["gap"] >= 1.0 and a.get("fast_up"):
        return "📈", "상승 추세"
    if a["fast"] < a["slow"] and a["gap"] <= -1.0 and not a.get("fast_up"):
        return "📉", "하락 추세"
    return "➖", "횡보 유지"


def recent_cross(closes, vols, lookback):
    """최근 lookback봉 안에서 일어난 마지막 20/200 EMA 교차. (방향, 몇 봉 전, 교차봉 거래량 배수) 또는 None"""
    L = len(closes)
    if L < NEED + lookback:
        return None
    EF, ES = ema_list(closes, FAST), ema_list(closes, SLOW)
    for back in range(0, lookback):
        end = L - back
        f_now, s_now = EF[end - 1], ES[end - 1]
        f_prev, s_prev = EF[end - 2], ES[end - 2]
        if f_prev <= s_prev and f_now > s_now:
            d = "up"
        elif f_prev >= s_prev and f_now < s_now:
            d = "down"
        else:
            continue
        # 교차 뒤 지금까지 같은 쪽을 유지하는지
        held = (EF[-1] > ES[-1]) == (d == "up")
        base = sum(vols[end - 21:end - 1]) / 20 if vols and end >= 21 else 0
        vx = vols[end - 1] / base if base > 0 else None
        return {"dir": d, "back": back, "held": held, "vol_x": vx}
    return None


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


def tv_link(ex, code, iv):
    sym = f"UPBIT:{code.replace('KRW-', '')}KRW" if ex == "upbit" else f"BINANCE:{code}"
    return f"https://www.tradingview.com/chart/?symbol={sym}&interval={TV_TF[iv]}"


def coin_line(h, iv, mark="▲"):
    """한 줄 요약: ▲ [업] BTC 비트코인"""
    warn = " ⚠️" if h["warn"] else ""
    ex = EX_ICON[h["ex"]]
    code = h["code"].replace("KRW-", "").replace("USDT", "")
    name = "" if h["name"] == code else " " + html.escape(h["name"])
    return f"{mark} {ex} <a href=\"{tv_link(h['ex'], h['code'], iv)}\">{html.escape(code)}</a>{name}{warn}"


def lights_txt(lights):
    """1h·4h·1d 신호등을 한 덩어리로: 🟢🟡🟢"""
    return "".join(lights.get(tf, "⚪") for tf in TF_ORDER)


def bar_range(bar, iv):
    if not bar:
        return ""
    b0 = bar.astimezone(KST)
    b1 = b0 + timedelta(seconds=INTERVAL_SEC[iv])
    return f"{b0:%m/%d %H:%M} ~ {b1:%H:%M}"


# ───────── 검사 ─────────
def scan(ex, iv, markets=None):
    """한 거래소·한 봉 기준으로 전체 코인을 분석. 코인별 결과 목록을 돌려줌"""
    if markets is None:
        markets = upbit_markets() if ex == "upbit" else binance_markets()
    fetch = upbit_closes if ex == "upbit" else binance_closes
    log(f"{LABEL[ex]} {iv}: {len(markets)}개 검사 시작")
    rows, errors, short, bar_time = [], 0, 0, None

    def one(m):
        try:
            return m, fetch(m["code"], iv), None
        except Exception as e:
            return m, None, e

    # 여러 개를 동시에 받되, 초당 요청 수는 RateLimit이 지켜줌
    with ThreadPoolExecutor(max_workers=4 if ex == "upbit" else 8) as pool:
        for m, res, err in pool.map(one, markets):
            if err is not None:
                errors += 1
                if errors <= 3:
                    log(f"  오류 {m['code']}: {err}")
                continue
            closes, vols, last_bar = res
            if last_bar and (bar_time is None or last_bar > bar_time):
                bar_time = last_bar
            a = analyze(closes, vols)
            if a is None:
                short += 1
                continue
            rows.append({**m, **a, "ex": ex, "krw": ex == "upbit"})
            if a["sig"]:
                log(f"  ★ {iv} {a['sig']} {m['code']}")
    log(f"{LABEL[ex]} {iv}: 분석 {len(rows)}개, 데이터 부족 {short}개, 오류 {errors}개")
    res = {"ex": ex, "iv": iv, "count": len(markets), "rows": rows, "errors": errors, "bar": bar_time}
    if markets and errors > len(markets) / 2:
        res["fail"] = f"{len(markets)}개 중 {errors}개 실패"
    return res


def scan_all(iv, market_cache):
    out = []
    for ex in [e.strip() for e in EXCHANGES if e.strip()]:
        try:
            if ex not in market_cache:
                market_cache[ex] = upbit_markets() if ex == "upbit" else binance_markets()
            out.append(scan(ex, iv, market_cache[ex]))
        except Exception as e:
            log(f"{ex} {iv} 전체 실패: {e}")
            out.append({"ex": ex, "iv": iv, "count": 0, "rows": [], "errors": -1, "bar": None, "fail": str(e)[:150]})
    return out


def signal_block(results, iv, title):
    """새 교차 신호 블록. 한 코인 = 한 줄. 신호가 없으면 빈 리스트"""
    hits = [h for r in results for h in r["rows"] if h["sig"] == "buy" or (h["sig"] == "sell" and SELL_ALERTS)]
    if not hits:
        return [], 0
    lines = [f"<b>{title}</b>"]
    for kind, mark in (("buy", "▲"), ("sell", "▼")):
        group = [h for h in hits if h["sig"] == kind]
        group.sort(key=lambda h: -(h["strength"][1] if h.get("strength") else 0))
        for h in group:
            parts = [coin_line(h, iv, mark)]
            if h.get("lights"):
                parts.append(lights_txt(h["lights"]))
            if h.get("strength") and kind == "buy":
                icon, pct, _ = h["strength"]
                parts.append(f"{icon}{pct}%")
            lines.append(" · ".join(parts))
    return lines, len([h for h in hits if h["sig"] == "buy"])


TF_ORDER = ("1h", "4h", "1d")


def enrich(results, iv, known):
    """신호가 난 코인에 1시간·4시간·일봉 신호등과 신호 강도를 붙임.
    known = {봉: {(거래소, 코드): 분석결과}} — 이미 전체 검사한 봉은 다시 받지 않음"""
    for r in results:
        for h in r["rows"]:
            if h["sig"] != "buy" and not (h["sig"] == "sell" and SELL_ALERTS):
                continue
            lights, other = {}, {}
            for tf in TF_ORDER:
                if tf == iv:
                    a = h
                elif (h["ex"], h["code"]) in known.get(tf, {}):
                    a = known[tf][(h["ex"], h["code"])]
                else:
                    try:
                        fetch = upbit_closes if h["ex"] == "upbit" else binance_closes
                        c, v, _ = fetch(h["code"], tf)
                        a = analyze(c, v)
                    except Exception:
                        a = None
                lights[tf] = light(a)
                if tf != iv:
                    other[tf] = a
            h["lights"] = lights
            h["strength"] = strength(h, iv, lights)


def strength(h, iv, lights):
    """신호 강도 (100점 만점 %)
    일봉 추세 30 · 다른 봉 추세 20 · 거래량 20 · RSI 20 · 20선 상승+가격이 200선 위 10"""
    score, why = 0, []
    big = {"15m": "4h", "1h": "1d", "4h": "1d", "1d": "4h"}[iv]   # 더 큰 봉
    small = [tf for tf in TF_ORDER if tf not in (iv, big)][0]  # 나머지 봉
    pts = {"🟢": 1.0, "🟡": 0.5, "🔴": 0.0, "⚪": 0.5}
    name = {"1d": "일봉", "4h": "4h", "1h": "1h"}
    score += 30 * pts[lights[big]]
    score += 20 * pts[lights[small]]
    for tf in (big, small):
        if lights[tf] == "🔴":
            why.append(f"{name[tf]} 아직 하락")
        elif lights[tf] == "🟡":
            why.append(f"{name[tf]} 교차 직전")
    vx = h.get("vol_x")
    if vx is None:
        score += 10
    elif vx >= 2:
        score += 20
    elif vx >= 1.5:
        score += 15
    elif vx >= 1:
        score += 10
    elif vx >= 0.8:
        score += 5
        why.append(f"거래량 평소 수준({vx:.1f}배)")
    else:
        why.append(f"거래량 약함({vx:.1f}배)")
    r = h.get("rsi")
    if r is None:
        score += 10
    elif 50 <= r < 70:
        score += 20
    elif 70 <= r < 75 or 45 <= r < 50:
        score += 10
    elif r >= 75:
        why.append(f"RSI 과열({r:.0f})")
    else:
        score += 5
        why.append(f"RSI 약함({r:.0f})")
    if h.get("fast_up"):
        score += 5
    if h["close"] > h["slow"]:
        score += 5
    else:
        why.append("가격이 200선 아래")
    pct = round(score)
    icon = "🟢" if pct >= STRONG else ("🟡" if pct >= MIDDLE else "🔴")
    return icon, pct, why


def market_light(res4):
    """시장 신호등: 4시간봉 20선이 200선 위인 코인 비율 + 비트코인 4시간봉"""
    rows = [h for r in res4 for h in r["rows"]]
    if not rows:
        return None
    pct = sum(1 for h in rows if h["fast"] > h["slow"]) / len(rows) * 100
    btc = next((h for h in rows if h["code"] in ("KRW-BTC", "BTCUSDT")), None)
    btc_up = btc is not None and btc["fast"] > btc["slow"]
    if pct >= 60 and btc_up:
        icon = "🟢"
    elif pct < 40 and not btc_up:
        icon = "🔴"
    else:
        icon = "🟡"
    return f"🚦 시장 {icon} (상승 {pct:.0f}% · BTC {light(btc)})"


def near_block(results, iv):
    """골든크로스 임박: 거래소별 한 줄. BTC(2봉)·SOL(5봉) 식"""
    lines = []
    for r in results:
        near = [h for h in r["rows"] if h["eta"] is not None and -NEAR_GAP <= h["gap"] < 0 and h["eta"] <= NEAR_BARS]
        near.sort(key=lambda h: h["eta"])
        if not near:
            continue
        items = []
        for h in near[:NEAR_MAX]:
            bars = max(1, math.ceil(h["eta"]))
            code = h["code"].replace("KRW-", "").replace("USDT", "")
            items.append(f"<a href=\"{tv_link(h['ex'], h['code'], iv)}\">{html.escape(code)}</a>({bars}봉)")
        more = f" 외 {len(near) - NEAR_MAX}" if len(near) > NEAR_MAX else ""
        lines.append(f"⏳ {EX_ICON[r['ex']]} 임박: " + "·".join(items) + more)
    return lines or ["⏳ 임박 코인 없음"]


def fail_lines(results):
    return [f"⚠️ {LABEL[r['ex']]} {r['iv']} 데이터를 못 받았어요: {html.escape(r['fail'])}" for r in results if r.get("fail")]


def summary_line(started, results):
    seen, parts = set(), []
    for r in results:
        if r["ex"] in seen:
            continue
        seen.add(r["ex"])
        parts.append(f"{r['count']}")
    return f"<i>{started:%H:%M} · {'+'.join(parts)}개 검사</i>"


def scan_lines():
    """4시간봉 새 신호 줄들 (+ 시장 신호등). 신호 없으면 '없음' 한 줄"""
    res = scan_all(INTERVAL, {})
    enrich(res, INTERVAL, {})
    sig, _ = signal_block(res, INTERVAL, f"📈 {INTERVAL}봉 매수 신호")
    lines = sig or [f"📈 {INTERVAL}봉 매수 신호: 없음"]
    ml = market_light(res) if INTERVAL == "4h" else None
    return ([ml] if ml else []) + lines + fail_lines(res), res


def trend_lines():
    """거래대금 상위 코인의 15분봉 추세전환 줄들. 한 코인 = 한 줄"""
    exs = [e.strip() for e in EXCHANGES if e.strip()]
    hits, checked, fails = [], 0, []
    for ex in exs:
        try:
            markets = upbit_markets() if ex == "upbit" else binance_markets()
            top = upbit_top(markets, TREND_TOP) if ex == "upbit" else binance_top(markets, TREND_TOP)
        except Exception as e:
            log(f"{ex} 목록 실패: {e}")
            fails.append(f"⚠️ {LABEL[ex]} 목록 실패")
            continue
        fetch = upbit_closes if ex == "upbit" else binance_closes
        log(f"{LABEL[ex]} 15m 추세전환: 상위 {len(top)}개 검사")

        def one(m):
            try:
                c, v, bar = fetch(m["code"], "15m")
                return m, recent_cross(c, v, TREND_BARS), analyze(c, v), bar, None
            except Exception as e:
                return m, None, None, None, e

        with ThreadPoolExecutor(max_workers=4 if ex == "upbit" else 8) as pool:
            for m, cr, a15, bar, err in pool.map(one, top):
                if err is not None or a15 is None:
                    continue
                checked += 1
                if not cr or not cr["held"]:
                    continue
                # 거짓 신호 줄이기: 교차 뒤 1봉 이상 유지했거나, 교차봉 거래량이 평소 1.5배 이상
                if cr["back"] < 1 and not (cr["vol_x"] and cr["vol_x"] >= 1.5):
                    continue
                hits.append({**m, **a15, "ex": ex, "krw": ex == "upbit", "cross": cr})
                log(f"  🔄 15m {cr['dir']} {m['code']} ({cr['back']}봉 전)")
    for h in hits:
        fetch = upbit_closes if h["ex"] == "upbit" else binance_closes
        lights, a4 = {}, None
        for tf in TF_ORDER:
            try:
                c, v, _ = fetch(h["code"], tf)
                a = analyze(c, v)
            except Exception:
                a = None
            lights[tf] = light(a)
            if tf == "4h":
                a4 = a
        h["lights"] = lights
        h["state4"] = trend_state(a4)
        h["strength"] = strength(h, "15m", lights) if h["cross"]["dir"] == "up" else None

    lines = [f"🔄 <b>15분봉 추세전환</b>"]
    if not hits:
        lines[0] += ": 없음"
    for kind, mark in (("up", "▲"), ("down", "▼")):
        group = [h for h in hits if h["cross"]["dir"] == kind]
        group.sort(key=lambda h: -(h["strength"][1] if h.get("strength") else 0))
        for h in group:
            icon4, name4 = h["state4"]
            if kind == "up":
                short = {"상승 추세": "상승 지속", "횡보 유지": "단기 상승", "하락 추세": "단기 반등", "데이터 부족": "보류"}[name4]
            else:
                short = {"상승 추세": "단기 조정", "횡보 유지": "단기 하락", "하락 추세": "하락 지속", "데이터 부족": "보류"}[name4]
            parts = [coin_line(h, "15m", mark), f"4h{icon4} {short}"]
            if h.get("strength"):
                si, sp, _ = h["strength"]
                parts.append(f"{si}{sp}%")
            lines.append(" · ".join(parts))
    return lines + fails, checked


def run_scan():
    """4시간봉만 검사해서 보냄 (수동 실행용)"""
    started = datetime.now(KST)
    lines, res = scan_lines()
    tg_send("\n".join([f"🕐 <b>{started:%m/%d %H:%M}</b>"] + lines + [summary_line(started, res)]))


def run_trend():
    """15분봉 추세전환만 검사해서 보냄 (수동 실행용)"""
    started = datetime.now(KST)
    lines, checked = trend_lines()
    tg_send("\n".join([f"🕐 <b>{started:%m/%d %H:%M}</b>"] + lines + [f"<i>{started:%H:%M} · 상위 {checked}개 검사</i>"]))


def run_auto():
    """15분마다 실행: 한 통으로 묶어서 보냄 (UTC 기준으로 할 일 결정)"""
    started = datetime.now(KST)
    now = datetime.now(timezone.utc)
    # 매시 00분 실행 = 본 검사, 15·30·45분 = 15분봉만.
    # 예약 실행이 몇 분 늦게 돌아도 헷갈리지 않게, 어떤 예약으로 실행됐는지(GT_SCHEDULE)를 먼저 봄
    sched = os.environ.get("GT_SCHEDULE", "")
    main_slot = sched.startswith("0 ") if sched else now.minute < 15
    hour = now.hour
    if main_slot and hour in (0, 15):              # KST 09시·00시
        run_report()
        return
    lines = [f"🕐 <b>{started:%m/%d %H:%M}</b>"]
    if main_slot and hour % 4 == 0:                # KST 13·17·21·01·05시: 4시간봉 검사도 함께
        sl, _ = scan_lines()
        lines += sl
    tl, checked = trend_lines()
    lines += tl
    has_news = any(l.startswith(("▲", "▼")) for l in lines)
    if has_news or HEARTBEAT:
        tg_send("\n".join(lines + [f"<i>{started:%H:%M} · 상위 {checked}개 검사</i>"]))
    else:
        log("새 소식 없음 — 텔레그램 안 보냄")


def run_report():
    """KST 00시·09시 정기 리포트: 항상 보냄"""
    started = datetime.now(KST)
    cache = {}
    res4 = scan_all("4h", cache)
    res1 = scan_all("1h", cache)
    known = {iv: {(h["ex"], h["code"]): h for r in rr for h in r["rows"]} for iv, rr in (("4h", res4), ("1h", res1))}
    enrich(res4, "4h", known)
    enrich(res1, "1h", known)

    lines = [f"🕘 <b>정기 리포트 {started:%m/%d %H:%M}</b>"]
    ml = market_light(res4)
    if ml:
        lines.append(ml)

    # ① 4시간봉 새 신호: 4시간봉이 막 마감된 때(봉 마감 후 1시간 이내)만
    bar4 = max((r["bar"] for r in res4 if r["bar"]), default=None)
    fresh4 = bar4 and (datetime.now(timezone.utc) - (bar4 + timedelta(hours=4))) < timedelta(hours=1)
    if fresh4:
        sig4, _ = signal_block(res4, "4h", "📈 4시간봉 매수 신호")
        lines += sig4 or ["📈 4시간봉 매수 신호: 없음"]

    # ② 1시간봉 새 신호
    sig1, _ = signal_block(res1, "1h", "🔵 1시간봉 매수 신호")
    lines += sig1 or ["🔵 1시간봉 매수 신호: 없음"]

    # ③ 4시간봉 임박 목록
    lines += near_block(res4, "4h")

    # ④ 15분봉 추세전환
    tl, _ = trend_lines()
    lines += tl

    lines += fail_lines(res4 + res1)
    lines.append(summary_line(started, res4))
    tg_send("\n".join(lines))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "test":
        tg_send("✅ Golden Trend 알림 봇 연결 테스트 성공! 이제 신호가 뜨면 여기로 알려드릴게요.")
    elif mode == "report":
        run_report()
    elif mode == "trend":
        run_trend()
    elif mode == "auto":
        run_auto()
    else:
        run_scan()
