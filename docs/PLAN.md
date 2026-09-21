# 육의전 패킷 → 공유 DB → 미루봇 `!육의전` — 구현 계획 (2026-09-21, 승인)

> 원본은 Claude Code 플랜(`C:\Users\A\.claude\plans\system-reminder-you-are-operating-splendid-globe.md`).
> 2026-09-21 사용자 지시로 관측기를 SEAssist 레포 안이 아니라 **이 프로젝트(별도 실행파일)** 에 두는 것으로
> 바뀌었다 — 아래는 그 정정을 반영한 판이다.

## 1. Context

- **육의전**은 유저가 판매 아이템을 등록하는 인게임 거래소. 목록은 **유저가 직접 육의전을 열 때만** s2c
  패킷으로 내려온다. 다른 패킷 기반 서드파티(geota 등)가 이미 이걸로 시세를 만든다.
- 미루봇-IRIS(카카오톡)의 **GS-01 육의전 검색·알람**은 원본을 geota 웹으로 잡았다가 HTTP 403 에 막혀 대기
  중(`32. 미루봇-IRIS/docs/GS-01_SOURCE_CONTRACT.md`). 파일럿 봇의 `!육의전 <아이템>`·`!알람등록/해제/목록`
  명령·출력 형식(`27. IRIS/bots/gersang_bot.py:50-71`)을 계승한다.
- 목표: **1~7명의 클라에서 육의전 패킷을 관측 → 파싱 → 공유 저장소에 합류 → 미루봇이 읽어 `!육의전` 을
  켠다.** 육의전 DB 는 "누군가 마지막으로 본 것"의 부분·지연 스냅샷이므로 **관측 시각**을 항상 함께 다룬다.
- 사용자 결정(2026-09-21): ① 소비자 봇 = 미루봇-IRIS ② 허브 = SEAssist 대시보드 서버(Pi) 확장 ③ 수집기 =
  **별도 경량 프로그램(이 프로젝트)** ④ opcode·필드는 모름 → 발굴부터.
- 규칙: 새 opcode 는 SEAssist `docs/PACKET-PROCESS.md` §3 레지스트리 ID(**H-2609-07**) 없이 FINDINGS 에
  못 들어가고, 등급 D 로는 런타임 판정에 못 들어간다. 이 트랙의 런타임은 관측 전용(게임 입력 없음)이라
  §2 표의 **B(관측 전용 배선)** 가 목표 등급.

## 2. 목표 구조

```
[거상 클라 ×N / PC] ─ s2c 8000 (평문) ─▶ Npcap ─▶ YukTracker (이 프로젝트: 경량 콘솔, 관리자, GUI·러너 없음)
                                                   ├─ --capture : 원시 창 window_*.jsonl (발굴, SEAssist PacketDiscoveryRecorder 벤더 사본)
                                                   └─ 관측 모드 : FlowDecoder → 육의전 프레임 전량 대기 → packet_market.parse
                                                                  → 로컬 스풀(JSONL) → HTTP POST /api/market/observations (Bearer)
[대시보드 서버 Pi:8799 (SEAssist dashboard/, aiohttp+sqlite)] market_observations · market_listings + GET /api/market/search · /listings · /stats
[미루봇-IRIS (같은 Pi)] Geosang GS-01: 대시보드 market 클라이언트 → !육의전 검색 / !알람등록·해제·목록 / 알림 job
```

- SEAssist GUI 의 패킷 엔진은 건드리지 않는다(세션 중에만 뜨는 구조 그대로). 관측은 이 프로그램이 전담.
- 패킷 코어는 SEAssist 레포의 **벤더 사본**(`tools/sync_seassist_core.py`, `VENDOR.json` 에 출처 커밋·해시).
  프로토콜 정본·코퍼스 테스트는 SEAssist 에 남고, 육의전 파서도 거기서 들어온 뒤 동기화로 가져온다.
