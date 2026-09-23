# 육의전 시세 허브 — 프로토콜 (HUB-PROTOCOL v1, 2026-09-22)

관측기(YukTracker, PR-Y1b) → 허브(`hub/`, Pi:8800) → 미루봇-IRIS(PR-Y4) 사이의 계약. 구현은 `hub/server.py`·`hub/db.py`·
`hub/devices.py`, 테스트 `hub/tests/`. 이 문서가 정본이고 코드가 어긋나면 코드가 틀린 것이다.

## 0. 공통 규칙

- 전송: JSON(UTF-8) over HTTP/1.1. 허브는 리스너 둘을 연다 — **관리 리스너** `port`(8800): 전 라우트, Pi 안(봇 127.0.0.1)·LAN·
  tailnet 의 직접 접속용(평문 HTTP, Funnel 을 걸지 않는다). **공개 리스너** `public_port`(8801): **Tailscale Funnel** 이 가리키는
  포트(`https://<pi-node>.<tailnet>.ts.net:10000/` → Pi 의 127.0.0.1:8801, 공유기 포트포워딩 없음; Funnel 공개 포트는 443/8443/10000 중
  하나라 같은 Pi 의 다른 서비스와 겹치지 않는 것을 쓴다 — 관측기는 주소를 통째로 받으므로 포트는 계약이 아니다) — 관측기가 **Npcap 만 있는 일반
  사용자 PC** 에서 올려야 하므로 VPN 은 전제하지 않는다(2026-09-22 결정). 자격은 두 종류:
  - **관리 시크릿** `secret` — 조회(`search`·`listings`·`stats`)와 제작자 업로드. **Funnel 을 타지 않는다**: exe 에 넣지 않고,
    공개 요청에서는 어느 라우트에서도 인정하지 않는다(`admin_public` false).
  - **기기 토큰** — 초대 코드로 자기등록(§3-0)한 기기마다 1개. `POST /api/market/observations` 에만 통한다.

  **공개 요청** 판정 = ① 공개 리스너로 온 요청은 헤더와 무관하게 전부, ② 관리 리스너에서는 tailscaled 가 Funnel 요청에 붙이는
  `Tailscale-Funnel-Request` 헤더(`proxy_header`)의 **존재**(값 무관). ①이 1차 방어(헤더 이름 오타·프록시 교체와 무관), ②는 8800 에
  Funnel 을 잘못 걸었을 때의 2차 방어 — tailscaled 는 클라이언트가 보낸 같은 이름의 헤더를 지운 뒤 붙이므로 공개 쪽에서 위조·제거가
  안 되고, 직접 접속 클라이언트가 붙이면 스스로 더 제한될 뿐이다. 속도제한·로그의 클라이언트 IP 는 공개 요청이면 `X-Forwarded-For`
  **마지막** 항목(프록시가 붙인 원 IP), 없으면 `public:?` 한 버킷(경고 1회), 직접 접속이면 소켓 peer — 컨테이너 안에서는 docker
  브리지 IP 라 봇·Funnel 프록시가 같아 공개 요청의 키로 쓰지 않는다.

  | 라우트 | 공개 요청 | 자격 |
  |---|---|---|
  | `GET /` | 허용 | 없음 |
  | `POST /api/market/register` | 허용 | 슬롯 초대 코드(본문) + IP 속도제한 |
  | `POST /api/market/observations` | 허용 | 기기 토큰 `Authorization: Bearer <token>`; 직접 접속이면 관리 시크릿도 |
  | `GET /api/market/ping` | 허용 | 위와 같음 — 토큰 확인 전용(§3-7), 자가진단이 쓴다 |
  | `GET /api/market/search` `listings` `stats` | **`403 {"ok":false,"error":"not_public"}`**(자격 검사 전) | 관리 시크릿 `Authorization: Bearer <secret>` |

  자격 없음·불일치·범위 밖(기기 토큰으로 조회, 공개 요청의 관리 시크릿)은 전부 `401 {"error":"unauthorized"}`.
- 속도제한(프로세스 메모리, 재기동이면 리셋; 두 리스너가 버킷을 공유): 등록 IP 당 `register_limit_per_hour`(10)·등록 **슬롯당**
  `register_slot_limit_per_hour`(5, 성공한 교체만 — 새어 나간 코드로 한 슬롯을 계속 갈아치우지 못하게), 업로드 기기 당
  `upload_limit_per_min`(120 — 관리 시크릿은 무제한; 취소 검사보다 먼저라 취소된 클라의 폭주도 기기 단위로 429), 인증 실패 **공개
  요청** IP 당 `auth_fail_limit_per_min`(30) — 토큰 검사 **뒤** 실패에만 센다(공유 NAT 뒤의 고장난 클라 하나가 같은 IP 의 정상
  기기를 막지 않게; 직접 접속은 브리지 IP 를 봇과 공유하므로 세지 않는다). 초과는 `429 {"ok":false,"error":"rate_limited",
  "retry_after":N}` + `Retry-After: N` — N 초 뒤 재시도(§3-1 관측기 규약).
- **additive-only**: 필드는 더하기만 하고 이름·의미를 바꾸지 않는다. 읽는 쪽은 미지 키를 무시한다(tolerant reader).
  응답의 `"v": 1` 은 이 문서의 판.
- 시각은 전부 Unix epoch 초(float). 서버 응답엔 항상 `server_time` 이 있어 클라이언트 시계 오프셋을 보정할 수 있다.
- 판매자명 등 실데이터는 허브 DB(Pi 로컬)에만 있다. 문서·PR·테스트는 합성값(`판매자A`, `DEV-1`)만 쓴다.
  **`device_id`(= 기본값이 그 PC 의 hostname)·허브 주소·Pi 사용자명도 실데이터다** — `stats.devices` 응답이나
  `market 관측 수신` 로그를 문서·PR 에 붙여넣을 때 기기명을 `DEV-1`/`<디바이스>` 로 바꾼다.

## 1. 데이터 모델 (`hub/db.py` SCHEMA — 배포 DB 무마이그레이션, `CREATE … IF NOT EXISTS` 만)

