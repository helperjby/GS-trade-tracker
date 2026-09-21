# 육의전 패킷 → 공유 DB → 미루봇 `!육의전` — 구현 계획 (2026-09-21 승인, 2026-09-22 정정)

> 원본은 Claude Code 플랜(`C:\Users\A\.claude\plans\system-reminder-you-are-operating-splendid-globe.md`).
> 2026-09-21 사용자 지시로 관측기를 SEAssist 레포 안이 아니라 **이 프로젝트(별도 실행파일)** 에 두는 것으로
> 바뀌었다. **2026-09-22 사용자 결정으로 SEAssist 레포(`15. Gersang auto eating`, `C:\dev\gersang`)에는 더 이상
> 머지하지 않는다** — 열려 있던 SEAssist PR #306(PR-Y2)도 머지하지 않고 이 레포로 이식하고(PR-Y2'), 허브는 SEAssist
> `dashboard/` 확장이 아니라 **이 레포의 독립 서버 `hub/`** 다. 아래는 두 정정을 반영한 판이다.

## 1. Context

- **육의전**은 유저가 판매 아이템을 등록하는 인게임 거래소. 목록은 **유저가 직접 육의전을 열 때만** s2c
  패킷으로 내려온다. 다른 패킷 기반 서드파티(geota 등)가 이미 이걸로 시세를 만든다.
- 미루봇-IRIS(카카오톡)의 **GS-01 육의전 검색·알람**은 원본을 geota 웹으로 잡았다가 HTTP 403 에 막혀 대기
  중(`32. 미루봇-IRIS/docs/GS-01_SOURCE_CONTRACT.md`). 파일럿 봇의 `!육의전 <아이템>`·`!알람등록/해제/목록`
  명령·출력 형식(`27. IRIS/bots/gersang_bot.py:50-71`)을 계승한다.
- 목표: **1~7명의 클라에서 육의전 패킷을 관측 → 파싱 → 공유 저장소에 합류 → 미루봇이 읽어 `!육의전` 을
  켠다.** 육의전 DB 는 "누군가 마지막으로 본 것"의 부분·지연 스냅샷이므로 **관측 시각**을 항상 함께 다룬다.