- 에이전트→서버는 WS 이벤트(`_PENDING_MAX=256`, 최고령 유실)가 아니라 **HTTP POST + 로컬 스풀**로
  at-least-once.

## 3. 단계와 PR

### Step 0 — 수집(코드 0줄) → G0
이 프로그램(`run_dev.bat` / `YukTracker.exe --capture 5`) 또는 SEAssist GUI `[패킷 수집]`(매크로 세션 중)로:
**정지 30s → 육의전 열기 → 잘 안 팔리는 고유한 아이템명 1개 검색 → 페이지 1..N → 카테고리 열람 → 빈 결과
검색 1회 → 닫기 → 정지 30s.** 콘솔 라벨(`market_manual`)로 각 동작 시각을 남긴다.
- 표본 요건(PROCESS §2): 양성 ≥3 창·서로 다른 세션/PC(B 등급은 ≥5) + 음성 ≥2 창(상점·창고·우편처럼 목록 UI
  는 열지만 육의전은 아닌 창 — 일반 사냥 창은 기존 36창 코퍼스가 이미 음성).
- 값 스윕: 가격 ≥65,536(u16/u32 경계)·수량 1 vs 다수·용병(Lv) vs 물품·긴 아이템명.

### Step 1 — 분석(G1~G5) → SEAssist `docs/PACKET-MARKET-2026-09-XX.md`
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
산출: PROCESS §4 규격의 필드 표, 페이지·count·문자열 형식 판정, 열거 opcode 집합. 후보 필드: 목록 id,
아이템 id/이름, 수량(또는 Lv), 가격, 판매자, 카테고리, 등록 시각, 총건수/페이지.

### PR-Y1 — 이 프로젝트, 수집 모드 (2026-09-21 골격 완료)
- `src/yuktracker/cli.py`(인자·UAC 승격) · `agent.py`(`PidIndexer`·엔진 배선·수집 창·콘솔 라벨) ·
  `game_processes.py`(Toolhelp32 → `gersang.exe`) · `seassist/`(벤더 사본 + `config.py` 심).
- SEAssist 선행 변경(같은 날): `ledger_paths.py` 분리·`packet_state_source` numpy 지연 import·
  `admin.relaunch_as_admin(module=)` — 사본이 cv2·genai 없이 돌기 위한 것.
- 배포: `build.bat` → `dist/YukTracker.exe`(UAC 매니페스트). 공유 방법(OneDrive 폴더·직접 전달)은 사용자 결정.

### PR-Y2 — SEAssist 프로토콜·파서·원장·코퍼스 (Step 1 확정 뒤) → 동기화로 가져옴
- `gersang_protocol.py`: `MARKET_OPCODES`(관측된 번호만), `StreamFramer(observe_market=)` 전량 대기 분기,
  `MarketObservation` 큐 + `take_market()`; `FlowDecoder` 통과. ENTER 조기 발화·락·앵커 무접촉.
- `packet_market.py`(순수·stdlib, 검증된 배치만 수용 — `packet_death.py` 선례): `parse_market_page(body)`.
- `PacketStateSource(market_cb=)` + 헬스 카운터; `PacketShadowLedger.note_market()` + `packet_explore.py market`
  판독 커맨드(같은 PR); `check_packet_corpus.py` COUNTS + manifest 등록(G6).
- 문서: FINDINGS 새 절, PROCESS §3 승격, TOOLS, CHANGELOG.

### PR-Y3 — SEAssist `dashboard/` market API (PR-Y2 와 병행, 필드는 Step 1 뒤 확정)
- `db.py`: `market_observations(obs_id PK, device_id, recv_ts, agent_ts, opcode, page, n_items, payload_json)`
  (INSERT OR IGNORE 재전송 dedupe), `market_listings(listing_key PK, item_name, item_name_norm, quantity, price,
  seller, category, listing_id, first_seen_ts, last_seen_ts, seen_count, last_device, last_obs_id)` + 인덱스,
  `prune()` 확장(config `retention_market_days`).