```sql
CREATE TABLE market_observations (      -- 관측기가 올린 페이지(프레임) 1개 = 1행
  obs_id TEXT PRIMARY KEY,              -- 재전송 dedup 키 (관측기가 만든다, 아래 §3-1)
  device_id TEXT NOT NULL,
  recv_ts REAL NOT NULL, agent_ts REAL NOT NULL,
  seen_ts REAL NOT NULL,                -- = min(agent_ts, recv_ts)  (§4)
  opcode INTEGER NOT NULL, page INTEGER, total_pages INTEGER, n_items INTEGER NOT NULL,
  payload_json TEXT NOT NULL);          -- 관측 봉투(anomalies·hdr4·item_table…). rows 는 정규화된 키를 뺀 미지 키만(§3-1)
CREATE TABLE market_listings (          -- 등록(판매 건) 1개의 최신 상태
  listing_key TEXT PRIMARY KEY,         -- "{listing_id}:{item_id}:{seller}"
  listing_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
  item_name TEXT, item_name_norm TEXT,  -- 관측기가 해석한 표시명 / 검색 키 (미해석이면 NULL)
  quantity INTEGER NOT NULL, price INTEGER NOT NULL, seller TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'item',
  flag45 INTEGER, flag46 INTEGER,       -- 원값 그대로 (H-2609-11 후보, 이름 안 붙임)
  first_seen_ts REAL NOT NULL, last_seen_ts REAL NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
  last_device TEXT NOT NULL, last_obs_id TEXT NOT NULL);
CREATE TABLE market_item_names (        -- 학습 표: 관측기가 보낸 id→이름 누적
  item_id INTEGER PRIMARY KEY, item_name TEXT NOT NULL, item_name_norm TEXT NOT NULL,
  first_seen_ts REAL NOT NULL, last_seen_ts REAL NOT NULL, last_device TEXT NOT NULL);
CREATE TABLE devices (                  -- 초대 코드로 자기등록한 관측기 기기 (§3-0); prune 대상 아님
  device_id TEXT PRIMARY KEY,           -- 허브 발급 "d-"+10hex — 사용자 PC 이름은 다른 사용자와 겹칠 수 있어 식별자로 안 쓴다
  label TEXT NOT NULL DEFAULT '',       -- 기기가 등록 때 스스로 적은 표시명, 인쇄 가능 문자 ≤64 (신뢰하지 않는다)
  alias TEXT NOT NULL DEFAULT '',       -- 관리자가 붙이는 별칭(§3-6 devices.py alias) — 표시는 이쪽을 앞세운다; 초기값 = 슬롯 이름
  slot TEXT NOT NULL DEFAULT '',        -- 등록에 쓴 초대 코드의 슬롯 이름(설정 invite_codes 의 키, 2026-09-23) — 기존 DB 엔 ADD COLUMN
  token_hash TEXT NOT NULL UNIQUE,      -- sha256(token) hex — 토큰 원문은 등록 응답 1회만, 저장하지 않는다
  created_ts REAL NOT NULL, created_ip TEXT,
  last_seen_ts REAL, upload_count INTEGER NOT NULL DEFAULT 0,   -- 인증된 업로드마다 last_seen, 저장 성공이면 +1
  revoked_ts REAL, note TEXT NOT NULL DEFAULT '');              -- 제거 = 다음 요청부터 403 (요청마다 조회, 캐시 없음)
```

- `listing_key` 에 아이템 id·판매자를 붙이는 이유: 등록 id 가 재사용돼도 다른 판매 건이 섞이지 않게. 같은 등록의
  재관측(수량 감소 등)은 같은 키로 upsert 된다.
- upsert 규칙 — **더 나중에 본 관측이 상태를 쓴다**: 새 관측의 `seen_ts` 가 기존 `last_seen_ts` 이상이면
  quantity/price/category/flag/last_device/last_obs_id 를 덮고, 더 오래된 관측이 늦게 올라오면 `first_seen_ts` 만
  앞당긴다. `item_name` 도 같은 규칙 — 새 관측의 이름이 NULL 이 아니고(기존이 NULL 이거나 새 관측이 더 나중이면) 덮는다:
  늦게 도착한 옛 이름이 최신 이름을 덮지 않고, NULL 은 있는 이름을 지우지 않는다. `seen_count` 는 신규 관측마다 +1
  (중복 obs_id 는 가산 없음). 인덱스 `(last_seen_ts, listing_key)` 가 §3-3 커서를 받친다.
- 이름 학습: 행에 `item_name` 이 있으면 `market_item_names` 에 upsert. 조회 때 행의 `item_name` 이 NULL 이면 이 표로
  보충한다(`COALESCE`) — 표가 없는 PC 의 관측도 다른 PC 가 한 번이라도 이름을 보냈으면 검색된다.
- 기기 표: market 표는 서버 이벤트루프만 쓰지만 `devices` 는 관리 CLI `hub/devices.py`(별도 프로세스, §3-6)가 드물게 한 행 UPDATE 를
  넣는다 — WAL + busy timeout. 제거된 기기·재등록으로 버려진 옛 행은 지우지 않는다(`stats` 에 남아 추적). **"등록"의 정의는 하나**
  (`_ACTIVE_WHERE`, `stats.devices_registered`): `revoked_ts IS NULL`. **업로드 여부·마지막 업로드 시각은 명단에 영향을 주지 않는다**
  — 2026-09-22 사용자 결정으로 활성 여부에 따른 자동 제외는 두지 않는다(아는 사람 최대 7명에게 직접 배포). 명단에서 빠지는 길은
  둘뿐이다: 관리자의 `devices.py revoke`(§3-6)와 **같은 슬롯 코드의 재등록 교체**(§3-0, 2026-09-23) — 정원은 슬롯 수다.
- 스키마 변경 규칙의 유일한 예외: `devices.slot` 은 기동 때 `PRAGMA table_info` 로 없으면 `ALTER TABLE … ADD COLUMN`(DEFAULT '')
  한다(additive). 열을 지우거나 바꾸는 마이그레이션은 여전히 없다.

## 2. 정규화 한 정의

