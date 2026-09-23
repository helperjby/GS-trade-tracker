# 배포·실기기 게이트(G7) 런북

관측기 exe 를 **아는 사람 최대 7명**에게 돌리고, 육의전을 한 번 열면 허브에 뜨는 것까지 확인하는 절차.
설계 맥락은 [PLAN.md](PLAN.md) §배포·실기기 게이트(G7), 허브 계약은 [HUB-PROTOCOL.md](HUB-PROTOCOL.md),
Pi 운영(재배포·Funnel·기기 관리)은 [../hub/README.md](../hub/README.md) — **여기서는 옮겨 적지 않고 순서만 잡는다.**

> 공개 레포 규칙: 아래 `<…>` 자리표시자에 실제 주소·초대 코드·기기명을 **적어 커밋하지 않는다**(AGENTS.md).

## 0. Pi 에 떠 있는 허브가 어느 판인지 먼저 본다

초대 코드 자기등록(`POST /api/market/register`)·공개 리스너 8801·`devices` 표는 **PR #10 이후 판**에만 있고, 슬롯별 코드
(`invite_codes`)는 2026-09-23 판부터다.
그 전 판이 떠 있으면 지인 PC 의 첫 실행 등록이 실패한다(옛 판은 `/api/*` 전부에 관리 시크릿을 요구해 `401 unauthorized`).
실측 2026-09-23: 09-22 11:24 배포본이 정확히 이 상태였다.

```bash
cd ~/yuktracker-hub && docker compose ps && curl -s http://127.0.0.1:8800/ && echo
curl -s -X POST http://127.0.0.1:8800/api/market/register -H "Content-Type: application/json" -d '{"v":1,"invite_code":"x"}'
```

| 응답 | 뜻 | 할 일 |
|---|---|---|
| `401 bad_invite` | 최신 판 + 초대 코드 설정됨 | 1번으로 |
| `403 registration_closed` | 최신 판인데 `invite_codes` 가 비었다 | `config.json` 에 `invite_codes` 표 추가(hub/README 2) → `docker compose restart` |
| 기동 로그에 `'invite_code' 는 … 폐기` | 슬롯 판 허브에 구 config | `invite_code`·`max_devices` 를 지우고 `invite_codes` 표로(hub/README 2) |
| `401 unauthorized` (본문에 `bad_invite` 가 **없다**) | **옛 판** — PR #10 이전의 전역 Bearer 미들웨어 | hub/README "파이 배포" 1)~3) 으로 재복사·재기동 |
| `404` (또는 라우트 없음) | **옛 판** | 위와 같음 |

`docker compose ps` 의 포트에 `8801` 이 없거나 `docker compose exec yuktracker-hub ls devices.py` 가 실패해도 옛 판이다.

`GET /api/market/ping` 이 404 면 허브가 PR-Y5 이전 판이다(옛 판은 `401 unauthorized`) — 관측기 자가진단의 "기기 토큰" 줄이 "허브가 옛 판입니다"로 뜬다.

## 1. 허브 — 공개 노출까지

hub/README "파이 배포" → "공개 노출(Tailscale Funnel)" 순서대로. 이 Pi 는 Funnel 443·8443 을 다른 서비스가 이미 쓰고 있어
공개 리스너는 **10000 번**(`--https=10000`, 주소 `https://<pi-node>.<tailnet>.ts.net:10000`)으로 낸다(2026-09-23 결정). 끝나면 **외부망(폰 LTE)** 에서 게이트가 전부 맞아야 한다:
`GET /` 200 · `stats` 403 `not_public`(더미 Bearer) · `register` 200/401 · `ping` 200 · `devices.py list` 에 그 기기.
게이트용 기기는 어느 슬롯 코드로 만들어도 그 슬롯의 지인이 등록하면 자동 교체되지만, 명단을 깨끗이 두려면 `devices.py revoke <device_id>
--note gate` 로 지운다.

## 2. exe 빌드 — 허브 주소는 빌드 때 주입한다

공개 레포 소스에 tailnet 주소를 두지 않으므로 주소는 환경변수로 받아 `_build_config.py`(gitignore)로 들어간다.

```bat
set YUKTRACKER_HUB_URL=https://<pi-node>.<tailnet>.ts.net:10000
build.bat
```

`build.bat` 은 출처 선언 검사(`sync_seassist_core.py --check`) → 테스트 → PyInstaller → `dist\YukTracker.exe` → SHA256 순이다.
빌드한 PC 에서 한 번 확인한다(관리자 콘솔):

```bat
dist\YukTracker.exe --selftest
```

`허브 설정` 줄에 주입된 주소가 보이면 주입 성공이다. 주소 없이 빌드하면 받는 사람이 `--hub-url` 을 직접 줘야 한다.

## 3. 지인에게 보낼 안내 (그대로 복사해서 쓰는 문구)

