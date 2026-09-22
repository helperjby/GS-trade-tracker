# 육의전 시세 허브 (yuktracker-hub)

관측기(YukTracker)가 올린 육의전 목록을 모아 두고 미루봇-IRIS 가 검색하는 **독립 서버**. 설계는
[docs/PLAN.md](../docs/PLAN.md) §PR-Y3, 계약(엔드포인트·필드·정규화·신선도)의 정본은
[docs/HUB-PROTOCOL.md](../docs/HUB-PROTOCOL.md).

- 의존성: `aiohttp` 하나 + stdlib sqlite3. **`src/` import 금지** — 관측기 exe 와 별개 배포 단위(빌드 컨텍스트 = 이 폴더).
  관측기의 "런타임 의존성 0" 규칙은 `src/yuktracker/` 에만 적용된다.
- 라우팅: `GET /`(무인증 상태 줄) · `POST /api/market/register`(초대 코드 → 기기 토큰, 무 Bearer) · `POST /api/market/observations`
  (기기 토큰; 직접 접속이면 관리 시크릿도) · `GET /api/market/search` · `GET /api/market/listings` · `GET /api/market/stats`(관리 시크릿,
  **직접 접속 전용** — Funnel 경유는 403 `not_public`). 규칙은 HUB-PROTOCOL §0.
- 공개: 리스너 둘 — 관리 **8800**(봇·제작자 직접 접속, Funnel 금지) / 공개 **8801**(Pi 의 **Tailscale Funnel** 이
  `https://<pi-node>.<tailnet>.ts.net/` 로 낸다, 아래 "공개 노출"). 일반 사용자 PC 에는 Npcap + 관측기 exe 만 있고 VPN 이 없기 때문.
  공개 리스너로 온 요청은 헤더와 무관하게 전부 공개 취급(관리 시크릿 무시·조회 403). 관리 시크릿은 exe 에 넣지 않는다.
- 보존: 관측·목록 30일(`retention_market_days`), 일 1회 자동 프루닝. 학습 표(아이템 id→이름)는 유지.
- SEAssist 대시보드(Pi:8799)와 무관 — 별도 컨테이너, 포트 **8800**(2026-09-22 결정: SEAssist 레포엔 더 이상 머지하지 않는다).

## 로컬 개발 (Windows)

```powershell
copy config.json.example config.json        # secret(16자↑)·invite_code(8자↑, 비우면 등록 닫힘) — 예시값이면 기동 거부. 상대 db_path 는 이 폴더(config 위치) 기준 —
                                            # 레포가 OneDrive 안이면 db_path 를 OneDrive 밖 절대 경로로(WAL 과 동기화 충돌)
pip install -r requirements.txt
python server.py --config config.json
python -X utf8 -m pytest tests -q            # repo 루트에서는: python -X utf8 -m pytest hub/tests -q
```

동작 확인(합성 데이터 — 실제 판매자명은 어디에도 적지 않는다):

```bash
curl -s http://127.0.0.1:8800/
curl -s -X POST http://127.0.0.1:8800/api/market/register -H "Content-Type: application/json" \
  -d '{"v":1,"invite_code":"<invite_code>","label":"DEV-1"}'          # → device_id·token — 기기 토큰 업로드는 device_id 를 이 값으로
curl -s -X POST http://127.0.0.1:8800/api/market/observations \
  -H "Authorization: Bearer <secret>" -H "Content-Type: application/json" \
  -d '{"v":1,"device_id":"DEV-1","observations":[{"obs_id":"DEV-1:1758500000000:0123456789ab","agent_ts":1758500000.0,"opcode":12831,"page":null,"total_pages":1,"hdr4":0,"anomalies":[],"rows":[{"listing_id":700001,"item_id":853,"item_name":"봉인의돌","quantity":10,"quantity_hi":0,"price":45000000,"price_hi":0,"seller":"판매자A","unknown40":"00000000","flag45":2,"flag46":0}]}]}'
curl -s "http://127.0.0.1:8800/api/market/search?q=%EB%B4%89%EC%9D%B8%EC%9D%98%20%EB%8F%8C" -H "Authorization: Bearer <secret>"   # q=봉인의 돌
curl -s "http://127.0.0.1:8800/api/market/listings?since_ts=0" -H "Authorization: Bearer <secret>"
curl -s "http://127.0.0.1:8800/api/market/stats" -H "Authorization: Bearer <secret>"
```

## 파이 배포 (Docker)

