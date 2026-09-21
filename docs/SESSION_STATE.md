# Session State

Updated: 2026-09-21 밤 (Asia/Seoul) — **PR-Y2 구현 완료(SEAssist 브랜치 `claude/market-y2-20260921`, 로컬 커밋 6개, 푸시·PR 대기)** ·
**PR-Y2b 아이템 표 추출기 구현 완료(이 레포 브랜치 `claude/pr-y2-parser-item-mapping-06a025`, PR 대기)** · Step 1 = SEAssist #304·#305(머지)

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
  사냥 코퍼스 3창의 `0x321f` 는 사용자가 열람 확인 → 양성). 21:16 창: 상단 열기·상단 상점 열기(`0x32aa`·`0x326b`·`0x3258`)·전장
  열기(`0x2f1c`)에서 `0x321f` 0 → **음성 2/2**. 합계 양성 9창·5세션·3PC / 음성 2창, 예측 (1)~(4) 위반 0 → **등급 B 제안**(리뷰어 판정,
  스윕·경계값 일부 미실시라 A 아님). `물품구매` 버튼·창 닫기는 s2c 없음.

- **PR-Y2 구현(2026-09-21 밤, SEAssist worktree `C:\dev\gersang\.claude\worktrees\market-y2-20260921`, 브랜치 `claude/market-y2-20260921`
  ← `origin/main` b54cb73 = #303·#304·#305 전부 머지된 상태)**: 사용자 결정 ① PR-Y2 → PR-Y2b 순서 ② **H-2609-08 을 B 로 승격**(리뷰어 판정)
  ③ 아이템 이름은 관측기가 업로드 때 붙임. 커밋: 프레이머(`MARKET_OPCODES`·`is_market`·`observe_market` opt-in 전량 대기 3,081B·`MarketObservation`) +
  `packet_market.py` 파서(구조 불일치 None, C/D 필드 anomaly) → 엔진 `market_cb`·헬스 7종·`note_market` 원장·App status → `explore market`
  (라이브·오프라인·프로브 3중 대조, `--mask`)·`market --shadow` → 코퍼스 `market` 열 + 창 9개 등록(**45창 76,488프레임 / market 39 / jochul 5**,
  wordinput 56창 무변경) → 문서(PROCESS §3.1 B, H-2609-10/11 제안, FINDINGS §5.4, TOOLS, PACKET-MARKET 부록, CHANGELOG) → 패치 재검사
  도구 `market` 열. 실캡처 80창: 9창 40페이지 라이브 == 오프라인 == 프로브, 라벨 8/8. 전체 스위트 green. **푸시·PR 생성은 사용자 확인 뒤**
  (PR 본문 초안 = 스크래치패드 `pr_y2_body.md`). 머지 직전 수동: 9 파일을 `15. Gersang auto eating\packet\<디바이스>\packet_discovery\` 에 복사 → 두 검사 재실행.
- **아이템 id → 이름 표 출처 결정(2026-09-21)**: 클라 `gersang.gcs` 의 `육의전 검색 기능 리스트`(4,001행) — 라벨 8/8·관측 188 id 중 186(3251 강화품 표,
  1449232 미상 — 용병 탭 추정). **PR-Y2b 구현(이 레포)**: `src/yuktracker/item_names.py`(순차 zlib 스캔·마커 식별·정규화·캐시·클라 탐색),
  `paths.py`, `game_processes.process_image_path`, `tools/dump_item_names.py`(`--check` 실기기: 4,001행·앵커 8/8·0.22s, 실행 중 클라 폴더 자동 탐색),
  합성 아카이브 테스트, `zlib` 허용, `.gitignore item_names*.json`, README·AGENTS·PLAN 갱신. UI 열 대응(사용자 스크린샷): 물품명·판매개수·판매자·단가
  ↔ `@4`·`@8`·`@24`·`@16`; 레벨 = 용병 탭 전용(추후); 기간(장기/단기) ↔ `@45` 후보(H-2609-11).

## 다음 행동

1. **PR-Y2 푸시·PR**(사용자 확인): `git -C C:\dev\gersang\.claude\worktrees\market-y2-20260921 push -u origin claude/market-y2-20260921` →
   `gh pr create --repo helperjby/gersang-auto-eating --base main`(본문 = `pr_y2_body.md`). 머지 직전 9창 복사 절차(PACKET-MARKET 부록).
2. **PR-Y2b PR**(이 레포, 브랜치 `claude/pr-y2-parser-item-mapping-06a025`) — SEAssist PR 과 독립.
3. SEAssist PR-Y2 머지 뒤 여기 동기화: `tools/sync_seassist_core.py` `MODULES += "packet_market.py"` **먼저** → `--source <머지된 main>` →
   VENDOR.json → 스모크 테스트(`packet_market` import + `FlowDecoder(observe_market=True)`).
4. Step 0 잔여 캡처 2창: ① 아이템 탭에서 라벨 5열(`아이템, 수량, 판매자, 단가, 기간` — 예 `정기의구슬(風), 10, <판매자>, 45,000,000, 장기`)
   → H-2609-11(`@45`) 검정 ② **용병 탭** 열람 → 같은 `0x321f` 인지·레벨 필드·1449232 류 행. A 승격용: 수량 ≥65,536·타 PC 세션.
5. `python tools\console_input_probe.py` 로 콘솔 한글 입력 잘림 진단 → `_Console` 수정 PR.
6. PR-Y3(SEAssist `dashboard/`): market 테이블(`item_id` + `item_name` null 허용 + 학습 표 `market_item_names`)·`POST /api/market/observations`·
   `GET /api/market/search`. 7. PR-Y1b(여기): `market_cb` → 이름 해석(`load_item_table`) → 스풀 → 업로드. 8. PR-Y4 미루봇(`Lv.` 표시 보류).