`norm(name) = "".join(name.split()).casefold()` — **공백 전부 제거 + casefold**. 허브가 저장(`item_name_norm`)과
검색(`q`) 양쪽에 같은 함수를 쓰므로 소비자는 원문 그대로 보내도 된다(봇 `_squash` 의 공백 제거는 이 규칙의 부분집합).
표시명의 선두 `[M]` 제거는 관측기(`item_names.py`) 몫 — 허브는 받은 이름을 그대로 저장한다. 매칭은 정규화 키의
**부분 문자열**(`instr`), 예: `봉인의 돌` → `봉인의돌` 은 `봉인의돌`·`봉인의돌(상)` 에 맞는다.

## 3. REST

### 3-0. `POST /api/market/register` — 슬롯 초대 코드 → 기기 토큰 (무 Bearer, 공개)

```jsonc
// → 요청
{"v":1, "invite_code":"<그 사람의 슬롯 초대 코드>", "label":"DESKTOP-ABC"}   // label 선택(표시용, 인쇄 가능 문자 ≤64, 없음·null = "")
// ← 200
{"ok":true,"v":1,"device_id":"d-3f9a1c7e2b","token":"<43자 urlsafe>","slot":"slot3","replaced":["d-0a1b2c3d4e"],"server_time":1758500000.5}
```

초대 코드는 **슬롯별**이다(2026-09-23 사용자 결정 — 설정 `invite_codes = {슬롯 이름: 코드}`, 슬롯 = 지인 한 사람, 정원 = 슬롯 수):
코드가 어느 슬롯인지 정하고, 기기 행에 `slot` 과 초기 `alias`(= 슬롯 이름)가 들어가 등록 즉시 명단에서 누구인지 보인다. **같은 슬롯
코드로 다시 등록하면**(재설치·PC 교체) 새 기기가 슬롯을 받고 그 슬롯의 등록(미제거) 기기는 **같은 트랜잭션에서 제거**된다
(`revoked_ts`, note 는 관리자 메모 뒤에 `/ 재등록 교체 → <새 id>` 를 **덧붙임**, 응답 `replaced` 에 id) — 관리자 개입 없이 끝나고 옛 exe 가
남아 있어도 다음 업로드부터 `403 device_revoked`. 새 기기의 `alias` 는 교체된 기기의 별칭을 **승계**(관리자가 붙인 이름이 재설치마다
사라지지 않게), 없으면 슬롯 이름. 교체는 `slot` 으로 찾는다 — **슬롯 이름은 기기의 정체성**이라 config 에서 이름을 바꾸면 옛 이름의
기기는 교체 대상에서 빠져 계속 올린다(기동 경고 + `stats.devices_orphaned`; 옛 기기를 `revoke` 하거나 이름을 되돌린다). 슬롯 없이 만든
행(수동 삽입)도 마찬가지로 교체되지 않는다. 조회~삽입~제거는 `BEGIN IMMEDIATE` 한 트랜잭션(동시 `devices.py unrevoke` 와 안 엇갈린다).

검사 순서(고정): ① IP 속도제한 — 성공·실패 모두 센다(코드 비교 전에 무차별 대입 상한) → ② 본문 크기(파싱 전) → ③ 등록 닫힘 →
④ 본문 → ⑤ 코드(앞뒤 공백을 벗긴 뒤 슬롯 수만큼 상수 시간 비교, 조기 종료 없음) → ⑥ 슬롯당 속도제한(`429 rate_limited`) → ⑦ 발급 +
같은 슬롯 교체. 정원 검사는 없다.

| 응답 | 조건 |
|---|---|
| `429 rate_limited` + `Retry-After` | IP 당 `register_limit_per_hour` 초과 |
| `411 {"ok":false,"error":"length_required"}` / `413 … "too_large"` | `Content-Length` 없음 / 4KiB 초과 — 무인증 라우트라 큰 본문은 읽지 않는다 |
| `403 {"ok":false,"error":"registration_closed"}` | 설정 `invite_codes` 가 비어 있음 |
| `400 {"ok":false,"error":"bad_request","field"?}` | 비 JSON·dict 아님 / `invite_code` 가 문자열 아님·공백뿐 / `label` 이 문자열 아님·64자 초과·제어 문자 |
| `401 {"ok":false,"error":"bad_invite"}` | 어느 슬롯의 코드와도 불일치 |
| ~~`403 registration_full`~~ | 2026-09-23 폐지 — 정원은 슬롯 수이고 같은 슬롯은 교체된다. 관측기의 안내 문장은 옛 판 허브 호환용으로 남아 있다 |
| `500 {"ok":false,"error":"storage_error","server_time"}` | DB 오류 |

- `device_id` 는 허브가 발급한다. 관측기는 `device_id`·`token` 을 저장하고 이후 업로드의 `device_id` 에 **그 값**을 쓴다(§3-1) —
  초대 코드는 저장하지 않는다(재등록은 명시적 `--invite-code` 실행에서만).
- 등록은 비멱등: 재등록(재설치·토큰 분실)은 새 기기이고 옛 행은 제거된 채 남는다(감사용). 200 뒤 저장 실패 같은 고아 행은
  `last_seen_ts` null 로 보이고 취소하면 된다.
- 취소(§3-6)는 **soft** — 같은 슬롯 코드로 다시 등록할 수 있다. 남용은 그 슬롯의 코드 교체(설정 + 재기동): 기존 토큰은 살고 신규
  등록만 막힌다. 슬롯을 표에서 지우면 그 코드로는 등록이 안 되지만 이미 발급된 토큰은 산다(`revoke` 로 끊는다).

### 3-1. `POST /api/market/observations` — 관측기 업로드 (at-least-once, 배치)

