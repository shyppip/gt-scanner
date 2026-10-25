"""
Golden Trend 매수신호 스캐너 (업비트 원화마켓 + 바이낸스 USDT 마켓)

규칙 (트레이딩뷰 Golden Trend 지표와 동일)
- 50봉 단순이동평균(SMA)이 200봉 SMA를 아래에서 위로 돌파 = 매수 신호
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
FAST = int(os.environ.get("GT_FAST", "50"))
SLOW = int(os.environ.get("GT_SLOW", "200"))
SELL_ALERTS = os.environ.get("GT_SELL_ALERTS", "false").lower() == "true"  # 데드크로스(청산)도 받을지
EXCHANGES = os.environ.get("GT_EXCHANGES", "upbit,binance").split(",")
HEARTBEAT = os.environ.get("GT_HEARTBEAT", "false").lower() == "true"     # 4시간 검사도 신호 없을 때 '검사 완료' 보내기
DRY_RUN = os.environ.get("GT_DRY_RUN", "false").lower() == "true"         # 텔레그램 대신 화면에만 출력
NEAR_GAP = float(os.environ.get("GT_NEAR_GAP", "1.5"))     # 임박 목록: 50선이 200선 아래 몇 % 이내
NEAR_BARS = int(os.environ.get("GT_NEAR_BARS", "12"))      # 임박 목록: 지금 속도면 몇 봉 안에 닿을지
NEAR_MAX = int(os.environ.get("GT_NEAR_MAX", "10"))        # 임박 목록: 거래소별 최대 개수

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "golden-trend-scanner/1.1"}
NEED = SLOW + 1          # 교차 판단에 필요한 마감봉 수 (직전봉 SMA200 + 현재봉 SMA200)

UPBIT_UNIT = {"1m": "minutes/1", "15m": "minutes/15", "1h": "minutes/60", "4h": "minutes/240", "1d": "days"}
INTERVAL_SEC = {"1m": 60, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
TV_TF = {"4h": "240", "1h": "60", "15m": "15", "1d": "D", "1m": "1"}
LABEL = {"upbit": "업비트", "binance": "바이낸스"}

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


def sma(vals, n, end):
    """vals[end-n:end] 평균"""
    return sum(vals[end - n:end]) / n


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
    f_now, s_now = sma(closes, FAST, L), sma(closes, SLOW, L)
    f_prev, s_prev = sma(closes, FAST, L - 1), sma(closes, SLOW, L - 1)
    sig = None
    if f_prev <= s_prev and f_now > s_now:
        sig = "buy"
    elif f_prev >= s_prev and f_now < s_now:
        sig = "sell"
    gap = (f_now / s_now - 1) * 100
    closing = (f_now - f_prev) - (s_now - s_prev)     # 한 봉 동안 두 선 간격이 좁혀진 양 (+면 50선이 따라붙는 중)
    eta = (s_now - f_now) / closing if (f_now < s_now and closing > 0) else None
    vol_x = None
    if vols and len(vols) >= 21:
        base = sum(vols[-21:-1]) / 20
        vol_x = vols[-1] / base if base > 0 else None
    return {"sig": sig, "fast": f_now, "slow": s_now, "close": closes[-1], "gap": gap, "eta": eta,
            "rsi": rsi(closes[-150:]), "vol_x": vol_x}


def light(a):
    """한 봉 기준 신호등: 🟢 50선이 200선 위 / 🟡 아래지만 교차 임박 / 🔴 아래 / ⚪ 데이터 부족"""
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
    if len(rows) == 200:
        last_utc = rows[-1]["candle_date_time_utc"]
        rows += get(url, {"market": market, "count": 200, "to": last_utc + "Z"}) or []
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
            closed.append((start, float(r["trade_price"]), float(r["candle_acc_trade_volume"])))
    closed.sort(key=lambda x: x[0])
    return [c for _, c, _ in closed], [v for _, _, v in closed], (closed[-1][0] if closed else None)


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
    rows = bn_get("/api/v3/klines", {"symbol": symbol, "interval": iv, "limit": NEED + 5})
    now_ms = time.time() * 1000
    closed = [(int(r[0]), float(r[4]), float(r[5])) for r in rows if int(r[6]) < now_ms]  # r[6] = 마감 시각
    return ([c for _, c, _ in closed], [v for _, _, v in closed],
            datetime.fromtimestamp(closed[-1][0] / 1000, timezone.utc) if closed else None)


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


def coin_line(h, iv):
    warn = " ⚠️유의종목" if h["warn"] else ""
    return (f"• [{LABEL[h['ex']]}] <a href=\"{tv_link(h['ex'], h['code'], iv)}\">{html.escape(h['code'])}</a> "
            f"{html.escape(h['name'])}{warn}")


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
    """새 교차 신호 블록. 신호가 없으면 빈 리스트"""
    hits = [h for r in results for h in r["rows"] if h["sig"] == "buy" or (h["sig"] == "sell" and SELL_ALERTS)]
    if not hits:
        return [], 0
    bar = max((r["bar"] for r in results if r["bar"]), default=None)
    lines = [f"<b>{title}</b>  <i>{bar_range(bar, iv)} 봉</i>"]
    for kind, head in (("buy", "📈 매수 (50선이 200선 위로)"), ("sell", "📉 청산 (50선이 200선 아래로)")):
        group = [h for h in hits if h["sig"] == kind]
        if not group:
            continue
        lines.append(head)
        for h in group:
            lines.append(coin_line(h, iv))
            if h.get("lights"):
                tl = "  ".join(f"{k} {v}{'(방금 교차)' if k == iv else ''}" for k, v in h["lights"].items())
                lines.append(f"   {tl}")
            extra = []
            if h.get("vol_x") is not None:
                extra.append(f"거래량 {h['vol_x']:.1f}배")
            if h.get("rsi") is not None:
                extra.append(f"RSI {h['rsi']:.0f}")
            lines.append(f"   종가 {fmt_price(h['close'], h['krw'])} · " + " · ".join(extra or [f"간격 {h['gap']:+.2f}%"]))
            if h.get("strength") and kind == "buy":
                icon, word, why = h["strength"]
                lines.append(f"   → 신호 강도 {icon} {word}" + (f" ({', '.join(why)})" if why else ""))
    lines.append("")
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
    """신호 강도: 다른 봉 추세·거래량·RSI를 점수로 묶음"""
    score, why = 0, []
    for tf in TF_ORDER:
        if tf == iv:
            continue
        if lights[tf] == "🟢":
            score += 2 if tf == "1d" else 1
        elif lights[tf] == "🟡":
            score += 1 if tf == "1d" else 0
            why.append(f"{tf} 교차 직전")
        elif lights[tf] == "🔴":
            why.append(f"{'일봉' if tf == '1d' else tf} 아직 하락")
    vx = h.get("vol_x")
    if vx is not None:
        if vx >= 1.5:
            score += 1
        elif vx < 0.8:
            score -= 1
            why.append(f"거래량 약함({vx:.1f}배)")
    r = h.get("rsi")
    if r is not None:
        if r >= 75:
            score -= 1
            why.append(f"RSI 과열({r:.0f})")
        elif 50 <= r < 75:
            score += 1
    if score >= 4:
        return "🟢", "강함", why
    if score >= 2:
        return "🟡", "보통", why
    return "🔴", "약함", why


def market_light(res4):
    """시장 신호등: 4시간봉 50선이 200선 위인 코인 비율 + 비트코인 4시간봉"""
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
    return f"🚦 시장 신호등 {icon}  4시간봉 상승 추세 코인 {pct:.0f}% · 비트코인 4h {light(btc)}"


def near_block(results, iv):
    """골든크로스 임박: 50선이 200선 바로 아래에서 빠르게 따라붙는 코인"""
    lines = [f"<b>⏳ {iv}봉 골든크로스 임박</b>  <i>(200선 아래 {NEAR_GAP}% 이내 · 지금 속도면 {NEAR_BARS}봉 안)</i>"]
    total = 0
    for r in results:
        near = [h for h in r["rows"] if h["eta"] is not None and -NEAR_GAP <= h["gap"] < 0 and h["eta"] <= NEAR_BARS]
        near.sort(key=lambda h: h["eta"])
        total += len(near)
        if not near:
            continue
        lines.append(f"{LABEL[r['ex']]} ({len(near)}개{' 중 ' + str(NEAR_MAX) + '개' if len(near) > NEAR_MAX else ''})")
        for h in near[:NEAR_MAX]:
            bars = max(1, math.ceil(h["eta"]))
            hours = bars * INTERVAL_SEC[iv] // 3600
            lines.append(coin_line(h, iv))
            lines.append(f"   간격 {h['gap']:+.2f}% · {'다음 봉' if bars == 1 else f'약 {bars}봉'}(≈{hours}시간 안) 교차 예상")
    if total == 0:
        lines.append("지금 조건에 맞는 코인 없음")
    lines.append("<i>※ 예상은 지금 속도가 그대로일 때의 단순 계산이라 빗나갈 수 있어요.</i>")
    lines.append("")
    return lines


def fail_lines(results):
    return [f"⚠️ {LABEL[r['ex']]} {r['iv']} 데이터를 못 받았어요: {html.escape(r['fail'])}" for r in results if r.get("fail")]


def summary_line(started, results):
    seen, parts = set(), []
    for r in results:
        if r["ex"] in seen:
            continue
        seen.add(r["ex"])
        parts.append(f"{LABEL[r['ex']]} {r['count']}개")
    return f"<i>{started:%m/%d %H:%M} KST · {' / '.join(parts)} 검사 · 투자 권유가 아닌 신호 알림입니다.</i>"


def run_scan():
    """4시간마다: 새 신호가 있을 때만 알림"""
    started = datetime.now(KST)
    res = scan_all(INTERVAL, {})
    enrich(res, INTERVAL, {})
    sig_lines, _ = signal_block(res, INTERVAL, f"🟡 Golden Trend {INTERVAL}봉 신호")
    fails = fail_lines(res)
    if sig_lines or fails or HEARTBEAT:
        lines = sig_lines or [f"✅ Golden Trend 검사 완료 ({INTERVAL}봉) — 새 신호 없음", ""]
        ml = market_light(res) if INTERVAL == "4h" else None
        legend = ["<i>신호등: 🟢 50선이 200선 위 · 🟡 교차 임박 · 🔴 아래 · ⚪ 데이터 부족</i>"] if sig_lines else []
        tg_send("\n".join(lines + ([ml] if ml else []) + legend + fails + [summary_line(started, res)]))
    else:
        log("새 신호 없음 — 텔레그램 안 보냄")


def run_report():
    """KST 00시·09시 정기 리포트: 항상 보냄"""
    started = datetime.now(KST)
    cache = {}
    res4 = scan_all("4h", cache)
    res1 = scan_all("1h", cache)
    known = {iv: {(h["ex"], h["code"]): h for r in rr for h in r["rows"]} for iv, rr in (("4h", res4), ("1h", res1))}
    enrich(res4, "4h", known)
    enrich(res1, "1h", known)

    lines = [f"🕘 <b>Golden Trend 정기 리포트</b> ({started:%m/%d %H:%M} KST)"]
    ml = market_light(res4)
    lines += [ml, ""] if ml else [""]

    # ① 4시간봉 새 신호: 4시간봉이 막 마감된 때(봉 마감 후 1시간 이내)만. 아니면 4시간 검사에서 이미 보낸 신호라 생략
    bar4 = max((r["bar"] for r in res4 if r["bar"]), default=None)
    fresh4 = bar4 and (datetime.now(timezone.utc) - (bar4 + timedelta(hours=4))) < timedelta(hours=1)
    n4 = 0
    if fresh4:
        sig4, n4 = signal_block(res4, "4h", "🟡 4시간봉 새 신호")
        lines += sig4 or ["🟡 4시간봉 새 신호: 없음", ""]

    # ② 1시간봉 새 신호
    sig1, n1 = signal_block(res1, "1h", "🔵 1시간봉 새 신호")
    lines += sig1 or ["🔵 1시간봉 새 신호: 없음", ""]

    # ③ 4시간봉 임박 목록
    lines += near_block(res4, "4h")

    # ④ 요약
    lines.append("<i>신호등: 🟢 50선이 200선 위 · 🟡 교차 임박 · 🔴 아래 · ⚪ 데이터 부족</i>")
    lines.append(f"새 매수 신호: 4시간봉 {n4 if fresh4 else '-'}개 · 1시간봉 {n1}개")
    lines += fail_lines(res4 + res1)
    lines.append(summary_line(started, res4))
    tg_send("\n".join(lines))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "test":
        tg_send("✅ Golden Trend 알림 봇 연결 테스트 성공! 이제 신호가 뜨면 여기로 알려드릴게요.")
    elif mode == "report":
        run_report()
    else:
        run_scan()
