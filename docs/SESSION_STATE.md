# Session State

Updated: 2026-09-21 (Asia/Seoul) — 프로젝트 신설, GitHub 레포 생성, PR-Y1(수집 모드) = PR #1

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
- 실기기 미검증. 첫 검증 = `run_dev.bat` → `[패킷] 캡처 시작` 줄 → 육의전 열람 → 창 파일이
  `<packet 폴더>/<디바이스>/packet_discovery/` 에 생기고 SEAssist `packet_explore.py list` 에 보이는 것.

## 다음 행동

1. Step 0 수집 — jby 포함 2~3 PC 에서 양성 창 ≥3·음성 창 ≥2(README 수집 절차).
2. Step 1 분석 — SEAssist 레포에서 `grep "<아이템명>"` → `timeline` → `stats` → `sub` → `body` → `bg`;
   결과를 SEAssist `docs/PACKET-MARKET-2026-09-XX.md` + PROCESS §3 H-2609-07 행 갱신.
3. PR-Y2(SEAssist): `MARKET_OPCODES`·전량 대기·`packet_market.py`·원장·코퍼스 → 동기화로 가져오기.
4. PR-Y3(SEAssist `dashboard/`): market 테이블·`POST /api/market/observations`·`GET /api/market/search`.
5. PR-Y1b(여기): 파서 → 스풀 → 업로드 관측 모드.