```jsonc
// → 요청 (관측 1~100건, 관측당 행 0~64)
{"v":1, "device_id":"DEV-1",
 "observations":[
   {"obs_id":"ab12cd34:1758500000123:0123456789ab",    // "{local_id}:{agent_ts_ms}:{sha1(body)[:12]}" — 관측기가 만든다
    "agent_ts":1758500000.123, "opcode":12831,         // 0x321f
    "page":null, "total_pages":12, "hdr4":0,           // page: 응답엔 페이지 번호가 없다(null 허용)
    "anomalies":[],                                     // 파서 anomaly 이름들 (있으면 그대로)
    "item_table":{"gcs_sha256":"…","rows":4001,"archive_ts":"2026-09-21T02:04:11Z"},  // 이름 해석에 쓴 표 (null 허용)
    "rows":[                                            // SEAssist packet_market.row_to_dict 키 + item_name
      {"listing_id":700001,"item_id":853,"item_name":"봉인의돌","quantity":10,"quantity_hi":0,
       "price":45000000,"price_hi":0,"seller":"판매자A","unknown40":"00000000","flag45":2,"flag46":0}]}]}
// 관측기 구현: yuktracker/market_observer.py(봉투) · spool.py(스풀·업로더) · hub_client.py(요청·응답 해석)
// ← 200
{"ok":true,"accepted":1,"duplicates":0,"rows":1,"server_time":1758500001.5}
```

| 필드 | 형 | 규칙 |
|---|---|---|
| `device_id` | str 1~101 | 기기 토큰이면 §3-0 이 발급한 `device_id`(다르면 `403 device_mismatch`), 관리 시크릿이면 자유 문자열(기기명). 요청 단위. **상한이 `obs_id` 보다 27 작다** — `obs_id` 가 `{device}` 에 `:{13자리 ms}:{12자리 해시}` 를 붙이기 때문(허브 발급 id 는 12자라 무관, 자유 문자열을 쓰는 쪽만 101자로 자른다) |
| `obs_id` | str 1~128 | PK. 같은 값 재전송은 `duplicates` 로 세고 **아무것도 바꾸지 않는다** |
| `agent_ts` | number | 관측기 벽시계(프레임 수신 시각). 서버는 `seen_ts = min(agent_ts, recv_ts)` 로 쓴다. 허용 범위 `[recv_ts − retention_market_days, recv_ts + max_agent_ts_ahead_sec(86400)]` — 밖이면 `400 agent_ts_out_of_range`(시계가 리셋된 기기의 관측이 200 을 받고 조회에 안 보이다 prune 에 사라지는 것을 막는다) |
| `opcode` | int | 관측 프레임 opcode(현재 `0x321f`) |
| `page` `total_pages` `hdr4` | int \| null | 헤더 값. 페이지 번호는 응답에 없어 `page` 는 보통 null |
| `rows[].listing_id/item_id/quantity/price` | int, 0 ≤ v ≤ 2⁶³−1 | 필수. bool 은 거부. 모든 int 는 SQLite INTEGER(i64) 범위 안 — 밖이면 400(검증 통과 뒤 500 이 나면 관측기가 영원히 재시도한다) |
| `rows[].seller` | str ≤128 | 필수(cp949 해독, 첫 NUL 까지). `listing_key` 에 들어간다 |
| `rows[].item_name` | str ≤128 \| null | 관측기가 클라 표로 해석한 표시명. 못 풀면 null(카운터 `market_unknown_item`) |
| `rows[].flag45/flag46` | int \| null | 원값 그대로 |
| `rows[].category` | str ≤32, 기본 `item` | 용병 탭 등은 추후(현재 관측기는 보내지 않는다) |
| 그 밖의 키 | — | 무시하되 `payload_json` 에 보존 — 봉투 수준 키는 그대로, **행은 위 정규화 키를 뺀 미지 키만**(`quantity_hi`·`unknown40`…; 행 본문은 `market_listings` 에 있으니 두 번 쓰지 않는다) |

| 응답 | 조건 |
|---|---|
| `200 {"ok":true,"accepted","duplicates","rows","server_time"}` | 전건 검증 통과 → 트랜잭션 1개로 반영 |
| `400 {"ok":false,"error":"bad_request","index"?,"field"?}` | 비 JSON / dict 아님 / 필수 필드 형·범위·길이 위반 — `index` 는 관측 순번, `field` 는 `rows[3].price` 꼴. **그 요청은 아무것도 쓰지 않는다** |
| `400 … "error":"too_many"` | 관측 >100 또는 관측당 행 >64 (`max_observations_per_request`·`max_rows_per_observation`) |
| `400 … "error":"agent_ts_out_of_range","index","field":"agent_ts"` | `agent_ts` 가 허용 범위 밖(관측기 시계 리셋·폭주) — 관측기는 그 배치를 격리하고 시계를 의심한다 |
| `401 {"error":"unauthorized"}` | 토큰 없음·무효, 공개 요청의 관리 시크릿 |
| `429 {"ok":false,"error":"rate_limited","retry_after":N}` + `Retry-After` | 기기 당 `upload_limit_per_min` 초과(관리 시크릿은 무제한) — 취소 검사보다 먼저 |
| `403 {"ok":false,"error":"device_revoked"}` | 기기 토큰이 취소됨(§3-6) — 관측기는 업로드를 멈추고 사용자에게 알린다 |
| `403 {"ok":false,"error":"device_mismatch","device_id":"<기대값>"}` | 기기 토큰인데 본문 `device_id` 가 토큰의 기기와 다름(400 검증 뒤) — 관측기 설정 불일치 |
| `413` | 본문 4MiB 초과(aiohttp) — 관측기는 배치를 나눈다 |
| `500 {"ok":false,"error":"storage_error","server_time"}` | 검증 통과 뒤 DB 오류(디스크·잠금) — 관측기는 5xx 로 재시도 |

관측기 규약(PR-Y1b): 스풀 파일을 성공 응답(200) 뒤에만 지운다. 네트워크 실패·5xx 는 지수 백오프 재시도, 400 은 그 배치를
격리 폴더로 옮기고 로그를 남긴다(재시도해도 같은 400). **429 는 `Retry-After` 초 기다렸다 재시도**(스풀 유지, 격리 아님).
**401·403 은 업로더를 멈추고**(스풀 유지) 상태 줄에 사유를 보인다 — 자동 재등록 금지, 사용자가 `--invite-code` 로 다시 등록한다;
`device_mismatch` 는 응답의 `device_id` 와 설정을 대조한다. `device_id` 는 스풀 파일이 아니라 **POST 시점**에 채운다(재등록 뒤
옛 스풀이 영구 mismatch 되지 않게). 같은 `obs_id` 가 두 번 가는 것은 정상(허브가 dedup).

