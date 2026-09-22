# 육의전 시세 허브 — 프로토콜 (HUB-PROTOCOL v1, 2026-09-22)

관측기(YukTracker, PR-Y1b) → 허브(`hub/`, Pi:8800) → 미루봇-IRIS(PR-Y4) 사이의 계약. 구현은 `hub/server.py`·`hub/db.py`,
테스트 `hub/tests/`. 이 문서가 정본이고 코드가 어긋나면 코드가 틀린 것이다.

## 0. 공통 규칙

- 전송: HTTP/1.1 평문 + JSON(UTF-8). tailnet/LAN 안에서만(포트포워딩 금지). 인증은 **공유 시크릿 1개** —
  `/api/*` 전부 `Authorization: Bearer <secret>`, 불일치·누락은 `401 {"error":"unauthorized"}`. `GET /` 만 무인증.
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

## 2. 정규화 한 정의

`norm(name) = "".join(name.split()).casefold()` — **공백 전부 제거 + casefold**. 허브가 저장(`item_name_norm`)과
검색(`q`) 양쪽에 같은 함수를 쓰므로 소비자는 원문 그대로 보내도 된다(봇 `_squash` 의 공백 제거는 이 규칙의 부분집합).
표시명의 선두 `[M]` 제거는 관측기(`item_names.py`) 몫 — 허브는 받은 이름을 그대로 저장한다. 매칭은 정규화 키의
**부분 문자열**(`instr`), 예: `봉인의 돌` → `봉인의돌` 은 `봉인의돌`·`봉인의돌(상)` 에 맞는다.

## 3. REST

### 3-1. `POST /api/market/observations` — 관측기 업로드 (at-least-once, 배치)

```jsonc
// → 요청 (관측 1~100건, 관측당 행 0~64)
{"v":1, "device_id":"DEV-1",
 "observations":[
   {"obs_id":"DEV-1:1758500000123:0123456789ab",       // "{device}:{agent_ts_ms}:{sha1(body)[:12]}" — 관측기가 만든다
    "agent_ts":1758500000.123, "opcode":12831,         // 0x321f
    "page":null, "total_pages":12, "hdr4":0,           // page: 응답엔 페이지 번호가 없다(null 허용)
    "anomalies":[],                                     // 파서 anomaly 이름들 (있으면 그대로)
    "item_table":{"gcs_sha256":"…","rows":4001,"archive_ts":"2026-09-21T02:04:11Z"},  // 이름 해석에 쓴 표 (null 허용)
    "rows":[                                            // SEAssist packet_market.row_to_dict 키 + item_name
      {"listing_id":700001,"item_id":853,"item_name":"봉인의돌","quantity":10,"quantity_hi":0,
       "price":45000000,"price_hi":0,"seller":"판매자A","unknown40":"00000000","flag45":2,"flag46":0}]}]}
// ← 200
{"ok":true,"accepted":1,"duplicates":0,"rows":1,"server_time":1758500001.5}
```

| 필드 | 형 | 규칙 |
|---|---|---|
| `device_id` | str 1~101 | 관측기 기기명(SEAssist `display_name` 과 같은 값). 요청 단위. **상한이 `obs_id` 보다 27 작다** — `obs_id` 가 `{device}` 에 `:{13자리 ms}:{12자리 해시}` 를 붙이기 때문. 긴 이름은 관측기가 101자로 자른다 |
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
| `401` | Bearer 불일치 |
| `413` | 본문 4MiB 초과(aiohttp) — 관측기는 배치를 나눈다 |
| `500 {"ok":false,"error":"storage_error","server_time"}` | 검증 통과 뒤 DB 오류(디스크·잠금) — 관측기는 5xx 로 재시도 |

관측기 규약(PR-Y1b): 스풀 파일을 성공 응답(200) 뒤에만 지운다. 네트워크 실패·5xx 는 지수 백오프 재시도, 400 은 그 배치를
격리 폴더로 옮기고 로그를 남긴다(재시도해도 같은 400). 같은 `obs_id` 가 두 번 가는 것은 정상(허브가 dedup).

### 3-2. `GET /api/market/search?q=&item_id=&limit=&max_age_sec=` — 이름 검색 (미루봇 `!육의전 <아이템>`)

```jsonc
// ← 200  (q=봉인의 돌)
{"v":1,"server_time":1758500100.0,"q":"봉인의 돌","q_norm":"봉인의돌","item_id":null,
 "max_age_sec":86400.0,"limit":20,"count":2,"total_matches":3,
 "listings":[
   {"listing_id":700001,"item_id":853,"item_name":"봉인의돌","quantity":10,"price":45000000,"seller":"판매자A",
    "category":"item","flag45":2,"flag46":0,"first_seen_ts":1758499000.0,"last_seen_ts":1758500000.1,
    "seen_count":3,"last_device":"DEV-1"}, …]}
```

| 인자 | 기본 | 규칙 |
|---|---|---|
| `q` | — | 정규화(§2) 뒤 부분 문자열 매칭. `q` 도 `item_id` 도 없으면 `400 {"error":"bad_request","field":"q"}` |
| `item_id` | — | 아이템 id 정확 일치(`q` 와 AND). 있으면 정확해야 한다 — 음수·비정수·2⁶³ 초과는 `400 {"error":"bad_request","field":"item_id"}`(조용히 깎거나 버리지 않는다) |
| `limit` | 20 | 1~100 클램프, 쓰레기는 기본값 |
| `max_age_sec` | 86400 | `last_seen_ts ≥ server_time − max_age_sec` 인 행만 `listings` 에. 숨긴 행까지 센 수가 `total_matches` |