- 사용자 결정(2026-09-21): ① 소비자 봇 = 미루봇-IRIS ② 허브 = SEAssist 대시보드 서버(Pi) 확장(**→ 09-22 에 ③' 로 대체**)
  ③ 수집기 = **별도 경량 프로그램(이 프로젝트)** ④ opcode·필드는 모름 → 발굴부터.
- **사용자 결정(2026-09-22)**: ①' SEAssist 레포에는 더 이상 머지하지 않는다 — 육의전 트랙의 코드·문서는 전부 이 레포.
  ②' SEAssist PR #306(PR-Y2, 프레이머 확장·`packet_market.py`·코퍼스 등록)은 **미머지** → 이 레포로 이식(PR-Y2'). 이식 원본은
  worktree `C:\dev\gersang\.claude\worktrees\market-y2-20260921`(브랜치 `claude/market-y2-20260921`, e905cd1).
  ③' 허브 = 이 레포 `hub/` 독립 서버(aiohttp+sqlite, Pi 별도 컨테이너·포트 8800). ④' 벤더 사본은 SEAssist main b54cb73 시점에
  **동결** — 이후 프로토콜 변경은 이 레포 소유 코드로 들어간다(방식은 PR-Y2' 계획에서).
- 규칙: 새 opcode 는 SEAssist `docs/PACKET-PROCESS.md` §3 레지스트리 ID 없이 FINDINGS 에 못 들어가고, 등급 D 로는 런타임
  판정에 못 들어간다 — 이 규칙은 그대로 따르되, 09-22 이후 레지스트리·등급의 **후속 기록은 이 레포 `docs/PACKET-MARKET.md`**
  (PR-Y2' 에서 이식)에 잇는다. SEAssist 문서는 동결 시점(b54cb73 = #303·#304·#305) 참조. 이 트랙의 런타임은 관측 전용
  (게임 입력 없음)이라 §2 표의 **B(관측 전용 배선)** 가 목표 등급(H-2609-08 은 B 승격 결정됨).

## 2. 목표 구조

```
[거상 클라 ×N / PC] ─ s2c 8000 (평문) ─▶ Npcap ─▶ YukTracker (이 프로젝트: 경량 콘솔, 관리자, GUI·러너 없음)
                                                   ├─ --capture : 원시 창 window_*.jsonl (발굴, SEAssist PacketDiscoveryRecorder 벤더 사본)
                                                   └─ 관측 모드 : FlowDecoder → 육의전 프레임 전량 대기 → packet_market.parse (PR-Y2' 이식)
                                                                  → item_names 로 이름 해석 → 로컬 스풀(JSONL) → HTTP POST /api/market/observations (Bearer)
[허브 Pi:8800 (이 레포 hub/, aiohttp+sqlite, 독립 컨테이너)] market_observations · market_listings · market_item_names
                                                   + GET /api/market/search · /listings · /stats   — 계약 docs/HUB-PROTOCOL.md
[미루봇-IRIS (같은 Pi)] Geosang GS-01: 허브 market 클라이언트(127.0.0.1:8800) → !육의전 검색 / !알람등록·해제·목록 / 알림 job
```

- SEAssist GUI·대시보드는 건드리지 않는다. 관측은 이 프로그램이, 저장·검색은 `hub/` 가 전담.
- 패킷 코어는 SEAssist 레포의 **벤더 사본**(`tools/sync_seassist_core.py`, `VENDOR.json` 에 출처 커밋·해시) — **b54cb73 에서 동결**.
  육의전 프레이머 확장·파서는 SEAssist 에 남지 않고(PR #306 미머지) 이 레포 소유 코드로 들어온다(PR-Y2').
- 에이전트→허브는 WS 이벤트가 아니라 **HTTP POST + 로컬 스풀**로 at-least-once(허브가 `obs_id` 로 dedup).

## 3. 단계와 PR

### Step 0 — 수집(코드 0줄) → G0
이 프로그램(`run_dev.bat` / `YukTracker.exe --capture 5`) 또는 SEAssist GUI `[패킷 수집]`(매크로 세션 중)로:
**정지 30s → 육의전 열기 → 잘 안 팔리는 고유한 아이템명 1개 검색 → 페이지 1..N → 카테고리 열람 → 빈 결과
검색 1회 → 닫기 → 정지 30s.** 콘솔 라벨(`market_manual`)로 각 동작 시각을 남긴다.
- 표본 요건(PROCESS §2): 양성 ≥3 창·서로 다른 세션/PC(B 등급은 ≥5) + 음성 ≥2 창(상점·창고·우편처럼 목록 UI
  는 열지만 육의전은 아닌 창 — 일반 사냥 창은 기존 36창 코퍼스가 이미 음성).
- 값 스윕: 가격 ≥65,536(u16/u32 경계)·수량 1 vs 다수·용병(Lv) vs 물품·긴 아이템명.

### Step 1 — 분석(G1~G5) → SEAssist `docs/PACKET-MARKET-2026-09-21.md` (완료, #304·#305 머지)
가설 **H-2609-07** "육의전 목록 응답은 8000 s2c 프레임이며 아이템명이 cp949 로 실린다" — 예측 (1) 육의전
열람 창에서만, 음성 창 0 (2) 검색한 아이템명이 본문에 cp949 로 있다 (3) 같은 목록을 두 번 열면 같은 opcode·
같은 배치 (4) 페이지마다 프레임 1개(또는 N, count 필드와 일치) → 반증: 음성 출현·이름 미검출·배치 불일치.
엔디언·폭·문자열 형식은 각각 별도 가설(§2).
```
python scripts/packet_explore.py --root <packet> grep "<아이템명>" --enc cp949
python scripts/packet_explore.py --root <packet> timeline <창>
python scripts/packet_explore.py --root <packet> stats --min-n 1
python scripts/packet_explore.py --root <packet> sub 0x????
python scripts/packet_explore.py --root <packet> body 0x???? --window <창> --max-bytes 256
python scripts/packet_explore.py --root <packet> bg 0x????
python scripts/mine_packet_discovery.py --all --lead-sec 30
```
산출: PROCESS §4 규격의 필드 표, 페이지·count·문자열 형식 판정, 열거 opcode 집합. 결과: 아이템명은 없고 **id 만**
(H-2609-07 기각 → H-2609-08 `0x321f` 9B 헤더 + 48B×행, B 승격), 후보 필드: 등록 id·아이템 id·수량·가격·판매자·플래그.

### PR-Y1 — 이 프로젝트, 수집 모드 (2026-09-21 골격 완료)
- `src/yuktracker/cli.py`(인자·UAC 승격) · `agent.py`(`PidIndexer`·엔진 배선·수집 창·콘솔 라벨) ·
  `game_processes.py`(Toolhelp32 → `gersang.exe`) · `seassist/`(벤더 사본 + `config.py` 심).
- SEAssist 선행 변경(같은 날, #303 머지): `ledger_paths.py` 분리·`packet_state_source` numpy 지연 import·
  `admin.relaunch_as_admin(module=)` — 사본이 cv2·genai 없이 돌기 위한 것.
- 배포: `build.bat` → `dist/YukTracker.exe`(UAC 매니페스트). 공유 방법(OneDrive 폴더·직접 전달)은 사용자 결정.

### PR-Y2 — SEAssist 프로토콜·파서·원장·코퍼스 (2026-09-21 구현, PR #306 — **미머지, 09-22 결정으로 이식 원본**)
- `gersang_protocol.py`: `MARKET_OPCODES = {0x321f}`·`is_market`(`len == 9 + 48×count`, `body[4]` 는 판별 조건 아님),
  `StreamFramer(observe_market=)` opt-in 전량 대기(자체 상한 `9 + 48×MARKET_MAX_ROWS(64)` = 3,081B),
  `MarketObservation`(raw) + `take_market()`/`market_dropped`/`market_rejected`; `FlowDecoder` 통과. ENTER 조기 발화·락·앵커 무접촉.
- `packet_market.py`(stdlib + `.gersang_protocol`, 검증된 배치만 수용 — `packet_death.py` 선례): `parse_market_page(body)` →
  `MarketPage(total_pages, count, hdr4, rows: MarketRow…, anomalies)`; C/D 필드(hdr4·@12/@20·@40·@45·@46)는 거부가 아니라
  `anomalies`. `mask_seller`·`row_to_dict`. **관측기 콜백 계약**: `market_cb(slot_idx, page, observation, *, pid)` — 파싱
  성공분만, 스니퍼 스레드, 예외는 엔진이 삼키고 `market_callback_errors`.
- `PacketStateSource(market_cb=)` + 헬스 7종; `PacketShadowLedger.note_market()` + `packet_explore.py market [--rows --mask]`
  (라이브 재생·오프라인·프로브 3중 대조)·`market --shadow`; `check_packet_corpus.py` `market` 열 + 창 9개 등록(45창, market 39).
- 문서: FINDINGS §0·§5.4, PROCESS §3(H-2609-08 **B**, H-2609-10·11 제안), TOOLS, PACKET-MARKET 부록, CHANGELOG.

### PR-Y2' — 위 PR-Y2 를 이 레포로 이식 (2026-09-22 결정, 허브 PR 다음)
- 가져올 것: 프레이머 확장(`MARKET_OPCODES`·`is_market`·`observe_market` 전량 대기·`MarketObservation`) · `packet_market.py`(파서·
  `row_to_dict`·`mask_seller`) · 엔진 `market_cb`/헬스 카운터 · 관련 테스트(합성 프레임) · `docs/PACKET-MARKET.md`(분석·필드 표·가설
  H-2609-08/10/11 — 판매자명 마스킹) · 실캡처 대조 도구(`tools/market_probe.py`: OneDrive 창 **읽기 전용**, `--mask`).
- 벤더 동결 처리 방식은 이 PR 의 계획에서 정한다 — 후보: (a) 사본은 그대로 두고 확장을 별도 모듈/서브클래스로 얹기
  (b) `sync_seassist_core.py` 에 패치 계층(복사 뒤 이 레포 패치 적용, `VENDOR.json` 에 패치 해시) (c) 사본을 이 레포 소유 코드로 전환
  (`VENDOR.json` 은 출처 기록으로만, `test_vendor` 핀은 변경 시 갱신). 어느 쪽이든 `--check` 는 b54cb73 체크아웃 기준.
- 검증: 합성 프레임 테스트 + `%TEMP%\market-y2-corpus`(45창)·`market-y2-review`(11창) 오프라인 재생으로 9창 40페이지·라벨 8/8 재현.
  SEAssist 고정 아카이브(`15. Gersang auto eating\packet`)에는 손대지 않는다(머지 직전 복사 절차 폐기).

### 아이템 id → 이름 표 (2026-09-21 결정, PR-Y2b 이 레포 PR #5)
- **출처 = 클라 리소스** `C:\AKInteractive\Gersang\gersang.gcs` 안의 zlib 스트림 `;\t육의전 검색 기능 리스트`
  (`아이템 코드\t이름\t이름 코드`, 4,001행, id 1~13,590). 근거: 라벨 앵커 8/8, 관측 40프레임 188 id 중 186 해석(3251 은
  같은 아카이브의 `DelSocketItem` 표 — 강화품, v1.1 `TableSpec` 추가 자리; 1449232 는 미상 — 용병 탭 추정). 탈락: 라벨 누적
  (8건, 검증용으로만), 화면 OCR(한글 OCR 없음·관측기 import 경계), 웹(geota 403). SEAssist PROCESS §3 **H-2609-10**(C 제안).
- **위치 = 이 레포**(클라 데이터는 프로토콜이 아니라 벤더 규칙 밖): `src/yuktracker/item_names.py` — 순차 zlib 스캔(시그니처
  → inflate, 64KiB drain, 실패 시 1B 전진, 소비한 만큼 건너뜀; 2차 패스는 시그니처 위치마다 독립) → 선두 512B 마커(한글 제목
  또는 `#Item Code\tName\tName Code`) → 전량 inflate → 탭 표(`;`/`#` 주석 skip, 첫 id 우선) → 행 ≥1,000 이면 채택. 실측 0.22s.
  `paths.py`(`%APPDATA%\YukTracker`), `game_processes.process_image_path`(`QueryFullProcessImageNameW`, 64→32bit·비승격 OK),
  `tools/dump_item_names.py --check/--out/--ids/--ids-from/--search`.
- **해석 주체 = 관측기(업로드 때)**: 관측기 PC 에 클라가 있으니 설치된 빌드의 표로 행마다 `item_id` + `item_name`
  (미해석 null + 카운터 `market_unknown_item`)을 보낸다 — 패치 자동 추종. 캐시 `%APPDATA%\YukTracker\item_names.json`,
  유효성 = 크기 일치 + 같은 경로면 mtime 일치 / 다른 사본이면 전체 sha256 일치(실측 4ms), 아니면 재스캔(0.12s) — 크기가 같은
  패치도 mtime 으로 잡는다(PR #5 리뷰 반영 2026-09-22). 탐색: `--client-dir` 또는 `%YUKTRACKER_CLIENT_DIR%` 가 있으면 **그 폴더만**
  (gcs 없으면 미발견 — 다른 클라로 넘어가지 않음), 없으면 실행 중 `gersang.exe` 경로 → `C:\AKInteractive\Gersang*`.
- **정규화 한 정의**: 표시명 = strip → 선두 `[M]` 한 번 제거(게임 UI 와 같음; `[천권]`·`<삼족오>` 는 유지); 검색 키 =
  공백 전부 제거 + casefold — 허브 `item_name_norm`(`hub/db.py norm_item_name`)·봇 `_squash` 와 같아야 한다. 같은 이름의 여러 id 는 합집합.
- 인게임 `물품 목록` 창 열 ↔ 필드(스크린샷, 레포 밖): 물품명 ← `@4`+표 · 판매개수 ← `@8` · 판매자 ← `@24` · 단가 ← `@16`;
  구매개수·총가격 = UI 파생; **레벨 = 용병 탭 전용(추후)**; 기간(장기/단기) ↔ `@45` 후보(H-2609-11, 라벨 5열 캡처로 검정).

### PR-Y3 — 허브 `hub/` (2026-09-22 구현, 이 레포 — 계약 정본 `docs/HUB-PROTOCOL.md`)
- 독립 서버: `hub/server.py`(aiohttp, `/api/*` Bearer, 업로드 **전건 검증 후 트랜잭션 1개**, 일 1회 retention) + `hub/db.py`
  (sqlite3 WAL, `CREATE … IF NOT EXISTS` 만 — 배포 DB 무마이그레이션) + `requirements.txt`(aiohttp 하나) + `config.json.example` +
  `Dockerfile`/`docker-compose.yml`(포트 8800, `/data` 볼륨) + `README.md`(로컬·Pi 배포) + `hub/tests/`(14건, 합성 데이터).
  `hub/` 는 `src/` 를 import 하지 않는다(관측기 exe 와 별개 배포 단위; "런타임 의존성 0" 은 `src/yuktracker/` 규칙).
- 표: `market_observations(obs_id PK, device_id, recv_ts, agent_ts, seen_ts, opcode, page, total_pages, n_items, payload_json)` ·
  `market_listings(listing_key PK = "{listing_id}:{item_id}:{seller}", listing_id, item_id, item_name NULL 허용, item_name_norm, quantity,
  price, seller, category, flag45, flag46, first_seen_ts, last_seen_ts, seen_count, last_device, last_obs_id)` · 학습 표
  `market_item_names(item_id PK, item_name, item_name_norm, first/last_seen_ts, last_device)`. `item_id` 원값은 항상 저장.
- 라우트: `POST /api/market/observations`(배치 ≤100, 관측당 행 ≤64, `obs_id` dedup → 중복은 무변경) · `GET /api/market/search?q=&item_id=&
  limit=&max_age_sec=`(정규화 `instr` 부분일치, `price ASC`, `total_matches`) · `GET /api/market/listings?since_ts=&limit=`(엄격 초과·
  `next_since_ts`) · `GET /api/market/stats` · `GET /`(무인증 상태 줄).
- 시각·신선도: `seen_ts = min(agent_ts, recv_ts)`; upsert 는 "더 나중에 본 관측이 상태를 쓴다"(역순 스풀 업로드는 first_seen 만 앞당김);
  "사라짐" 판정 없음, `max_age_sec`(기본 24h) 밖은 숨기고 `last_seen_ts` 를 준다. 보존 30일(`retention_market_days`), 학습 표는 유지.

### PR-Y1b — 이 프로젝트, 관측 모드 (PR-Y2'·Y3 뒤)
- `market_cb(slot_idx, page, observation, *, pid)`(엔진이 이미 파싱한 `MarketPage`) → 행마다 `row_to_dict` + `item_name`
  (`item_names.load_item_table` 표, 미해석 null + 카운터 `market_unknown_item`) → 스풀 `%APPDATA%\YukTracker\spool\*.jsonl`
  (obs_id = `device:agent_ts_ms:sha1(body)[:12]`, 봉투에 `item_table{gcs_sha256, rows, archive_ts}`·`anomalies`·`hdr4`·
  `total_pages`) → 업로더 스레드 `urllib` POST(HUB-PROTOCOL §3-1: 200 뒤 삭제, 실패 지수 백오프, 400 은 격리). 콜백은 print + 큐
  enqueue 만(스니퍼 스레드에서 파일 쓰기 금지). 설정은 **이 프로젝트의** `%APPDATA%\YukTracker\config.json`(`hub_url`·`hub_secret`)
  + CLI 오버라이드(`--hub-url`·`--client-dir` 포함); 시작 줄 `[아이템표] N건 (gcs …)`·`[허브] <url>`, 주기적 `os.stat` 재검사로 패치
  추종. 미상 플래그(`flag45`·`flag46`·`unknown40`)는 이름 붙이지 않고 원값 그대로.
- 테스트: 스풀 영속·재시도(스레드 `http.server`)·중복 obs_id·파서 None 시 카운터만·표 없을 때 이름 null.

### 배포·실기기 게이트(G7)
1. 이 프로젝트 `build.bat` → 각 PC 에 `YukTracker.exe`(SEAssist 머지·배포 불필요).
2. Pi: `hub/` 복사 → `docker compose up -d --build`(별도 컨테이너, 8800) → `curl …:8800/api/market/stats`.
3. 사용자 1명이 육의전을 연다 → 관측기 상태 줄 → 허브 `search?q=<아이템>` 에 그 목록(≥1건 실발화).
   게이트: 승격·Npcap·캡처 시작 줄 / 육의전 열람 1회 = 관측 ≥1 / 허브 반영 ≤10s / 스풀 재시도 / 음성 0건.

### PR-Y4 — 미루봇-IRIS GS-01 (별도 레포·Codex 절차)
- `docs/GS-01_SOURCE_CONTRACT.md` 원본 정정: 이 레포 허브 market API(`http://127.0.0.1:8800`, Bearer, `docs/HUB-PROTOCOL.md`).
- 통합 클라이언트 + env `MIRUBOT_MARKET_API_ORIGIN/SECRET`(fail-closed).
- `yukeuijeon_search`(`!육의전 <아이템>` → `🏪 육의전 검색: {kw} ({n}건)` + `{item} | {qty}개 | {price:,}원 |
  {seller} | {N분 전}` + 신선도 줄 — `count < total_matches` 면 "오래된 N건 숨김"), `yukeuijeon_alarm`(등록·해제·목록,
  additive 스키마·ADR) + job `geosang.yukeuijeon_poll`(60s, `/api/market/listings?since_ts=` → `next_since_ts` 저장 → 매칭 →
  기존 내구 발송 경로).
  (2026-09-21: `Lv.`(용병) 표시는 용병 탭 캡처·가설 뒤로 보류 — 이번 파서는 아이템 탭만; `기간`(장기/단기)은 H-2609-11 검정 뒤 표시 후보.)

## 4. 가정·미결
- 목록은 8000 s2c `0x321f`(확정, B). 요청(c2s)은 암호화라 조회 조건은 응답에서만.
- 모든 사용자가 같은 서버(세종)라고 가정하되 관측에 `server_ip` 를 남긴다.
- 가격 LE u32(확정), 문자열 cp949. 수량 ≥65,536·타 PC 세션 표본은 A 승격용으로 남아 있다.
- 클라 패치로 번호가 이동할 수 있다(`0x30dd→0x30df` 전례) — 헬스 `market_frames=0` 지속 + 열람 보고가
  PATCH-RECHECK 트리거(이 레포 `docs/PACKET-MARKET.md` 에서 관리).
- 스풀·창 파일·허브 DB 에 타 유저 판매자명이 남는다 — 레포 픽스처는 합성으로, 운영 파일은 로컬·업로드 후 삭제, 허브 DB 는 Pi 볼륨.

## 5. 순서 요약
Step 0 수집 ∥ PR-Y1(완료) → Step 1 분석(완료) → PR-Y2(SEAssist #306, 미머지) → PR-Y2b(#5) → **PR-Y3 허브(이 레포)** →
PR-Y2' 이식 → PR-Y1b → 배포·G7 → PR-Y4.
