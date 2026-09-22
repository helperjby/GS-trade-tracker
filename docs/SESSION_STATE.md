# Session State

Updated: 2026-09-22 오후 (Asia/Seoul) — **허브 공개 업로드(Tailscale Funnel + 초대 코드 자기등록, 아래 절, PR #10 머지 대기)** ·
**방향 전환(사용자 결정 2026-09-22): SEAssist 레포에는 더 이상 머지하지 않는다.**
**PR #5(PR-Y2b 아이템 표)·PR #6(PR-Y3 허브) 머지 완료**(https://github.com/helperjby/GS-trade-tracker/pull/5 840d8d3 · https://github.com/helperjby/GS-trade-tracker/pull/6 1ed43ef,
각 `/code-review high` 15건·14건 전부 반영) · **허브 Pi 배포 완료(2026-09-22 11:24, `~/yuktracker-hub`, :8800, G7 `stats` 응답 확인 — 아래 "허브 배포")** ·
SEAssist PR #306(PR-Y2) **미머지 → 닫음 예정, 이 레포로 이식(PR-Y2')** · Step 1 = SEAssist #304·#305(머지, 동결 시점 참조)

## 허브 공개 업로드 — Tailscale Funnel + 초대 코드 자기등록 (2026-09-22 오후, PR #10)

- 배경: 관측기 exe 는 **Npcap 만 있는 일반 사용자 PC** 에서 돈다 → VPN 전제 불가(사용자 질문 "Tailscale 뿐인가?"에서 출발). 사용자 결정:
  공개 경로 = Pi 의 **Tailscale Funnel**(사용자 PC 에 아무것도 안 깖, 포트포워딩 없음), 업로드 인증 = **초대 코드 자기등록**(기기별 토큰 +
  허브 발급 `device_id`), 관리 `secret` 은 조회 전용·exe 금지. Funnel 사실은 KB 1223/1311 + tailscale 소스 `ipn/ipnlocal/serve.go` 로 확인
  (클라 `Tailscale-*` 헤더 삭제 후 `Tailscale-Funnel-Request` 부착, `X-Forwarded-For` Set, `--set-path` 는 StripPrefix).
