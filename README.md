# Golden Trend 매수신호 알림 봇

업비트 원화마켓 전체와 바이낸스 USDT 마켓 전체를 4시간마다 검사해서,
**4시간봉 50선이 200선을 위로 뚫는 순간(Golden Trend 매수 신호)** 이 나온 코인을 텔레그램으로 알려줍니다.

- 트레이딩뷰 Golden Trend 지표와 같은 규칙: 단순이동평균(SMA) 50 / 200, **마감된 봉에서만** 판단
- 검사 시각: 매일 KST 01·05·09·13·17·21시 3분쯤 (4시간봉 마감 직후). GitHub 사정으로 5~15분 늦을 수 있음
- 서버: GitHub Actions(무료). PC를 꺼둬도 돌아감
- 주문은 하지 않음. 알림만 보냄. API 키 필요 없음

---

## 설치 (처음 한 번, 15분 정도)

### 1. 텔레그램 봇 만들기
1. 텔레그램에서 **@BotFather** 검색 → 대화 시작
2. `/newbot` 입력 → 봇 이름(예: `골든트렌드 알림`) → 아이디(예: `my_goldentrend_bot`, 끝이 bot)
3. BotFather가 주는 **토큰**(`123456789:AAH...` 모양)을 복사해 둠 → 이게 `TELEGRAM_BOT_TOKEN`
4. 방금 만든 봇을 검색해서 들어가 **시작(/start)** 을 누르고 아무 말이나 한 번 보냄
5. 인터넷 주소창에 아래를 붙여넣고(토큰 부분만 바꿔서) 엔터
   `https://api.telegram.org/bot여기에토큰/getUpdates`
6. 나오는 글에서 `"chat":{"id":123456789` 의 숫자를 복사 → 이게 `TELEGRAM_CHAT_ID`

### 2. GitHub 저장소 만들기
1. https://github.com 가입/로그인
2. 오른쪽 위 **+** → **New repository** → 이름 `gt-scanner` → **Private** 선택 → Create
3. **uploading an existing file** 링크 클릭 → 이 폴더의 파일을 전부 끌어다 놓기
   (`scanner.py`, `README.md`, `.github` 폴더까지. `.github` 폴더가 안 보이면 아래 '참고' 확인)
4. **Commit changes**

### 3. 토큰 등록 (비밀값이라 코드에 직접 쓰지 않음)
저장소 → **Settings** → 왼쪽 **Secrets and variables → Actions** → **New repository secret**
- Name `TELEGRAM_BOT_TOKEN` / Secret: 1번에서 받은 토큰
- Name `TELEGRAM_CHAT_ID` / Secret: 1번에서 받은 숫자

### 4. 테스트
저장소 → **Actions** 탭 → (처음이면 활성화 버튼 누르기) → 왼쪽 **Golden Trend 스캔** →
**Run workflow** → mode에 `test` 입력 → 실행
→ 텔레그램에 "연결 테스트 성공!"이 오면 끝. 그다음부터는 4시간마다 알아서 돕니다.

`scan`으로 한 번 더 실행하면 지금 바로 전체 검사를 해봅니다(새 신호가 없으면 메시지 없음).

---

## 옵션 (Settings → Secrets and variables → Actions → **Variables** 탭)
| 이름 | 값 | 설명 |
|---|---|---|
| `GT_HEARTBEAT` | `true` | 신호가 없어도 4시간마다 "검사 완료" 메시지 받기 (잘 도는지 확인용) |
| `GT_SELL_ALERTS` | `true` | 50선이 200선 아래로 내려가는 청산 신호도 받기 |

봉 기준을 바꾸려면 `.github/workflows/scan.yml`의 `GT_INTERVAL: "4h"`를 `1h` / `1d` 등으로 바꾸고,
`cron` 줄도 그 봉 마감 시각에 맞게 바꿔야 합니다(1시간봉이면 `"3 * * * *"`).

## 알림 예시
```
🟡 Golden Trend 신호 (4h봉)
기준봉: 10/25 13:00 ~ 17:00 KST (마감 확정)

📈 매수 신호 (50선이 200선 위로)
• [업비트] KRW-SOL 솔라나
   종가 285,000원 · 50/200 간격 +0.08%
• [바이낸스] SOLUSDT SOL
   종가 $195.20 · 50/200 간격 +0.05%
```
코인 이름을 누르면 트레이딩뷰 4시간봉 차트가 열립니다. ⚠️유의종목은 업비트 투자유의 지정 종목.

## 참고
- **비용**: Private 저장소는 한 달 2,000분 무료. 이 봇은 한 달 약 600~800분 사용.
- **바이낸스 접속**: GitHub 서버는 미국에 있고, 바이낸스는 미국 접속을 막는 경우가 있어
  공개 시세 전용 주소(data-api.binance.vision)를 씁니다. 그래도 막히면 텔레그램에
  "바이낸스 데이터를 못 받았어요"가 오고, 업비트 검사는 정상으로 계속됩니다.
- **`.github` 폴더가 안 올라갈 때**: 저장소에서 **Add file → Create new file** → 파일 이름에
  `.github/workflows/scan.yml` 입력 → 내용 붙여넣기 → Commit.
- 상장한 지 얼마 안 돼 4시간봉이 201개(약 34일) 안 되는 코인은 검사에서 빠집니다.
- 신호는 과거 가격으로 계산한 기술적 신호일 뿐이고 수익을 보장하지 않습니다. 투자 판단은 본인 책임입니다.