정렬: `price ASC, last_seen_ts DESC, listing_key`. 봇의 표시 형식(파일럿 계승): `{item_name} | {quantity}개 | {price:,}원 |
{seller} | {N분 전}` — `N분 전` 은 `server_time − last_seen_ts`. `count < total_matches` 면 "오래된 N건 숨김" 한 줄.

### 3-3. `GET /api/market/listings?since_ts=&since_key=&limit=` — 증분 폴링 (알람 잡, 60s)

```jsonc
// ← 200
{"v":1,"server_time":…,"since_ts":1758500000.0,"since_key":"700001:853:판매자A","limit":500,"count":2,
 "next_since_ts":1758500055.2,"next_since_key":"700009:3506:판매자B",
 "listings":[ …§3-2 와 같은 행 모양 + "listing_key"…, (last_seen_ts, listing_key) 오름차순 ]}
```

**복합 keyset 커서** `(last_seen_ts, listing_key) > (since_ts, since_key)`, `(last_seen_ts, listing_key)` 오름차순, `limit`
1~2000(기본 500). 소비자는 응답의 `next_since_ts`·`next_since_key` 를 **둘 다** 저장했다가 다음 호출에 넣는다(마지막 행의
값; 행이 없으면 받은 값 그대로). 한 페이지의 행은 전부 같은 `seen_ts` 이고 관측기 시계가 앞서면 배치 전체가 같은
`recv_ts` 를 받으므로, `last_seen_ts` 하나만 엄격 초과하는 커서는 같은 시각의 행이 `limit` 를 넘길 때 나머지를 영원히
놓친다 — 키를 함께 비교하면 `limit` 가 작아도 빠짐없이 이어 읽는다. `since_key` 를 생략하면(빈 문자열) `since_ts` 와
같은 시각의 행이 전부 다시 온다 — 옛 소비자는 at-least-once(중복은 있어도 유실은 없다).

### 3-4. `GET /api/market/stats` — 운영 확인 (배포 게이트 G7)

```jsonc
{"v":1,"server_time":…,"observations":412,"listings":1380,"items":97,"item_names":95,
 "fresh_listings":210,"fresh_sec":86400.0,"latest_recv_ts":1758500055.2,
 "devices":[{"device_id":"DEV-1","observations":300,"last_recv_ts":…},{"device_id":"DEV-2","observations":112,"last_recv_ts":…}]}
```

### 3-5. `GET /` — 무인증 상태 줄

`{"service":"yuktracker-hub","v":1,"server_time":…}` — 접속·healthcheck 확인용, 데이터 없음.

## 4. 시각·신선도

- 허브 저장 시각은 `seen_ts = min(agent_ts, recv_ts)`: 스풀에 묵었다가 늦게 올라온 관측은 **관측 시각**이 남고(그때 본
  목록이므로), 관측기 시계가 앞서 있으면 서버 수신 시각으로 깎는다(미래 시각 금지). 목록의 `first/last_seen_ts` 는 이 값.
- "사라짐" 판정은 없다(요청 c2s 가 암호화라 조회 조건을 모른다). 소비자는 `max_age_sec`(기본 24h)로 오래된 행을 숨기고
  `last_seen_ts` 를 함께 보여준다. 응답이 총건수·조회조건을 싣게 되면 스냅샷 diff 로 고도화(FOLLOWUP).
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
이긴다 — 컨테이너는 Dockerfile 이 `/data/hub.db` 로 고정) `retention_market_days`(30) `search_max_age_sec`(86400)
`search_limit_default`(20) `search_limit_max`(100) `listings_limit_default`(500) `listings_limit_max`(2000)
`max_observations_per_request`(100) `max_rows_per_observation`(64) `max_agent_ts_ahead_sec`(86400). 관측기 쪽 키(PR-Y1b):
`hub_url`·`hub_secret`(`%APPDATA%\YukTracker\config.json`).

## 7. 변경 이력

- 2026-09-22 v1 — 최초. SEAssist `dashboard/` 확장(구 PR-Y3 계획) 대신 이 레포의 독립 서버로(사용자 결정: SEAssist 레포 머지 중단).
- 2026-09-22 v1 리뷰 반영(PR #6, additive): §3-3 복합 keyset 커서(`since_key`·`next_since_key`·행 `listing_key`), §3-1 i64 범위·
  문자열 상한·`agent_ts_out_of_range`·`500 storage_error`·payload 행은 미지 키만, §3-2 `item_id` 400, §1 이름 upsert 규칙,
  §6 secret 규칙·`db_path` 기준·`HUB_DB_PATH`·`max_agent_ts_ahead_sec`, DB `synchronous=NORMAL`.
- 2026-09-22 v1 리뷰 반영(PR #9 리뷰): §3-1 `device_id` 상한 128 → **101**(파생 `obs_id` 가 상한 128 을 넘어 그 기기의
  배치가 영구 격리되던 것 — `hub/server.py` 도 같이 조임), §1 기기명·허브 주소를 실데이터로 명시, 예시 기기명 `DEV-1`/`DEV-2` 로 통일.