> **거상 육의전 시세 모으기 — 설치 안내**
>
> 1. **Npcap 설치**: https://npcap.com → `Npcap ... installer` 내려받아 기본값으로 설치(이미 있으면 건너뜀).
> 2. 보낸 `YukTracker.exe` 를 아무 폴더에나 두고 **오른쪽 클릭 → 관리자 권한으로 실행**.
>    (패킷을 읽으려면 관리자 권한이 필요합니다. 게임에 뭔가를 입력하거나 보내지는 않습니다 — 받는 패킷만 읽습니다.)
> 3. 처음 한 번만 **초대 코드**를 물어봅니다 → `<본인 초대코드>` 를 붙여넣고 Enter. (코드는 사람마다 다릅니다 — 남에게 넘기지 마세요.)
> 4. 거상을 켜고 접속하면 `캡처 시작` 줄이 뜹니다. 그 뒤 **육의전을 열고 목록을 몇 페이지 넘겨 주세요** —
>    창에 `[육의전] … 목록 N행` 이 찍히면 올라간 겁니다. 그냥 켜 두면 육의전을 열 때마다 자동으로 모입니다.
> 5. 끄려면 창에 `q` + Enter (또는 창 닫기). 창 안을 마우스로 드래그하면 글자만 안 올라올 수 있는데(관측·업로드는 계속),
>    그럴 땐 Enter 나 Esc 를 한 번 누르면 됩니다.
>
> **안 될 때**: 같은 창에서 `q` 로 끈 뒤, `YukTracker.exe --selftest` 를 관리자 권한으로 실행해서 나온 화면을 그대로 보내 주세요.
> 어디서 막혔는지 한 줄씩 나옵니다.
>
> 프로그램을 지웠다가 다시 깔거나 PC 를 바꿔도 **같은 코드**로 다시 등록하면 됩니다(옛 기기는 자동으로 빠집니다).

보내는 쪽 메모: 지인마다 **본인 슬롯의 코드**(`config.json` 의 `invite_codes`) 하나씩, 관리 시크릿은 절대 보내지 않는다(exe 에도 없다).
등록되면 `devices.py list` 에 슬롯 이름이 별칭으로 바로 찍힌다 — 이름을 바꾸고 싶을 때만 `devices.py alias <device_id> "<이름>"`.

## 4. G7 게이트 — 이게 맞으면 통과

| # | 확인 | 어디서 |
|---|---|---|
| 1 | 승격·Npcap·캡처 시작 줄이 뜬다 | 지인 PC 콘솔 (또는 `--selftest` 전부 O) |
| 2 | 육의전 1회 열람 = 관측 ≥1 (`[육의전] … 목록 N행`) | 지인 PC 콘솔 |
| 3 | 허브 반영 ≤10s: `search?q=<그 아이템>` 에 그 목록 | 제작자 PC(tailnet 8800) |
| 4 | 음성 0건 — 육의전을 안 연 동안 관측이 생기지 않는다 | 종료 헬스 `육의전 N쪽` |
| 5 | 스풀 재시도: 허브를 잠깐 내렸다 올려도 `대기 N배치` 가 0 으로 돌아온다 | 지인 PC 콘솔 |
| 6 | `devices.py list` 에 그 기기의 `uploads` 가 1 이상 | Pi |

```bash
# 3번 (제작자 PC, tailnet 직접 접속 — 공개 Funnel 로는 403 not_public 이 정상)
curl -s -H "Authorization: Bearer <관리 시크릿>" "http://<pi-tailnet-ip>:8800/api/market/search?q=<아이템>"
```

## 5. G7 2차 — 운영 확인

- `GET /api/market/stats` 의 `observations`·`devices_registered` 가 는다(아직 안 들어온 사람 = `devices_max` − (`devices_registered` −
  `devices_orphaned`); `devices_orphaned` ≠ 0 이면 설정 슬롯 이름과 안 맞는 기기가 있다 — hub/README 운영 메모).
- `docker compose logs --tail 50` 에 `market 관측 수신: d-… (별칭) 신규 N / 중복 M / 행 K` 줄.
- 기기가 하나도 안 올라오면: 그 PC 에서 `--selftest` → `기기 토큰` 줄이 401/403 인지, `패킷 흐름` 줄이 X 인지로 갈린다.
- 결과는 `SESSION_STATE.md` 에 날짜와 함께 한 줄 기록(기기명·주소는 자리표시자로).

## 자주 나오는 실패와 원인

| 증상 | 원인 | 조치 |
|---|---|---|
| `X Npcap — 관리자 권한이 아니라…` | 승격 안 됨 | 오른쪽 클릭 → 관리자 권한으로 실행 |
| `X 패킷 흐름 — 거상은 떠 있는데…` | 다른 어댑터·미접속 | 서버 접속 확인, VPN/가상 어댑터 끄고 재시도 |
| `! 아이템 표 — 못 읽었습니다` | 클라 폴더가 기본 경로 밖 | `--client-dir "<거상 폴더>"` (이름 없이도 관측·업로드는 된다) |
| `X 기기 토큰 — 401` | 토큰 무효(허브 DB 교체 등) | 본인 코드로 `--invite-code <코드>` 재등록 |
| `X 기기 토큰 — 403 제거됨` | 관리자가 명단에서 뺌, 또는 같은 코드로 다른 PC 가 등록해 교체됨 | 본인 PC 가 맞으면 같은 코드로 재등록(상대가 교체됨), 아니면 관리자에게 문의 |
| `X 기기 토큰 — 옛 판입니다` | 허브가 PR-Y5 이전 | Pi 재배포(0번) |
| `X 허브 도달 — 닿지 못했습니다` | 주소 오타(`:10000` 누락)·Funnel 꺼짐 | Pi 에서 `tailscale funnel status` |
| `! 업로드 대기 N배치` 가 안 줄어듦 | 401/403 로 업로더 정지 | 같은 화면의 `기기 토큰` 줄을 본다 |
