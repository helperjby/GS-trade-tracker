# Session State

Updated: 2026-09-21 (Asia/Seoul) — 실기기 첫 검증 완료(라벨 9건), 라벨 운용 수정 = **PR #2**(https://github.com/helperjby/GS-trade-tracker/pull/2, 브랜치 `claude/run-dev-label-check-34b190`)

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
  `tools/console_input_probe.py` 로 진단한 뒤 `_Console` 읽기 방식을 정한다(미수정).

## 다음 행동

1. `python tools\console_input_probe.py` 를 관측기와 같은 콘솔에서 돌려 살아남는 읽기 방식 확인 → `_Console` 수정 PR.
2. Step 0 수집 — jby 포함 2~3 PC 에서 양성 창 ≥3·음성 창 ≥2(README 수집 절차). 음성 창(상점·창고)에서 `0x321f`
   가 안 나오는지가 첫 대조점.
3. Step 1 분석 — SEAssist 레포에서 `grep "<판매자명>"`(아이템명은 문자열로 없다) → `timeline` → `stats` → `sub`
   → `body` → `bg`; `LEAD_BY_EVENT["market_manual"]`(30초 안팎) 등록; 결과를 SEAssist
   `docs/PACKET-MARKET-2026-09-XX.md` + PROCESS §3 H-2609-07 행 갱신.
4. PR-Y2(SEAssist): `MARKET_OPCODES`·전량 대기·`packet_market.py`·원장·코퍼스 → 동기화로 가져오기.
5. PR-Y3(SEAssist `dashboard/`): market 테이블·`POST /api/market/observations`·`GET /api/market/search`.
6. PR-Y1b(여기): 파서 → 스풀 → 업로드 관측 모드.