- 구현(브랜치 `claude/hub-public-funnel-20260922`, PR #9 위 스택 → #9 머지 뒤 retarget): `hub/server.py` — 공개 판정 = `proxy_header` 존재,
  조회 라우트는 자격 검사 전 403 `not_public`, 공개 요청의 관리 시크릿은 어느 라우트에서도 무시, `POST /api/market/register`(429 → closed →
  400 → 401 `bad_invite` → 403 `registration_full` → 발급), 기기 토큰 업로드(403 `device_revoked`/`device_mismatch`, 기기 당 429),
  `RateLimiter` 3종(인증 실패는 공개 IP 만 — 직접 접속은 docker 브리지 IP 를 봇과 공유해 세면 봇이 막힌다), 설정 `invite_code`(8자↑·예시값·
  `secret` 동일 거부)·`max_devices`·`admin_public`·`proxy_header`·limit 3개; `hub/db.py` — `devices` 표(additive)·메서드·stats
  `label`/`devices_registered`/`devices_revoked`·busy timeout; `hub/devices.py` — list/revoke/unrevoke/note CLI(서버와 같은 DB, 취소 즉시
  반영, 없는 DB 는 만들지 않음). Plan 에이전트 검토 반영: XFF **마지막** 항목, `registration_full` 은 5xx 아닌 403(exe 가 조용히 재시도하지
  않게), `device_id` 는 exe 가 POST 시점에 채움, 취소는 soft. hub 테스트 **67 passed**(신규 39), 로컬 스모크 17단계 통과(127.0.0.1:8801).
- 문서: HUB-PROTOCOL §0 전송 전제 개정·§1 devices·§3-0·§3-1·§3-4·§3-6·§6·§7, hub/README "공개 노출" 절(Funnel 절차·외부망 게이트·운영 메모,
  `sed` 순서 INVITE 먼저), PLAN §PR-Y3 공개 항목·§PR-Y1b(관측기 키 `hub_url`·`hub_device_id`·`hub_token`, 첫 실행 등록, 429/401/403 규약)·G7.
- 남은 것: `/code-review 10 high` 반영 → 머지 → Pi: hub/ 재복사 → `config.json` 에 `invite_code` 추가 → `docker compose up -d --build` →
  `sudo tailscale funnel --bg 8800`(관리 콘솔 HTTPS·`funnel` 노드 속성) → 폰 LTE 게이트(`GET /` 200 · stats 403 · register 200/401) →
  `devices.py list`. 그 뒤 PR-Y2' → PR-Y1b(등록 UX 포함).

## 현재 상태

- 레포: https://github.com/helperjby/GS-trade-tracker (private). `main` = 부트스트랩(문서만), **PR #1**
  (https://github.com/helperjby/GS-trade-tracker/pull/1, 브랜치 `feat/y1-capture-mode`) 가 코드 전부(28 파일). 이후 변경은 이 레포의 PR 로.

- 사용자 결정(2026-09-21): 소비자 봇 = 미루봇-IRIS, 허브 = SEAssist 대시보드 서버(Pi) 확장(→ 09-22 대체), 수집기 =
  **SEAssist GUI 가 아닌 별도 경량 프로그램(이 프로젝트, 별도 실행파일)**, 육의전 opcode 는 모름 → 발굴부터.
- **사용자 결정(2026-09-22)**: ① SEAssist 레포(`15. Gersang auto eating`, `C:\dev\gersang`)에는 더 이상 머지하지 않는다 — 육의전 트랙은
  전부 이 레포에서. ② 열려 있는 SEAssist PR #306(PR-Y2)도 머지하지 않고 이 레포로 **이식**(PR-Y2'). 이식 원본 = worktree
  `C:\dev\gersang\.claude\worktrees\market-y2-20260921`(브랜치 `claude/market-y2-20260921`, e905cd1, 커밋 6개) — #306 닫기는 사용자,
  worktree 는 이식이 끝날 때까지 보존. ③ 허브 = SEAssist dashboard 확장이 아니라 **이 레포의 독립 서버 `hub/`**(aiohttp+sqlite, Pi 별도
  컨테이너·포트 8800). ④ 벤더 사본은 SEAssist main **b54cb73**(= #303·#304·#305 머지 상태; 벤더 모듈 내용은 62e33db 와 동일) 시점에
  동결 — 이후 변경(프레이머 확장·파서)은 이 레포 소유 코드로, 방식(패치 계층 vs 소유 전환)은 PR-Y2' 계획에서. ⑤ 고정 아카이브
  `15. Gersang auto eating\packet` 에 9창을 복사하는 "머지 직전 절차"는 폐기(#306 미머지). 지난 세션의 임시 root
  `%TEMP%\market-y2-corpus`(45창)·`%TEMP%\market-y2-review`(11창)는 이식 검증용으로 남겨 둔다.
- SEAssist 레포 쪽 선행 변경 = **PR #303**(비공개 레포, 커밋 62e33db, 머지됨): 경로 함수를
  `src/core/ledger_paths.py` 로 분리(wordinput_runner 재수출), `packet_state_source` 의 numpy/PIL
  지연 import, `admin.relaunch_as_admin(module=)` — 벤더 사본이 cv2·genai 없이 돌기 위한 것.
- 이 프로젝트: `src/yuktracker/`(cli·agent·game_processes·item_names·paths) + 벤더 사본 + 테스트 + `build.bat`/`run_dev.bat` + **`hub/`**(허브 서버).
  관측기는 수집 모드만 있다(파서·업로드는 PR-Y2'·PR-Y1b).
- 벤더 사본은 SEAssist 62e33db 와 일치(`VENDOR.json`, `--check` 통과). 비승격 스모크(`--no-elevate`)는 rc 2(관리자 아님) 로 정상 종료.
  `--check` 는 로컬 체크아웃이 62e33db/b54cb73 벤더 모듈과 같을 때만 통과한다 —
  09-21 현재 `C:\dev\gersang` 본체는 `codex/monster-sweep`(5e8fdba) 이라 "불일치 6건" 이 뜨지만 벤더 사본은 무변경(`tests/test_vendor.py` 해시 핀 green).
- **실기기 첫 검증 완료(2026-09-21, 디바이스 DEV-A, Windows Terminal + 한글 IME)**: `run_dev.bat` 로 창 3개
  (`<OneDrive>\SEAssist\wordinput_review\<디바이스>\packet_discovery\`), 마지막 창(18:08, 547세그, 유실 0, writer 오류 0)에
  `market_manual` 라벨 9건 — SEAssist `packet_explore.py list` 에 `market_manual×9` 로 보인다. 라벨에 적은 판매자명이
  CP949 로 s2c 세그먼트 안에서 확인됐고, 프레이머(`timeline`)로는 **opcode `0x321f` · sub 218 · len 489** 프레임
  (창당 1~4회, `초기화` 라벨 직전). 본문은 48B × 10 항목, 아이템명 문자열은 없다(ID). 실데이터는 레포에 넣지 않는다.
- 검증에서 드러난 문제와 조치: ① 한글 IME 상태의 `q` 가 `ㅂ` 라벨로 기록(2회) → `agent.QUIT_WORDS` 에 `ㅂ`.
  ② 콘솔 X 로 닫은 창에 `window_end` 없음 → `SetConsoleCtrlHandler` 훅이 핸들러 스레드에서 `console_close` 로 마감.
  ③ 상세 라벨이 원인 패킷보다 3~35초 늦음 → README 절차(동작 직후 짧은 라벨, 상세는 다음 줄); SEAssist
  `mine_packet_discovery.LEAD_BY_EVENT["market_manual"]=30` 은 #304 로 머지됨. ④ 한글 입력 잘림
  (`악몽을 피우는 씨앗` → `을 는 앗`) — Python 읽기 경로는 줄 단위(cooked read)라 콘솔 호스트 + IME 쪽으로 추정,
  `tools/console_input_probe.py` 로 진단한 뒤 `_Console` 읽기 방식을 정한다(미수정). PR #2 머지 뒤 실기기 재확인: ㅂ 종료 O, 20:23 창 라벨 7건 전부 온전.
- **Step 1 분석(2026-09-21) = SEAssist PR #304·#305**(비공개 레포, 머지, `docs/PACKET-MARKET-2026-09-21.md`): 육의전 목록 응답 = 8000 s2c **`0x321f`**,
  9B 헤더(`[5:7]` 총 페이지 u16 · `[7:9]` 행 수 u16, 페이지 번호 없음) + **48B × 행**(등록 id · 아이템 id · 수량 · 가격 = u32 LE, 판매자 cp949
  16B NUL, 미상 4B, 플래그 2B). 코퍼스 30프레임 전부 `len == 9 + 48×count`, 콘솔 라벨 8행 **8/8 일치**. **아이템명은 없고 id 만** →
  H-2609-07 기각, H-2609-08(프레임·행 구조) · H-2609-09(폭) 등록. 페이지 번호는 요청에만 → 소비자는 등록 id 로 페이지를 이어 붙인다.
  전서구 창·상단 상점·전장 = 음성 2/2, 양성 9창·5세션·3PC, 예측 (1)~(4) 위반 0 → **등급 B**(사용자 승격 결정). `물품구매` 버튼·창 닫기는 s2c 없음.
- **PR-Y2 구현(2026-09-21 밤, SEAssist worktree `market-y2-20260921` ← `origin/main` b54cb73)**: 프레이머(`MARKET_OPCODES`·`is_market`·
  `observe_market` opt-in 전량 대기 3,081B·`MarketObservation`) + `packet_market.py` 파서(구조 불일치 None, C/D 필드 anomaly) → 엔진
  `market_cb`·헬스 7종·`note_market` 원장·App status → `explore market`(라이브·오프라인·프로브 3중 대조, `--mask`)·`market --shadow` →
  코퍼스 `market` 열 + 창 9개 등록(45창 76,488프레임 / market 39 / jochul 5) → 문서(PROCESS §3.1 B, H-2609-10/11 제안, FINDINGS §5.4, TOOLS,
  PACKET-MARKET 부록, CHANGELOG) → 패치 재검사 도구 `market` 열. 실캡처 80창: 9창 40페이지 라이브 == 오프라인 == 프로브, 라벨 8/8. 전체 스위트
  4,001 passed. **PR #306**(비공개 레포) — **09-22 결정으로 머지하지 않는다**(이식 원본).
- **아이템 id → 이름 표(2026-09-21 결정) = PR-Y2b, 이 레포 PR #5**: 클라 `gersang.gcs` 의 `육의전 검색 기능 리스트`(4,001행) — 라벨 8/8·관측 188 id 중
  186(3251 강화품 표, 1449232 미상 — 용병 탭 추정). `src/yuktracker/item_names.py`(순차 zlib 스캔·마커 식별·정규화·캐시·클라 탐색), `paths.py`,
  `game_processes.process_image_path`, `tools/dump_item_names.py`(`--check` 실기기: 4,001행·앵커 8/8·0.22s), 합성 아카이브 테스트(92 passed — 리뷰 반영 뒤),
  `zlib` 허용, `.gitignore item_names*.json`. UI 열 대응(사용자 스크린샷): 물품명·판매개수·판매자·단가 ↔ `@4`·`@8`·`@24`·`@16`;
  레벨 = 용병 탭 전용(추후); 기간(장기/단기) ↔ `@45` 후보(H-2609-11).
- **PR #5 리뷰 반영(2026-09-22, `/code-review 5 high` 15건 전부, c042521)**: 캐시 유효성 = 같은 경로 (크기, mtime) / 다른 사본 전체 sha256
  (앞 1MiB `head_sha256` 제거, `CACHE_VERSION` 2 — 크기 같은 패치를 영원히 놓치던 결함) · `refresh` 여도 캐시 폴백 유지 · id 는 ASCII 숫자만
  (`²` 행 하나가 표 전체를 잃게 하던 `isdigit`→`int`) · 크기 상한은 읽기 전 stat + 읽는 동안 변경 감지(캐시 오염 방지) · `write_json` mkstemp ·
  `ItemTable.raw` int 키 정규화 한 번 · 고정 폴더(`--client-dir`/env)에 gcs 없으면 다른 클라로 안 넘어감 + `%VAR%`/`~` 확장 ·
  `LoadResult(origin/gcs_path/error)` 로 도구가 hit/miss·종료 코드를 정확히(gcs 있는데 표 없음 = rc 1, `--check` 는 캐시 폴백도 실패) ·
  `--ids-from` 은 `아이템=N` 만 + UTF-8→cp949 · `paths.app_dir` 순수(`--help` 가 폴더 안 만듦, 심 `appdata_base` 공용) ·
  `process_image_path` 32767 재시도 · 시각 `Z` 한 형식 · 문서 수치 정정(8.4MB·1,247 스트림·추출 0.12s). 실기기: `--check --refresh`
  4,001행·앵커 8/8·0.16s, 사본 Gersang2 는 sha 로 캐시 hit, `--client-dir C:\AKInteractive`(gcs 없음) → 경고 + rc 2.
- **PR-Y3 허브 구현(2026-09-22 새벽, 이 레포, 브랜치 `claude/hub-market-api-20260922` ← PR #5 헤드 c9d200e, 09-22 리뷰 반영 c042521 위로 rebase)**: `hub/`(`server.py`·`db.py`·
  `requirements.txt`·`config.json.example`·`Dockerfile`·`docker-compose.yml`·`README.md`) + `hub/tests/`(14건, 합성 데이터) + **`docs/HUB-PROTOCOL.md`**
  (계약 정본) + CI `Run hub tests` 단계 + `.gitignore`(`hub/config.json`·`hub/data/`). 계약: `POST /api/market/observations`(배치 ≤100·행 ≤64,
  **전건 검증 후 트랜잭션 1개**, `obs_id` dedup — 중복은 무변경) · `GET /api/market/search?q=&item_id=&limit=&max_age_sec=`(정규화 한 정의 =
  공백 제거 + casefold, `instr` 부분일치, `price ASC`, `total_matches`) · `GET /api/market/listings?since_ts=&since_key=&limit=`(복합 keyset 커서 → `next_since_ts`·`next_since_key`, 리뷰 반영) ·
  `GET /api/market/stats` · `GET /`(무인증). 시각 `seen_ts = min(agent_ts, recv_ts)`, upsert 는 "더 나중에 본 관측이 상태를 쓴다",
  `listing_key = listing_id:item_id:seller`, 학습 표 `market_item_names`(이름 null 행 보충), 보존 30일. **검증**: hub 14 passed · 루트 73 passed ·
  실서버 스모크(127.0.0.1:8800 — POST 2관측/3행 → 재POST 중복 2 → search 수량 8·seen_count 2 → listings 2 → stats → 무인증 401 →
  잘못된 행 400 `rows[0].listing_id` → 상태 불변). Docker 이미지 빌드는 이 PC 의 Docker 데몬이 꺼져 있어 **미검증**(Dockerfile 은 SEAssist
  dashboard 의 것과 같은 꼴 — Pi 에서 `docker compose up -d --build` 로 확인). PR 링크는 생성 뒤 이 줄에 기록한다.
- **PR #6 리뷰 반영(2026-09-22, `/code-review 6 high` 14건 전부, #5 리뷰 반영 c042521 위로 rebase)**: 모든 int 를 i64 범위로 검증(2⁶³ 가격이
  sqlite OverflowError → 500 → 관측기 영구 재시도 루프였다) + 저장 오류는 JSON `500 storage_error` · `/listings` 복합 keyset 커서
  `(last_seen_ts, listing_key)` + `since_key`/`next_since_key`/행 `listing_key`(같은 seen_ts 행이 limit 넘으면 영구 누락되던 결함, 인덱스 교체) ·
  상대 `db_path` 는 config 폴더 기준(레포 루트 실행이 `<repo>/data/hub.db` 에 실데이터를 만들던 것) + `HUB_DB_PATH` 환경변수·Dockerfile
  `ENV HUB_DB_PATH=/data/hub.db`(컨테이너 임시 FS 에 쓰다 `up --build` 에 소멸하던 것) · `agent_ts` 범위 밖(보존기간 이전·`max_agent_ts_ahead_sec`
  초과 미래)은 `400 agent_ts_out_of_range` · 이름 upsert 도 최신 우선(늦게 온 옛 이름이 덮지 않음, NULL 은 덮지 않음) · 주입 DB 는 앱이 닫지
  않음 · `item_id` 쿼리 음수·비정수·i64 초과 → `400 field item_id` · secret 16자 이상·`CHANGE-ME` 거부 · payload_json 행은 미지 키만 ·
  seller/item_name ≤128·category ≤32 · `synchronous=NORMAL` · `web.AppKey` + `cleanup_ctx` · `_q_num` 하나로. HUB-PROTOCOL §1·§3-1·§3-2·§3-3·§4·§6·§7,
  hub/README, PLAN 갱신. **검증**: hub 28 passed(`-W error::NotAppKeyWarning`) · 루트 92 passed · 실서버 스모크 16/16(127.0.0.1:8801,
  scratchpad config: CHANGE-ME 기동 거부 → DB 가 config 폴더 옆 → POST 2/3 → 재POST 중복 2 → search 3 → keyset 커서 limit=1 로 3행+빈 호출 →
  stats → 401 → 잘못된 행·2⁶³·agent_ts 0·item_id abc 전부 400 → stats 불변).
- **허브 배포(2026-09-22 11:24, Pi `<user>@<pi-lan-ip>`)**: `hub/` 만 tar 로 `~/yuktracker-hub` 에 복사, 컨테이너 `yuktracker-hub-yuktracker-hub-1`
  (restart unless-stopped, `0.0.0.0:8800`, 기동 로그 `db_path=/data/hub.db`), DB `~/yuktracker-hub/data/hub.db`(compose 기본 `./data` → `/data`,
  Dockerfile `HUB_DB_PATH`). 시크릿은 Pi 가 생성(32자 `token_urlsafe`, `~/yuktracker-hub/config.json` chmod 600) — 레포·문서·채팅에 적지 않는다,
  관측기 `hub_secret`(PR-Y1b)·미루봇(PR-Y4)이 이 값을 공유. 확인: 컨테이너 안·LAN `<pi-lan-ip>:8800`·tailnet `<pi-tailnet-ip>:8800` 에서 `GET /` 200,
  `stats` 인증 200(전부 0·devices []), 무인증 401. **`/mnt/dashdata` 는 이 Pi 에 없다**(루트가 SSD `/dev/sda2` 로 이전, 대시보드도 `~/dashboard/data`)
  → hub/README 의 데이터 폴더 예시를 `./data` 기본으로 정정. 갱신 절차 = hub/ 재복사 → `docker compose up -d --build`(DB 는 볼륨이라 유지).
  PR #5 머지 뒤 #6 base 는 GitHub 이 자동으로 `main` 으로 바꿨다(브랜치 자동 삭제).

## 다음 행동

1. PR #5·#6 머지 · Pi 배포 — **완료(2026-09-22)**. SEAssist PR #306 은 **닫는다**(머지 안 함). 2. 허브 운영 확인: 첫 실업로드(PR-Y1b) 뒤
   `stats.devices` 에 기기가 보이고 `docker compose logs` 에 `market 관측 수신` 줄이 찍히는지(G7 2차).
3. **PR-Y2' 이식(이 레포)**: #306 worktree 의 프레이머 확장·`packet_market.py`·엔진 `market_cb`/헬스·테스트·`docs/PACKET-MARKET.md`(마스킹)·
   실캡처 대조 도구 → 이 레포 소유 코드로. 벤더 동결 처리 방식(패치 계층 vs 소유 전환, `sync_seassist_core.py`·`VENDOR.json`·`test_vendor` 핀)
   결정 포함. 검증 = 합성 프레임 테스트 + `%TEMP%\market-y2-corpus` 45창 오프라인 재생(9창 40페이지·라벨 8/8 재현).
4. PR-Y1b(여기): `market_cb` → `load_item_table` 이름 해석 → 스풀 → 첫 실행 `--invite-code` 등록(§3-0) → `hub_url/hub_device_id/hub_token`
   (`%APPDATA%\YukTracker\config.json`)로 HTTPS 업로드(HUB-PROTOCOL §3-1; 429 대기·401/403 정지·`device_id` 는 POST 시점). 5. 실기기: Step 0 잔여 캡처 2창(라벨 5열 `@45` 검정 · 용병 탭 열람 · 수량 ≥65,536·타 PC 세션) ·
   `python tools\console_input_probe.py` 진단 → `_Console` 수정 PR. 6. PR-Y4 미루봇: `http://127.0.0.1:8800`, HUB-PROTOCOL §3-2/§3-3(`Lv.` 표시 보류).