`obs_id` 의 접두사는 허브가 발급한 `device_id` 가 아니라 **설치본 고유 id(`local_id`, 8자리 hex)** 다 — 같은 이유다.
재등록으로 `device_id` 가 바뀌어도 스풀에 남아 있던 관측의 id 가 흔들리지 않아야 dedup 이 성립한다(`app_config.local_id`,
`%APPDATA%\YukTracker\config.json` 에 1회 생성). 그 값이 무엇이든 허브는 문자열로만 다룬다.

허브 쪽 기록: 기기 토큰 업로드가 200 이면 관측과 **같은 트랜잭션**에서 `devices.last_seen_ts`·`upload_count` 를 쓴다(커밋 1회,
저장 실패면 기기 기록도 없다). 인증은 됐지만 거부된 업로드(400·403 `device_mismatch`)도 `last_seen_ts` 는 남긴다 — 운영자가
잘못 설정된 exe 를 `devices.py list` 에서 찾을 수 있게(카운트는 성공만).

### 3-2. `GET /api/market/search?q=&item_id=&limit=&max_age_sec=` — 이름 검색 (미루봇 `!육의전 <아이템>`)

```jsonc
// ← 200  (q=봉인의 돌)
{"v":1,"server_time":1758500100.0,"q":"봉인의 돌","q_norm":"봉인의돌","item_id":null,
 "max_age_sec":259200.0,"limit":20,"expiry_days":2,"count":2,"total_matches":3,
 "listings":[
   {"listing_id":700001,"item_id":853,"item_name":"봉인의돌","quantity":10,"price":45000000,"seller":"판매자A",
    "category":"item","flag45":2,"flag46":0,"first_seen_ts":1758499000.0,"last_seen_ts":1758500000.1,
    "seen_count":3,"last_device":"DEV-1",
    "expires_from_ts":1758553200.0,"expires_by_ts":1758639600.0}, …]}   // 소멸 추정 경계(§4) — 2026-09-23 additive
```

| 인자 | 기본 | 규칙 |
|---|---|---|
| `q` | — | 정규화(§2) 뒤 부분 문자열 매칭. `q` 도 `item_id` 도 없으면 `400 {"error":"bad_request","field":"q"}` |
| `item_id` | — | 아이템 id 정확 일치(`q` 와 AND). 있으면 정확해야 한다 — 음수·비정수·2⁶³ 초과는 `400 {"error":"bad_request","field":"item_id"}`(조용히 깎거나 버리지 않는다) |
| `limit` | 20 | 1~100 클램프, 쓰레기는 기본값 |
| `max_age_sec` | 259200 | 관측 나이 안전망: `last_seen_ts ≥ server_time − max_age_sec` 인 행만. 2026-09-23 부터 기본 24h → **72h**(1차 필터는 아래 소멸 상한) |

`listings` 에 실리는 행 = **`expires_by_ts > server_time`**(소멸 상한이 아직 안 지남, §4) AND `max_age_sec` 조건. 숨긴 행까지 센 수가
`total_matches`. 응답 `expiry_days` 는 허브가 상한 계산에 쓴 일수(설정 `listing_expiry_days`, 현재 단기 2).

정렬: `price ASC, last_seen_ts DESC, listing_key`. 봇의 표시 형식(파일럿 계승 + 소멸 칸, 2026-09-23): `{item_name} | {quantity}개 |
{price:,}원 | {seller} | {N분 전} | {소멸}` — `N분 전` 은 `server_time − last_seen_ts`, `{소멸}` 은 `expires_from_ts == expires_by_ts` 면
`~M/D 00:00`(등록일 확정), 다르면 `~M/D 00:00 (M/D 부터 가능)`(하한·상한, KST). `count < total_matches` 면 "사라졌거나 오래된 N건 숨김" 한 줄.

### 3-3. `GET /api/market/listings?since_ts=&since_key=&limit=` — 증분 폴링 (알람 잡, 60s)

```jsonc
// ← 200
{"v":1,"server_time":…,"since_ts":1758500000.0,"since_key":"700001:853:판매자A","limit":500,"count":2,
 "next_since_ts":1758500055.2,"next_since_key":"700009:3506:판매자B",
 "listings":[ …§3-2 와 같은 행 모양(expires_from/by_ts 포함) + "listing_key"…, (last_seen_ts, listing_key) 오름차순 ]}
```

만료 필터는 **걸지 않는다**(증분은 전부 전달 — 소비자가 `expires_by_ts` 로 판단·정리한다).
**복합 keyset 커서** `(last_seen_ts, listing_key) > (since_ts, since_key)`, `(last_seen_ts, listing_key)` 오름차순, `limit`
1~2000(기본 500). 소비자는 응답의 `next_since_ts`·`next_since_key` 를 **둘 다** 저장했다가 다음 호출에 넣는다(마지막 행의
값; 행이 없으면 받은 값 그대로). 한 페이지의 행은 전부 같은 `seen_ts` 이고 관측기 시계가 앞서면 배치 전체가 같은
`recv_ts` 를 받으므로, `last_seen_ts` 하나만 엄격 초과하는 커서는 같은 시각의 행이 `limit` 를 넘길 때 나머지를 영원히
놓친다 — 키를 함께 비교하면 `limit` 가 작아도 빠짐없이 이어 읽는다. `since_key` 를 생략하면(빈 문자열) `since_ts` 와
같은 시각의 행이 전부 다시 온다 — 옛 소비자는 at-least-once(중복은 있어도 유실은 없다).

### 3-4. `GET /api/market/stats` — 운영 확인 (배포 게이트 G7)

