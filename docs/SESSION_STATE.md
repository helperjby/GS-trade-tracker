# Session State

Updated: 2026-09-21 (Asia/Seoul) — Step 1 분석 = SEAssist **PR #304**(머지) + **#305**(페이지·전서구 반영) · 라벨 운용 수정 = **PR #2**(머지)

## 현재 상태

- 레포: https://github.com/helperjby/GS-trade-tracker (private). `main` = 부트스트랩(문서만), **PR #1**
  (https://github.com/helperjby/GS-trade-tracker/pull/1, 브랜치 `feat/y1-capture-mode`) 가 코드 전부(28 파일). 이후 변경은 이 레포의 PR 로.

- 사용자 결정(2026-09-21): 소비자 봇 = 미루봇-IRIS, 허브 = SEAssist 대시보드 서버(Pi) 확장, 수집기 =
  **SEAssist GUI 가 아닌 별도 경량 프로그램(이 프로젝트, 별도 실행파일)**, 육의전 opcode 는 모름 → 발굴부터.
- SEAssist 레포 쪽 선행 변경 = **PR #303**(https://github.com/helperjby/gersang-auto-eating/pull/303, 커밋 62e33db, 전체 스위트 3923 green, 머지·배포 대기): 경로 함수를
  `src/core/ledger_paths.py` 로 분리(wordinput_runner 재수출), `packet_state_source` 의 numpy/PIL
  지연 import, `admin.relaunch_as_admin(module=)` — 벤더 사본이 cv2·genai 없이 돌기 위한 것.
- 이 프로젝트: `src/yuktracker/`(cli·agent·game_processes) + 벤더 사본 + 테스트 + `build.bat`/`run_dev.bat`.
  수집 모드만 있다(파서·업로드 없음).
- 벤더 사본은 SEAssist 62e33db 와 일치(`VENDOR.json`, `--check` 통과). 테스트 25건 green, 비승격 스모크(`--no-elevate`)는 rc 2(관리자 아님) 로 정상 종료.
  `--check` 는 로컬 `C:\dev\gersang` 이 62e33db(브랜치 `claude/yuguijeon-packet-analysis-d6a358`) 체크아웃일 때만 통과한다 —
  09-21 현재 로컬은 `codex/monster-sweep`(5e8fdba) 이라 "불일치 6건" 이 뜨지만 벤더 사본은 무변경(`tests/test_vendor.py` 해시 핀 green).
- **실기기 첫 검증 완료(2026-09-21, 디바이스 HIC0TCR, Windows Terminal + 한글 IME)**: `run_dev.bat` 로 창 3개
  (`<OneDrive>\SEAssist\wordinput_review\HIC0TCR\packet_discovery\`), 마지막 창(18:08, 547세그, 유실 0, writer 오류 0)에
  `market_manual` 라벨 9건 — SEAssist `packet_explore.py list` 에 `market_manual×9` 로 보인다. 라벨에 적은 판매자명이
  CP949 로 s2c 세그먼트 안에서 확인됐고, 프레이머(`timeline`)로는 **opcode `0x321f` · sub 218 · len 489** 프레임
  (창당 1~4회, `초기화` 라벨 직전) — **후보일 뿐** PACKET-PROCESS §3 에 행이 없다(H-2609-07 등록·음성 창 대조는
  Step 1). 본문은 48B × 10 항목으로 보이고 아이템명 문자열은 없다(ID 추정). 실데이터는 레포에 넣지 않는다.
- 검증에서 드러난 문제와 조치: ① 한글 IME 상태의 `q` 가 `ㅂ` 라벨로 기록(2회) → `agent.QUIT_WORDS` 에 `ㅂ`.
  ② 콘솔 X 로 닫은 창에 `window_end` 없음 → `SetConsoleCtrlHandler` 훅이 핸들러 스레드에서 `console_close` 로 마감.
  ③ 상세 라벨이 원인 패킷보다 3~35초 늦음 → README 절차(동작 직후 짧은 라벨, 상세는 다음 줄); SEAssist
  `mine_packet_discovery.LEAD_BY_EVENT` 에 `market_manual` 등록은 PR-Y2 몫(현재 기본 3초). ④ 한글 입력 잘림
  (`악몽을 피우는 씨앗` → `을 는 앗`) — Python 읽기 경로는 줄 단위(cooked read)라 콘솔 호스트 + IME 쪽으로 추정,
  `tools/console_input_probe.py` 로 진단한 뒤 `_Console` 읽기 방식을 정한다(미수정). PR #2 머지 뒤 실기기 재확인: ㅂ 종료 O, 20:23 창 라벨 7건 전부 온전.
- **Step 1 분석 1차(2026-09-21) = SEAssist PR #304**(https://github.com/helperjby/gersang-auto-eating/pull/304, #303 위 스택,
  `docs/PACKET-MARKET-2026-09-21.md`): 육의전 목록 응답 = 8000 s2c **`0x321f`**, 9B 헤더(`[5:7]` 총 페이지 u16 · `[7:9]` 행 수 u16,
  페이지 번호 없음) + **48B × 행**(등록 id · 아이템 id · 수량 · 가격 = u32 LE, 판매자 cp949 16B NUL, 미상 4B, 플래그 2B). 코퍼스
  30프레임 전부 `len == 9 + 48×count`, 콘솔 라벨 8행의 수량·가격·판매자 **8/8 일치**. **아이템명은 없고 id 만** → H-2609-07 기각,
  H-2609-08(프레임·행 구조, C 제안, 검정중) · H-2609-09(폭) 등록. 소비자 검색(`!육의전 <아이템>`)은 **id→이름 표가 따로 필요** —
  PR-Y2/Y3 설계 전제. 도구 `scripts/packet_market_probe.py`(`--mask`), `mine LEAD_BY_EVENT["market_manual"]=30`.
  후속 **PR #305**(https://github.com/helperjby/gersang-auto-eating/pull/305): 20:50 창(페이지 1~10 라벨 → 프레임 10개, 헤더 `[3]=[4]=0`
  → **페이지 번호는 요청에만**, 소비자는 등록 id 로 페이지를 이어 붙인다; **전서구 창 = 음성 1창**(`0x32fc`·`0x32fa` 만, `0x321f` 0);
  사냥 코퍼스 3창의 `0x321f` 는 사용자가 열람 확인 → 양성). 합계 양성 9창·5세션·3PC / 음성 1창 — **B 까지 상점·창고 1창 남음**.
  `물품구매` 버튼·창 닫기는 s2c 없음.

## 다음 행동

1. `python tools\console_input_probe.py` 를 관측기와 같은 콘솔에서 돌려 살아남는 읽기 방식 확인 → `_Console` 수정 PR.
2. Step 0 잔여 — **음성 창 1개 더**(상점 또는 창고: 목록 UI 열되 육의전 아님) → 음성 2/2 이면 H-2609-08 **B 승격 검토**.
   용병(Lv) 목록 표본, 다른 PC 세션은 있으면 좋음. (페이지 넘김·전서구·미라벨 확인은 09-21 완료.)
3. Step 1 잔여 — `@40`(프레임 상수)·`@45`(1|2)·`@46`(0~7) 의미(서버·카테고리·등급 바꿔 열기), 아이템 id→이름 표 출처
   (클라 리소스 / 라벨 누적 — 8건 시드 / OCR) 결정 → SEAssist PROCESS §3 H-2609-08 갱신. 재현: `python scripts/packet_market_probe.py --root <packet>`.
4. PR-Y2(SEAssist): `MARKET_OPCODES = {0x321f}`(H-2609-08 B 승격 뒤)·전량 대기·`packet_market.py`(9B 헤더 + 48B 행 파서)·원장·코퍼스 → 동기화로 가져오기.
5. PR-Y3(SEAssist `dashboard/`): market 테이블·`POST /api/market/observations`·`GET /api/market/search`.
6. PR-Y1b(여기): 파서 → 스풀 → 업로드 관측 모드.