전제: SEAssist 대시보드와 같은 Pi(Docker + compose 플러그인, Tailscale). 데이터 볼륨은 대시보드(`~/dashboard/data`)와 **별도 폴더** —
기본은 compose 파일 옆 `./data`(실배포 2026-09-22: `~/yuktracker-hub/data`). 이 Pi 에 `/mnt/dashdata` 는 없다(루트가 SSD 로 이전됨).

```bash
# 1) 이 폴더를 파이로 복사 (Windows 에서, Git Bash)
tar -C <repo루트> -czf - --exclude=hub/data --exclude=hub/config.json hub \
  | ssh <user>@<pi> 'mkdir -p ~/yuktracker-hub && tar xzf - -C ~/yuktracker-hub --strip-components=1'

# 2) 파이에서 1회 설정
cd ~/yuktracker-hub
S=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))'); I=$(python3 -c 'import secrets;print(secrets.token_urlsafe(9))')
sed -e "s/CHANGE-ME-INVITE/$I/" -e "s/CHANGE-ME/$S/" config.json.example > config.json; chmod 600 config.json
                                        # secret 32자(관리 — 봇·제작자만) · invite_code 12자(커뮤니티 공유; 비우면 등록 닫힘). INVITE 치환이 먼저여야 한다.
                                        # db_path 는 손대지 않아도 된다 — 컨테이너는 HUB_DB_PATH=/data/hub.db
# (선택) 데이터를 다른 폴더에 두려면: echo 'HUB_DATA_DIR=/절대/경로' > .env   — 기본은 ./data

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
- 공개 리스너 8801 은 compose 가 **`127.0.0.1:8801:8801`** 로 루프백에만 낸다 — tailscaled 가 같은 호스트에서
  프록시하므로 Funnel 은 그대로 되고, LAN·게스트 와이파이에서 Funnel 을 건너뛰고 평문으로 찌르는 경로는 막힌다.
  관리 리스너 8800 은 봇(같은 Pi)과 제작자(Tailscale VPN)가 직접 붙어야 해서 전 인터페이스에 남긴다.
- 접속 주소: 일반 사용자 관측기 = 공개 `https://<pi-node>.<tailnet>.ts.net`(Funnel, 아래) / 제작자 PC = tailnet `http://<pi-tailnet-ip>:8800`
  / LAN `http://<pi-lan-ip>:8800` / 미루봇(같은 Pi) `http://127.0.0.1:8800`. 직접 접속은 평문 HTTP — **공유기 포트포워딩은 여전히 금지**
  (TLS 는 Funnel 이 맡고, 그 밖의 경로는 신뢰망 안이라는 전제).
- 자격: `secret`(관리 — 봇·제작자 조회, 직접 접속 전용, exe 에 넣지 않는다) / `invite_code`(커뮤니티 공유 — 기기 등록) / 기기 토큰
  (등록 응답, 관측기 `hub_token`). 레포엔 `.example` 만 커밋.
- 판매자명 등 실데이터가 DB 에 쌓인다 — 볼륨은 Pi 로컬(OneDrive 밖), 덤프·DB 를 레포에 넣지 않는다.

## 공개 노출 (Tailscale Funnel)

Pi 쪽에서만 한다 — 사용자 PC 는 그대로. 전제: Tailscale ≥1.38.3, 관리 콘솔 DNS 에서 **MagicDNS + HTTPS 인증서** 켬, tailnet
정책(ACL)에 `funnel` 노드 속성(첫 `tailscale funnel` 실행이 안내 URL 을 찍는다). 공개 포트는 443/8443/10000 뿐이고 Funnel 트래픽은
대역폭 제한이 있다(수치 미공개 — JSON 배치엔 충분).

```bash
sudo tailscale funnel --bg 8801                  # https://<pi-node>.<tailnet>.ts.net/ → 127.0.0.1:8801(공개 리스너), 재부팅 뒤에도 유지(--bg)
tailscale funnel status                          # 설치판 구문은 `tailscale funnel --help` 로 확인. 8800 에는 걸지 않는다
# 끄기: sudo tailscale funnel --https=443 off   (또는 sudo tailscale funnel reset)
```

외부망(폰 LTE) 게이트 — 아래가 전부 맞아야 공개 상태가 설계대로다(진짜 시크릿은 공개망에 보내지 않는다 — 더미로 충분):