```jsonc
{"v":1,"server_time":…,"observations":412,"listings":1380,"items":97,"item_names":95,
 "fresh_listings":210,"fresh_sec":259200.0,"live_listings":180,"expiry_days":2,"latest_recv_ts":1758500055.2,
                                          // live_listings = expires_by_ts > server_time 인 행(search 기본 필터와 같은 정의, §4);
                                          // fresh_listings 는 관측 나이(fresh_sec = search_max_age_sec) 기준 — 별개
 "devices":[{"device_id":"d-3f9a1c7e2b","label":"DESKTOP-ABC","alias":"소가","slot":"slot3","observations":300,"last_recv_ts":…},
            {"device_id":"DEV-2","label":null,"alias":null,"slot":null,"observations":112,"last_recv_ts":…}],
 "devices_registered":5,"devices_revoked":1,"devices_orphaned":0,"devices_max":7}   // label·alias·slot: 등록 기기만, 관리 시크릿 업로드는 null
                                          // 집계: 등록(미제거) / 제거 / 등록됐지만 설정 슬롯에 없는 기기(이름 변경·수동 삽입) / 정원 = 슬롯 수
                                          // 아직 안 들어온 사람 = devices_max − (devices_registered − devices_orphaned)
```

### 3-5. `GET /` — 무인증 상태 줄

`{"service":"yuktracker-hub","v":1,"server_time":…}` — 접속·healthcheck 확인용, 데이터 없음. 공개 요청도 200.

### 3-6. 기기 관리 — `hub/devices.py` (HTTP 아님, Pi 에서)

```bash
docker compose exec yuktracker-hub python devices.py list [--active]           # device_id slot alias label created last_seen uploads revoked note; --active = 미제거만
docker compose exec yuktracker-hub python devices.py revoke <device_id> --note "사유"   # 명단에서 제거(분실·남용 — 재설치는 재등록 교체가 알아서 한다)
docker compose exec yuktracker-hub python devices.py unrevoke <device_id>
docker compose exec yuktracker-hub python devices.py alias <device_id> "<별칭>"        # 빈 문자열이면 해제
docker compose exec yuktracker-hub python devices.py note <device_id> "메모"
```

서버와 같은 SQLite 파일(`HUB_DB_PATH`)에 직접 쓴다 — 서버는 요청마다 토큰을 조회하므로 제거는 다음 업로드부터 `403 device_revoked`.
HTTP 관리 라우트를 두지 않는 이유: 공개 표면을 늘리지 않고 셸에 시크릿이 필요 없다. 명단을 바꾸는 것은 관리자와 같은 슬롯의 재등록
교체(§3-0)뿐이다 — 시간이 지나 저절로 빠지는 기기는 없다(§1). 이미 제거된 기기를 다시 제거해도 원래 `revoked_ts` 는 보존(메모만 갱신), 등록 상태인 기기의 `unrevoke`
는 "이미 등록 상태" 안내. `alias` 는 관리자가 붙이는 별칭이고 `label` 은 기기가 스스로 적은 값이라 서로 덮지 않는다. 토큰 해시는
출력하지 않고, alias·label·note 의 제어 문자는 `?`, 전각 문자는 표시 폭 2 로 정렬한다. 없는 DB 경로는 만들지 않는다(exit 1).

### 3-7. `GET /api/market/ping` — 기기 토큰 확인 (관측기 `--selftest`)

```jsonc
// ← 200 (기기 토큰)                                  // ← 200 (관리 시크릿, 직접 접속)
{"ok":true,"v":1,"device_id":"d-3f9a1c7e2b",          {"ok":true,"v":1,"device_id":"admin",
 "label":"소가","server_time":1758500000.5}            "label":"","server_time":…}
```

관측기가 가진 자격은 기기 토큰 하나이고 조회 라우트(§3-2~§3-4)는 공개 리스너에서 403 `not_public` 이며 빈 배치 업로드는
400 이다 — 그래서 **"내 토큰이 아직 유효한가"를 관측을 만들어 보내기 전에 물을 수 있는 유일한 라우트**다(자가진단·현장 지원).

| 응답 | 조건 |
|---|---|
| `200` | 기기 토큰(공개·직접 모두) 또는 직접 접속의 관리 시크릿. `label` 은 관리자 별칭 > 기기가 적은 label > `""` |
| `401 {"error":"unauthorized"}` | 토큰 없음·무효, 공개 요청의 관리 시크릿 |
| `403 {"ok":false,"error":"device_revoked"}` | 기기가 명단에서 제거됨(§3-6) |
| `429 … + Retry-After` | 기기 당 `upload_limit_per_min` — **업로드와 같은 버킷**을 쓴다(자가진단이 업로드 몫을 쓴다는 뜻) |

- 판정은 전부 인증 미들웨어가 한다 — 핸들러는 **DB 를 건드리지 않는다**. `devices.last_seen_ts` 는 "마지막 업로드 시도" 의미를
  유지한다(ping 은 그 값을 움직이지 않는다).
- 관측기는 200 의 `device_id` 를 설정의 `hub_device_id` 와 대조한다 — 다르면 업로드가 403 `device_mismatch` 가 될 상태라
  재등록을 안내한다(§3-1).
- **옛 판 허브**(이 라우트 이전)는 직접 접속에서 404, 공개 리스너에서는 관리 라우트로 보여 403 `not_public` 이다. 관측기는 둘을
  같은 안내("허브가 옛 판입니다")로 묶는다.

## 4. 시각·신선도

- 허브 저장 시각은 `seen_ts = min(agent_ts, recv_ts)`: 스풀에 묵었다가 늦게 올라온 관측은 **관측 시각**이 남고(그때 본
  목록이므로), 관측기 시계가 앞서 있으면 서버 수신 시각으로 깎는다(미래 시각 금지). 목록의 `first/last_seen_ts` 는 이 값.