- `server.py`: `POST /api/market/observations`, `GET /api/market/search?q=&limit=&max_age_sec=`(공백 제거+
  casefold 부분일치), `GET /api/market/listings?since_ts=`, `GET /api/market/stats`. 뷰어 UI 무변경(v1).
- 신선도(v1): "사라짐" 판정 없음(요청은 8000 c2s 암호화). `max_age_sec`(기본 24h) 밖은 숨기고 `last_seen_ts`
  를 준다. 응답이 총건수/조회조건을 실으면 스냅샷 diff 로 고도화 — FOLLOWUP.
- `dashboard/tests/test_market.py`; DASHBOARD-PROTOCOL/PLAN §6/README 배포 절차.

### PR-Y1b — 이 프로젝트, 관측 모드 (PR-Y2·Y3 뒤)
- `market_cb` → `packet_market.parse_market_page` → 스풀 `%APPDATA%\YukTracker\spool\*.jsonl`
  (obs_id = `device:agent_ts_ms:sha1(body)[:12]`) → 업로더 스레드 `urllib` POST(성공 삭제·실패 지수 백오프).
  설정은 SEAssist `settings.json` 의 `dashboard_server_url/dashboard_secret` 재사용 + CLI 오버라이드.
- 테스트: 스풀 영속·재시도(스레드 `http.server`)·중복 obs_id·파서 None 시 카운터만.

### 배포·실기기 게이트(G7)
1. SEAssist main 머지·배포 → 이 프로젝트 `build.bat` → 각 PC 에 `YukTracker.exe`.
2. Pi: `dashboard/` 재복사 → `docker compose up -d --build` → `curl …/api/market/stats`.
3. 사용자 1명이 육의전을 연다 → 관측기 상태 줄 → 서버 `search?q=<아이템>` 에 그 목록(≥1건 실발화).
   게이트: 승격·Npcap·캡처 시작 줄 / 육의전 열람 1회 = 관측 ≥1 / 서버 반영 ≤10s / 스풀 재시도 / 음성 0건.

### PR-Y4 — 미루봇-IRIS GS-01 (별도 레포·Codex 절차)
- `docs/GS-01_SOURCE_CONTRACT.md` 원본 정정: SEAssist 대시보드 market API(127.0.0.1:8799, Bearer).
- 통합 클라이언트 + env `MIRUBOT_MARKET_API_ORIGIN/SECRET`(fail-closed).
- `yukeuijeon_search`(`!육의전 <아이템>` → `🏪 육의전 검색: {kw} ({n}건)` + `{item} | {qty|Lv.} | {price:,}원 |
  {seller} | {N분 전}` + 신선도 줄), `yukeuijeon_alarm`(등록·해제·목록, additive 스키마·ADR) + job
  `geosang.yukeuijeon_poll`(60s, `/api/market/listings?since_ts=` → 매칭 → 기존 내구 발송 경로).

## 4. 가정·미결(발굴 결과로 확정)
- 목록이 8000 s2c 에 실린다(4011 이면 `probe` 로 프레이밍부터). 요청(c2s)은 암호화라 조회 조건은 응답에서만.
- 모든 사용자가 같은 서버(세종)라고 가정하되 관측에 `server_ip` 를 남긴다.
- 가격 LE u32 예상(경계값 표본으로 확정), 문자열 cp949.
- 클라 패치로 번호가 이동할 수 있다(`0x30dd→0x30df` 전례) — 헬스 `market_frames=0` 지속 + 열람 보고가
  PATCH-RECHECK 트리거.
- 스풀·창 파일에 타 유저 판매자명이 남는다 — 레포 픽스처는 합성으로, 운영 파일은 로컬·업로드 후 삭제.

## 5. 순서 요약
Step 0 수집(즉시) ∥ PR-Y1(완료 골격 → 실기기 확인) → Step 1 분석 → PR-Y2 ∥ PR-Y3 → PR-Y1b → 배포·G7 → PR-Y4.
