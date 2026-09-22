# 육의전 시세 허브 (yuktracker-hub)

관측기(YukTracker)가 올린 육의전 목록을 모아 두고 미루봇-IRIS 가 검색하는 **독립 서버**. 설계는
[docs/PLAN.md](../docs/PLAN.md) §PR-Y3, 계약(엔드포인트·필드·정규화·신선도)의 정본은
[docs/HUB-PROTOCOL.md](../docs/HUB-PROTOCOL.md).

- 의존성: `aiohttp` 하나 + stdlib sqlite3. **`src/` import 금지** — 관측기 exe 와 별개 배포 단위(빌드 컨텍스트 = 이 폴더).
  관측기의 "런타임 의존성 0" 규칙은 `src/yuktracker/` 에만 적용된다.
- 라우팅: `GET /`(무인증 상태 줄) · `POST /api/market/observations` · `GET /api/market/search` ·
  `GET /api/market/listings` · `GET /api/market/stats` — `/api/*` 는 `Authorization: Bearer <secret>`.
- 보존: 관측·목록 30일(`retention_market_days`), 일 1회 자동 프루닝. 학습 표(아이템 id→이름)는 유지.
- SEAssist 대시보드(Pi:8799)와 무관 — 별도 컨테이너, 포트 **8800**(2026-09-22 결정: SEAssist 레포엔 더 이상 머지하지 않는다).

## 로컬 개발 (Windows)

```powershell
copy config.json.example config.json        # secret 필수(16자 이상, CHANGE-ME 면 기동 거부). 상대 db_path 는 이 폴더(config 위치) 기준 —
                                            # 레포가 OneDrive 안이면 db_path 를 OneDrive 밖 절대 경로로(WAL 과 동기화 충돌)
pip install -r requirements.txt
python server.py --config config.json
python -X utf8 -m pytest tests -q            # repo 루트에서는: python -X utf8 -m pytest hub/tests -q
```

동작 확인(합성 데이터 — 실제 판매자명은 어디에도 적지 않는다):

```bash
curl -s http://127.0.0.1:8800/
curl -s -X POST http://127.0.0.1:8800/api/market/observations \
  -H "Authorization: Bearer <secret>" -H "Content-Type: application/json" \
  -d '{"v":1,"device_id":"DEV-1","observations":[{"obs_id":"DEV-1:1758500000000:0123456789ab","agent_ts":1758500000.0,"opcode":12831,"page":null,"total_pages":1,"hdr4":0,"anomalies":[],"rows":[{"listing_id":700001,"item_id":853,"item_name":"봉인의돌","quantity":10,"quantity_hi":0,"price":45000000,"price_hi":0,"seller":"판매자A","unknown40":"00000000","flag45":2,"flag46":0}]}]}'
curl -s "http://127.0.0.1:8800/api/market/search?q=%EB%B4%89%EC%9D%B8%EC%9D%98%20%EB%8F%8C" -H "Authorization: Bearer <secret>"   # q=봉인의 돌
curl -s "http://127.0.0.1:8800/api/market/listings?since_ts=0" -H "Authorization: Bearer <secret>"
curl -s "http://127.0.0.1:8800/api/market/stats" -H "Authorization: Bearer <secret>"
```

## 파이 배포 (Docker)

전제: SEAssist 대시보드와 같은 Pi(Docker + compose 플러그인, Tailscale). 데이터 볼륨은 대시보드와 **별도 폴더**
(예 `/mnt/dashdata/yuktracker-hub`).

```bash
# 1) 이 폴더를 파이로 복사 (Windows 에서, Git Bash)
tar -C <repo루트> -czf - --exclude=hub/data --exclude=hub/config.json hub \
  | ssh jby@192.168.0.14 'mkdir -p ~/yuktracker-hub && tar xzf - -C ~/yuktracker-hub --strip-components=1'

# 2) 파이에서 1회 설정
cd ~/yuktracker-hub
cp config.json.example config.json      # secret 설정(16자 이상). db_path 는 손대지 않아도 된다 — 컨테이너는 HUB_DB_PATH=/data/hub.db
echo 'HUB_DATA_DIR=/mnt/dashdata/yuktracker-hub' > .env

# 3) 기동/업데이트 (재복사 후 동일 명령)
docker compose up -d --build

# 4) 확인
docker compose logs -f --tail 20
curl -s http://127.0.0.1:8800/
curl -s -H "Authorization: Bearer <시크릿>" http://127.0.0.1:8800/api/market/stats
```

- DB 위치는 Dockerfile 의 `ENV HUB_DB_PATH=/data/hub.db` 가 config 의 `db_path` 보다 우선한다 — compose 가 `/data` 를 볼륨에
  매핑하므로 운영자가 config 를 안 고쳐도 `compose up --build` 에 데이터가 사라지지 않는다. 기동 로그 첫 줄에 `db_path=` 가 찍힌다.
- 증분 폴링 소비자(미루봇)는 `next_since_ts` 와 `next_since_key` 를 둘 다 저장해 다음 호출에 넣는다(HUB-PROTOCOL §3-3).
- 접속 주소: tailnet `http://100.123.248.88:8800`(관측기 PC) / LAN `http://192.168.0.14:8800`(집 Wi-Fi).
  미루봇(같은 Pi)은 `http://127.0.0.1:8800`. **포트포워딩 금지** — 평문 HTTP + 공유 시크릿 전제.
- 시크릿: `config.json` 의 `secret` 하나를 관측기(`hub_secret`, PR-Y1b)·봇이 공유. 레포엔 `.example` 만 커밋.
- 판매자명 등 실데이터가 DB 에 쌓인다 — 볼륨은 Pi 로컬(OneDrive 밖), 덤프·DB 를 레포에 넣지 않는다.

## 구조

```
server.py   aiohttp 앱 — Bearer 미들웨어 · 업로드 전건 검증 · 4 라우트 · 일 1회 retention
db.py       SQLite — market_observations / market_listings / market_item_names · 정규화 한 정의 · upsert · prune
tests/      pytest — 인증 · 검증(전건 거부) · dedup · upsert · 이름 학습 · 정규화 · 신선도 · 증분 목록 · 통계 · prune
```