- **소멸 추정(2026-09-23)** — 게임 규칙(사용자 확인): 등록일 D(KST)에 올린 물품은 팔리지 않으면 **단기 = D+2 00:00, 장기 = D+3 00:00**
  에 목록에서 사라진다(9/23 20:03 등록 → 단기 9/25 00:00). 등록 시각은 패킷에 없고(PACKET-MARKET §1) 관측 시각만 있으므로 허브는
  관측 T 에 살아 있었다 → 등록일 ∈ {date(T)−1, date(T)} 로부터 두 경계를 **조회 때** 계산한다(저장 안 함, 스키마 무변경):
  - `expires_from_ts` = date_kst(`last_seen_ts`)+1 00:00 — 이 시각부터 사라졌을 **수** 있다(하한)
  - `expires_by_ts` = date_kst(`first_seen_ts`)+`listing_expiry_days` 00:00 — 이 시각 뒤는 **확실히** 없다(상한)

  첫 관측과 마지막 관측이 자정을 걸치면 둘이 같아져 등록일이 확정된다. `기간`(@45) 은 H-2609-11 검정 전(D)이라 **모든 행을 단기(2일)로
  본다**(사용자 결정 2026-09-23) — 검정 뒤 행별 일수(2=장기 → 3일)는 후속(additive). 등록 id 자정 앵커로 등록일을 확정하는 방법은 보류.
  KST 는 DST 가 없어 고정 오프셋 +9h(`db.kst_midnight`, tzdata 불필요). 팔려서 먼저 사라진 것은 알 수 없다(요청 c2s 가 암호화라 조회
  조건을 모른다) — `search` 는 상한이 지난 행만 숨기고 `last_seen_ts` 를 함께 준다. 응답이 총건수·조회조건을 싣게 되면 스냅샷 diff 로
  고도화(FOLLOWUP).
- 관측기 시계 보정은 하지 않는다(대시보드와 같은 원칙). 작은 오차(하루 이내 앞섬)는 `recv_ts` 로 깎고, 보존 기간보다 과거이거나
  `max_agent_ts_ahead_sec` 넘게 미래인 `agent_ts` 는 `400 agent_ts_out_of_range` 로 돌려보낸다 — 조용히 받으면 그 관측은
  어떤 조회에도 안 보이고 다음 prune 에 사라지는데 관측기는 200 을 받고 스풀을 지웠을 것이다. 큰 오차는
  `stats.devices[].last_recv_ts` 와 관측의 `agent_ts` 차이로도 드러난다.

## 5. 보존

일 1회 `prune`: `market_observations.recv_ts` · `market_listings.last_seen_ts` 가 `now − retention_market_days(30)` **미만**인 행
삭제(경계값 보존). `market_item_names` 는 지우지 않는다.

## 6. 설정 (`hub/config.json`, `.example` 참조)

`host` `port`(8800) `secret`(필수 — **16자 이상 문자열**, 예시값 `CHANGE-ME` 면 기동 거부: 기본 host `0.0.0.0` 이라 공개
시크릿으로 쓰기 API 가 LAN 에 열린다) `db_path`(상대 경로는 **config 파일 폴더 기준**; 환경변수 `HUB_DB_PATH` 가 있으면 그것이
이긴다 — 컨테이너는 Dockerfile 이 `/data/hub.db` 로 고정) `retention_market_days`(30) **`listing_expiry_days`**(2 — 소멸 상한 일수, 정수 ≥1,
§4; `기간` 검정 뒤 장기 3 은 행별로) `search_max_age_sec`(259200 — 2026-09-23 부터 72h)
`search_limit_default`(20) `search_limit_max`(100) `listings_limit_default`(500) `listings_limit_max`(2000)
`max_observations_per_request`(100) `max_rows_per_observation`(64) `max_agent_ts_ahead_sec`(86400).

공개·등록(2026-09-22, 슬롯 2026-09-23): `public_port`(8801 — 공개 리스너, `port` 와 달라야 기동) **`invite_codes`**(`{슬롯 이름: 초대
코드}` 객체; 없음·null·빈 객체 = 등록 닫힘. 슬롯 이름 = 인쇄 가능 문자 1~64자·공백 벗김·중복 금지, 초기 별칭이 된다. 코드 = 공백을
벗긴 뒤 **8자 이상**, 예시값(`CHANGE-ME…`)·`secret` 과 같은 값·**슬롯 간 중복**은 기동 거부 — 코드는 지인 한 사람에게 주는 반공개 값,
정원 = 슬롯 수) `admin_public`(false) `proxy_header`(`Tailscale-Funnel-Request`; Cloudflare Tunnel 로 바꾸면 `CF-Connecting-IP`)
`register_limit_per_hour`(10) `register_slot_limit_per_hour`(5) `upload_limit_per_min`(120) `auth_fail_limit_per_min`(30). 설정은 기동 때 읽는다 — 코드 교체·슬롯 추가는
`config.json` 수정 뒤 `docker compose restart`. 구 `invite_code`·`max_devices` 키가 있으면 이관 안내와 함께 기동 거부.

관측기 쪽 키(PR-Y1b, `%APPDATA%\YukTracker\config.json`): `hub_url`(공개 Funnel 주소; exe 내장 기본값) · `hub_device_id` · `hub_token`
(§3-0 응답 저장). 초대 코드는 `--invite-code` 인자/첫 실행 프롬프트로만 받고 저장하지 않는다. 구 `hub_secret` 키는 폐기 — 관리
시크릿은 exe 에 들어가지 않는다.

## 7. 변경 이력

- 2026-09-23 v1 **소멸 추정**(additive; 사용자 결정 3건 — 허브+봇 계약까지 반영 · 등록일 추정은 관측 시각 경계만 · `기간` 검정 전엔 단기 고정):
  §4 게임 규칙(단기 D+2 00:00 · 장기 D+3 00:00 KST)과 추정식, §3-2 행 `expires_from_ts`·`expires_by_ts` + 응답 `expiry_days` + 기본 필터
  `expires_by_ts > server_time`(24h 나이 필터는 72h 안전망으로 — `search_max_age_sec` 기본 86400 → 259200) + 봇 표시 소멸 칸, §3-3 행 필드
  (필터 없음), §3-4 `live_listings`·`expiry_days`, §6 `listing_expiry_days`. 스키마 무변경(조회 시점 계산).