```bash
H=https://<pi-node>.<tailnet>.ts.net
curl -s $H/                                                                   # 200 상태 줄 (첫 요청은 인증서 발급으로 수 초)
curl -s -H "Authorization: Bearer x" $H/api/market/stats                      # 403 {"ok":false,"error":"not_public"} — 공개 리스너는 자격을 보지도 않는다
curl -s -X POST $H/api/market/register -H "Content-Type: application/json" \
  -d '{"v":1,"invite_code":"<invite_code>","label":"GATE"}'                   # 200 device_id·token
curl -s -X POST $H/api/market/register -H "Content-Type: application/json" \
  -d '{"v":1,"invite_code":"nope"}'                                           # 401 bad_invite
docker compose exec yuktracker-hub python devices.py list                     # GATE 가 보인다 → revoke <device_id> --note gate
docker compose exec yuktracker-hub python devices.py alias <device_id> "친구1"  # 관리자 별칭(목록·로그에 이 이름이 먼저)
```

운영 메모:
- **명단은 관리자가 관리한다**(2026-09-22 결정): 배포 대상은 아는 사람 **최대 7명**이라 `max_devices` 기본이 **7** 이고,
  정원이 세는 것은 **미제거 기기 전부**다 — 업로드를 한 번도 안 한 기기도 자리를 차지하며 시간이 지나 저절로 빠지는 일은 없다.
  새 사람에게 자리를 내주려면 `devices.py revoke <device_id>` 로 하나를 뺀다(제거는 **soft** — 행은 감사용으로 남고 같은 초대
  코드로 재등록하면 새 기기가 된다). 남용은 `invite_code` 교체 → `docker compose restart`(기존 토큰 유지, 신규 등록만 막힘).
- 별칭: `devices.py alias <device_id> "소가"` — 등록 때 기기가 스스로 적은 `label` 과 별개로 관리자가 붙이는 이름이고,
  목록·서버 로그는 별칭을 앞세운다. 빈 문자열이면 해제. 자리 현황은 `stats` 의 `devices_registered`/`devices_max`.
  지인 1명 = 별칭 1개로 두면 `devices.py list` 와 `stats.devices[]` 가 그대로 접속 현황판이 된다.
- **재설치 주의**: `device_id` 는 허브가 등록 때마다 새로 발급한다(호스트명·하드웨어에서 뽑지 않는다). 그래서 지인이
  프로그램을 지웠다 다시 깔고 등록하면 **거부(409)도, 기존 행 갱신도 아니고 새 행이 하나 더 생긴다** — 옛 행이 자리를
  계속 차지한다. 재설치 당시엔 아무 오류도 안 나고, 나중에 다른 사람이 등록할 때 `registration_full` 로 터진다.
  → 지인들에게 "재설치할 일 있으면 말해줘"라고 안내하고, 관리자는 `devices.py list` 에서 같은 별칭 두 줄을 보면
  옛 `device_id` 를 `revoke` 한다. (여유 슬롯 = `max_devices` − 사람 수 만큼은 말없이 흡수된다.)
- 관리 시크릿은 Funnel 을 타지 않는다(`admin_public` false) — 봇은 127.0.0.1:8800, 제작자는 tailnet/LAN 의 8800 직접. 공개 리스너는
  자격을 보지 않고 403 을 주며, 8800 에 실수로 Funnel 을 걸어도 `Tailscale-Funnel-Request` 헤더로 한 번 더 막는다(2차 방어).
- 속도제한: 등록 IP 10/h, 업로드 기기 120/min(취소된 기기의 폭주도), 인증 실패 공개 IP 30/min — 토큰 검사 뒤 실패에만(공유 NAT 의
  다른 정상 기기는 안 막힘). 429 + `Retry-After`. 직접 접속은 소켓 IP 가 전부 docker 브리지라 인증 실패를 세지 않는다.
- 공개 URL 은 관측기 exe 에 내장된다(PR-Y1b) — 문서·레포엔 자리표시자만. Cloudflare Tunnel 로 바꾸면 `proxy_header` 를
  `CF-Connecting-IP` 로.

## 구조

```
server.py   aiohttp — 리스너 둘(관리 8800 / 공개 8801, DB·속도제한 공유) · 공개/직접 구분 · Bearer(관리 시크릿/기기 토큰) 미들웨어 · RateLimiter · 업로드 전건 검증 · register · 5 라우트 · 일 1회 retention
db.py       SQLite — market_observations / market_listings / market_item_names / devices(등록 정의 하나 = 미제거) · 정규화 한 정의 · upsert · prune
devices.py  관리 CLI — 기기 list / revoke / unrevoke / alias / note (서버와 같은 DB 파일, 시크릿 불필요)
tests/      pytest — 인증·공개/직접 · 등록·기기 토큰·취소 · 속도제한 · 검증(전건 거부) · dedup · upsert · 이름 학습 · 정규화 · 신선도 · 증분 목록 · 통계 · prune · CLI
```