- 2026-09-23 v1 **슬롯별 초대 코드**(사용자 결정 — 등록 즉시 누구인지 보이게, 재설치에 관리자 개입 없이): §6 `invite_code`·`max_devices`
  → `invite_codes = {슬롯: 코드}`(정원 = 슬롯 수, 코드 슬롯 간 중복 금지, 구 키는 기동 거부), §1 `devices.slot`(기존 DB 는 ADD COLUMN —
  무마이그레이션 규칙의 유일한 additive 예외) + 초기 `alias` = 슬롯 이름, §3-0 응답 `slot`·`replaced` + 같은 슬롯 재등록은 옛 기기를
  같은 트랜잭션에서 제거(`registration_full` 폐지), §3-4 `devices[].slot`·`devices_max` = 슬롯 수, §3-6 표에 `slot` 열.
  `/code-review 15 high` 10건 반영: 슬롯당 등록 상한 `register_slot_limit_per_hour`(정원 폐지의 보완), 교체 때 별칭 승계·메모 덧붙임,
  `_tx` 는 `BEGIN IMMEDIATE`(조회부터 같은 트랜잭션), ADD COLUMN 은 검사 없이 시도 + 중복만 삼킴(동시 첫 열기 경쟁 제거), 슬롯 이름
  변경의 고아 기기 = 기동 경고 + `stats.devices_orphaned`, `devices.py list --active`, 슬롯 이름·label 검사 한 함수(`printable_name`),
  예시값 규칙 하나(`CHANGE-ME` 접두), '정원 자리' 문구 정리.
- 2026-09-22 v1 — 최초. SEAssist `dashboard/` 확장(구 PR-Y3 계획) 대신 이 레포의 독립 서버로(사용자 결정: SEAssist 레포 머지 중단).
- 2026-09-22 v1 리뷰 반영(PR #6, additive): §3-3 복합 keyset 커서(`since_key`·`next_since_key`·행 `listing_key`), §3-1 i64 범위·
  문자열 상한·`agent_ts_out_of_range`·`500 storage_error`·payload 행은 미지 키만, §3-2 `item_id` 400, §1 이름 upsert 규칙,
  §6 secret 규칙·`db_path` 기준·`HUB_DB_PATH`·`max_agent_ts_ahead_sec`, DB `synchronous=NORMAL`.
- 2026-09-22 v1 리뷰 반영(PR #9 리뷰): §3-1 `device_id` 상한 128 → **101**(파생 `obs_id` 가 상한 128 을 넘어 그 기기의
  배치가 영구 격리되던 것 — `hub/server.py` 도 같이 조임), §1 기기명·허브 주소를 실데이터로 명시, 예시 기기명 `DEV-1`/`DEV-2` 로 통일.
- 2026-09-22 v1 공개 업로드(additive, 사용자 결정: 관측기는 Npcap 만 있는 일반 사용자 PC 에서 돈다 → VPN 전제 폐기): §0 전송 전제
  개정 — Tailscale Funnel 공개 + 자격 2종(관리 시크릿은 직접 접속 전용·exe 금지, 기기 토큰) + `Tailscale-Funnel-Request` 공개 판정 +
  속도제한 429; §1 `devices`; §3-0 register; §3-1 `device_id` 규칙·401/403/429·관측기 규약; §3-4 `label`·`devices_registered/revoked`;
  §3-6 `devices.py`; §6 새 설정 키·관측기 키(`hub_secret` 폐기). `/code-review 10 high` 13건 반영: 공개 리스너 `public_port` 분리
  (헤더 부재 = 직접 접속이라는 fail-open 제거, 헤더는 2차 방어), 인증 실패 집계는 토큰 검사 뒤(공유 NAT), XFF 없는 공개 요청은
  `public:?`, 정원 = 활성 정의(`device_idle_days` + 미업로드 하루)·`revoke --stale`, 기기 기록은 관측과 같은 트랜잭션 + 거부도 last_seen,
  등록 본문 4KiB(411/413), 429 가 취소 검사보다 먼저, 초대 코드 strip, CLI 중복 취소 보존·전각 폭, README 게이트는 더미 Bearer.
- 2026-09-22 v1 기기 명단은 관리자가 관리(사용자 결정): 배포 대상은 **아는 사람 최대 7명**이라 ① `max_devices` 기본 500 → **7**,
  ② **활성 여부에 따른 자동 제외 폐기** — `device_idle_days`·미업로드 유예·`devices.py revoke --stale` 을 없애고 "등록"의 정의를
  `revoked_ts IS NULL` 하나로(§1). 한 번도 안 올린 기기도 자리를 차지하고 자리를 비우는 것은 `revoke` 뿐이다. ③ 관리자 별칭
  `devices` 표 `alias` + `devices.py alias <device_id> "<별칭>"`(§3-6) — 기기가 스스로 적은 `label` 과 별개, 표시·로그는 별칭 우선.
  §3-4 는 `devices_active` 대신 `devices_max`(설정된 정원)를 싣고 `devices[].alias` 가 는다.
- 2026-09-22 v1 고정 기기 전제의 최소 방어(사용자 결정 — 배포 대상이 아는 사람 고정 기기라 XFF 우회 방어·자가 치유는 하지
  않는다): ① compose 가 공개 리스너를 `127.0.0.1:8801:8801` 로 루프백에만 낸다(Funnel 은 같은 호스트에서 프록시하므로
  그대로 동작, LAN 직접 접속 경로만 사라진다). ② `request.json()` 의 `except` 에 `LookupError` 추가 — `Content-Type` 의
  charset 이 모르는 인코딩이면 무인증 register 가 500 + 트레이스백을 쌓던 것을 400 으로. ③ 모든 쓰기를 `Database._tx`
  (commit/rollback 보장)로 통과 — `prune` 의 두 DELETE 와 `touch_device` 가 잠금으로 실패해도 암묵 트랜잭션이 남지
  않는다(남으면 그 연결이 낡은 WAL 스냅샷을 붙들어 `devices.py` 의 제거가 안 먹고 다른 프로세스 쓰기가 막힌다).
- 2026-09-22 v1 additive(PR-Y5, 배포 준비): **§3-7 `GET /api/market/ping`** — 기기 토큰 확인 전용 라우트. 관측기의 자격은 기기
  토큰 하나이고 조회 라우트는 공개 리스너에서 403, 빈 배치 업로드는 400 이라 "토큰이 아직 유효한가"를 물을 길이 없었다
  (관측기 `--selftest` 가 쓴다). 스키마·기존 라우트 무변경, 인증·속도제한은 업로드와 같은 규칙을 그대로 따르고 핸들러는 DB 를
  건드리지 않는다. §0 라우트 표에 한 줄 추가.
