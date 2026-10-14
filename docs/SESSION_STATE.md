# Session State

Updated: 2026-09-23 저녁 (Asia/Seoul) — **허브 소멸 추정(아래 절, PR 대기) · 실기기 G7 통과(F1_JBY; 결함 2건 수정 PR #16 머지) · 슬롯별 초대 코드 PR #15 머지·Pi 7슬롯 재배포 — 다음은 소멸 PR 머지·Pi 재배포 → 지인 전달 → PR-Y4 미루봇** ·
PR-Y2' 이식 + PR-Y1b 관측·업로드 완료(아래 두 절) ·
허브 공개 업로드 PR #10 **머지 완료** ·
**방향 전환(사용자 결정 2026-09-22): SEAssist 레포에는 더 이상 머지하지 않는다.**
**PR #5(PR-Y2b 아이템 표)·PR #6(PR-Y3 허브) 머지 완료**(https://github.com/helperjby/GS-trade-tracker/pull/5 840d8d3 · https://github.com/helperjby/GS-trade-tracker/pull/6 1ed43ef,
각 `/code-review high` 15건·14건 전부 반영) · **허브 Pi 배포 완료(2026-09-22 11:24, `~/yuktracker-hub`, :8800, G7 `stats` 응답 확인 — 아래 "허브 배포")** ·
SEAssist PR #306(PR-Y2) **미머지 → 닫음 예정, 이 레포로 이식(PR-Y2')** · Step 1 = SEAssist #304·#305(머지, 동결 시점 참조)

## 허브 소멸 추정 — 관측 시각으로 목록 소멸 경계 계산 (2026-09-23 저녁, 브랜치 `feat/hub-listing-expiry-20260923`)

- 사용자가 준 게임 사실: 등록일 D(KST)에 올린 물품은 팔리지 않으면 **단기 D+2 00:00 · 장기 D+3 00:00** 에 목록에서 사라진다(9/23 20:03 등록 →
  단기 9/25 00:00). 등록 시각은 패킷에 없어(PACKET-MARKET §1) 관측 시각으로 추정. 사용자 결정 3건: 허브+봇 계약까지 반영 · 등록일 추정은
  **관측 시각 경계만**(등록 id 자정 앵커 보류) · `기간`(@45, H-2609-11 D) **검정 전엔 모든 행을 단기로**.
- 종전 24h 나이 필터는 양방향으로 틀렸다(살아 있는 행을 숨기고, 사라진 행을 보였다). 새 규칙(HUB-PROTOCOL §4): 관측 T 에 살아 있었다 →
  등록일 ∈ {date(T)−1, date(T)} → `expires_from_ts` = date(last_seen)+1 00:00(하한) · `expires_by_ts` = max(date(first_seen)+2 00:00, 하한)(상한).
  자정을 걸쳐 두 번 보면 둘이 같아져 등록일 확정. `/code-review 18 high` 7건 중 6건 반영(상한에 하한 합치기 = 느린 시계 기기·from>by·장기
  재관측 세 건 해결, SQL 날짜 산술 제거(열 비교 `_LIVE_WHERE`), ctor 정수 검증, 자정 경계 테스트 플레이크, 배포 config 의 옛 86400 기동 경고
  `_warn_short_max_age`); 1건(장기 물품을 단기로 보는 것)은 사용자 결정이라 문서 주석만.
- 구현(스키마 무변경, API additive): `hub/db.py` `kst_midnight`·`expiry_bounds`(정의 하나; SQL 은 열 비교 `_LIVE_WHERE`, 파이썬 정의와 일치 테스트) · `Database(listing_expiry_days=)`
  · 행 dict 에 두 필드 · `search_market(..., now)` 는 `expires_by_ts > now` 필터 · `stats.live_listings`·`expiry_days`; `hub/server.py` 설정
  `listing_expiry_days`(2, 정수 ≥1 검증) · `search_max_age_sec` 기본 86400 → **259200**(72h 안전망) · search 응답 `expiry_days`. 봇 표시 형식에
  소멸 칸(`~M/D 00:00`, 확정이 아니면 `(M/D 부터 가능)`). 허브 테스트 115 passed, 관측기 186 passed, 벤더 체크 일치.
- 후속: H-2609-11 → B 승격 뒤 행별 일수(2=장기 → 3일, `expiry_bounds`·`_LIVE_WHERE` 에 flag45 분기) · 봇(PR-Y4)은 `expires_from/by_ts` 소비.

## 실기기 G7 1차 (2026-09-23 오후, F1_JBY) — 업로더 스레드 미기동 결함 발견·수정

- F1_JBY(관리자, 거상 3클라): `--selftest` 9줄 중 8 O + 기기 토큰 `!`(미등록) → 콘솔 프롬프트 등록 3회 실패(붙여넣은 코드가 12자 중
  11자 — 이 기기의 콘솔 입력 잘림, 코드 쪽 읽기는 `readline`+strip 뿐) → `--invite-code` 인자로 **slot1 등록 성공**(`(슬롯 slot1)`),
  `[패킷] 캡처 시작`·`흐름을 잡았습니다`·육의전 페이지 인식까지 정상. **그러나 허브에 0건**: `devices.py list` uploads 0, 접근 로그에
  F1_JBY 의 `POST /api/market/observations` 가 한 번도 없음.
- 원인: `agent.run` 이 `setup_market` 이 만든 `spool.Uploader` 를 **`start()` 하지 않았다** — 관측은 큐(`enqueue`)에 쌓이고 스레드가
  없으니 스풀 파일도 POST 도 없고, 종료 때 `stop()` 만 불러 큐가 그대로 버려진다(스풀 이월도 없음). PR-Y1b 의 종단 스모크는 업로더를
  직접 몰아 이 경로를 못 봤다. 수정: `run()` 이 `market.uploader.start()`(이 PR) + 회귀 테스트 2건(run 이 start 를 부른다 · 진짜
  스레드가 큐 → 스풀 → POST). 이 실행에서 본 페이지들은 유실 — 새 exe 로 다시 열람해야 한다.
- **`/code-review 16 high` 8건 전부 반영**(스레드가 실제로 돌게 되자 드러난 후속): ① 백오프·429 대기가 `time.sleep`(최대 300s)이라
  `stop()` 이 못 깨우고 종료 내림이 안 돌던 것 → `retry_at`(단조시계) 로 바꿔 스레드를 재우지 않는다, ② 그래서 백오프 중에도 루프가
  큐를 스풀로 내린다(허브가 죽어 있어도 512 넘쳐 버리지 않음), ③ `start()` 는 엔진이 뜬 뒤 try 안에서(엔진 실패 경로에는 안 띄움,
  finally 가 반드시 close), ④ 콘솔 X·로그오프 훅도 `market.close()`, ⑤ join 시간 초과면 메인 스레드가 큐를 스풀에 내리고 그래도
  남으면 종료 요약에 `큐 미저장 N(유실)`, ⑥ 수명은 `Market.start()/close()` 한 쌍(`_stop_uploader` 폐기), ⑦⑧ run() 을 **진짜**
  Uploader 로 모는 통합 테스트 3건(지난 스풀이 POST 된다·엔진 실패면 안 뜬다·POST 에 붙들려도 큐는 스풀로) + 스텁 순서 검증 +
  `setup_market(uploader_kw=)` 주입. 루트 245 passed.
- **G7 2차(14:58~15:03, 새 exe)**: 클라3 육의전 열람 → `[육의전] 클라3 목록 10행 / 총 235쪽` 8건 → **허브 도착**(관측 8·행 61,
  이름 해석 됨, `devices.py list` uploads 4) — 게이트 2·3·6 통과. 단 첫 페이지 인식 14:59:02 가 허브에는 15:01:57(3분) 에
  도착했고 콘솔 줄도 15:01:59·15:03:13 에 몰려 찍혔다. 원인 = conhost QuickEdit: 사용자가 콘솔 글자를 드래그해 복사하는
  동안 콘솔 쓰기가 통째로 멈추고, `say` 가 스니퍼 스레드에서 동기 `print` 라 패킷 처리·enqueue·업로드가 같이 멈췄다(풀리면
  몰림). 수정(이 PR 3차 커밋): `ConsoleOut` 쓰기 스레드(엔진이 뜬 뒤 비동기, 큐 2000·넘치면 세어 종료 때 한 줄), 로거 핸들러도
  같은 경로, 관측 콜백은 enqueue 를 say 보다 먼저, 관측 모드는 QuickEdit 끔(`--selftest` 는 복사해야 하니 유지), DEPLOY 안내문.
- **G7 3차(15:05~15:14, 3차 빌드 exe) — 게이트 6항목 전부 통과**: ① 승격·Npcap·캡처 시작 줄 O(자가진단 8 O) ② 육의전 열람 = 관측
  ≥1(8→14쪽 도착) ③ 허브 반영 수 초(콘솔 정지 수정 뒤) ④ 음성 0 — 창을 안 여는 90초 동안 관측 31 고정 ⑤ **스풀 재시도**: 허브
  `docker compose stop` 15:11:45 → 사용자 페이지 넘김 → `start` 15:12:19 → 내려 있던 동안의 16쪽이 11초 안에 자동으로 이어
  올라옴(14→30) ⑥ `devices.py list` uploads 27. 관측 31·목록 61+, 이름 해석 정상. 남은 것 = 폰 LTE 재확인(선택)·지인 전달(슬롯 2~7).
- **지인 1호(slot2) 첫 시도 실패 → Funnel 장애 발견·해결(15:20~15:35)**: 지인 PC 콘솔 `허브에 닿지 못했습니다 — SSL: UNEXPECTED_EOF`.
  공개 DNS 의 인그레스 IPv4 로 `--resolve` 해 보니 **443·8443·10000 전부 TLS 직후 끊김** — Pi `journalctl -u tailscaled` 에 `peerapi:
  ingress: denied; no ingress cap` 346회, **9월 11일부터**. 넷맵의 CapGrant(인그레스 23노드 → Pi) 는 정확했고 tailscaled 재시작·Funnel
  토글로도 안 풀림 → tailscale **1.102.1 → 1.102.4** 업데이트로 즉시 해결(두 인그레스 IP 모두 200, register 401, Python TLS OK).
  지금까지의 '외부망 게이트' 는 전부 tailnet 안(제작자 PC·Pi)에서 한 것이라 MagicDNS 직행으로 Funnel 을 안 거쳤다 — hub/README·
  DEPLOY 를 '반드시 tailnet 밖에서(폰 LTE 또는 `--resolve`)' 로 정정. 지인은 같은 코드로 재시도하면 된다(exe 무변경).
- 이 트랙에서 배운 것(문서에 반영): 콘솔 프롬프트는 이 기기에서 글자를 잘라먹으니 초대 코드는 `--invite-code` 인자로(DEPLOY 안내문은
  프롬프트 유지 — 코드가 짧고 대부분 PC 는 정상); 스니퍼 스레드는 콘솔에 직접 쓰지 않는다(`ConsoleOut`); 업로더 수명은 `Market` 이 쥔다.

## 슬롯별 초대 코드 (2026-09-23, PR #15)

- 배경: 사용자 제안 "invite_code 를 slot 별로 지정하는 게 인식·관리가 편할 것" — 코드 하나·임의 `device_id`·사후 별칭 구조는 등록
  즉시 누구인지 안 보이고, 재설치는 `registration_full` 로 뒤늦게 터졌다(hub/README 재설치 주의). 결정 2건(AskUserQuestion):
  ① 같은 슬롯 재등록 = **옛 기기 자동 교체**(거부 아님), ② 단일 `invite_code` **폐기**(`invite_codes` 만, 구 키는 이관 안내로 기동 거부).
- 허브: `invite_codes = {슬롯 이름: 코드}`(슬롯 이름 = label 규칙·중복 금지, 코드 = 8자↑·예시값/secret 거부·**슬롯 간 중복 금지**,
  `slot_for_invite` 는 조기 종료 없이 전부 상수 시간 비교), `max_devices` 폐기(정원 = 슬롯 수 → `stats.devices_max`), `devices.slot`
  열(기존 DB 는 기동 때 `PRAGMA table_info` → `ADD COLUMN` — 무마이그레이션 규칙의 유일한 additive 예외), `register_slot_device` 가
  삽입 + 같은 슬롯 활성 기기 제거(note `재등록 교체 → <새 id>`)를 한 트랜잭션에서, 응답에 `slot`·`replaced`, 초기 `alias` = 슬롯 이름,
  `registration_full` 폐지, `devices.py list` 에 `slot` 열, `stats.devices[].slot`. 관측기는 응답의 `slot` 을 등록 완료 줄에 덧붙일 뿐
  (코드 무변경에 가깝다).
- 문서: HUB-PROTOCOL §0·§1·§3-0·§3-4·§3-6·§6·§7, hub/README(설정 생성 = python 한 토막으로 슬롯 7개, 운영 메모 재설치 절 교체),
  DEPLOY(§0 이관 행·§1·§3 안내문 "본인 코드"·§5·증상표), PLAN, config.json.example.
- 검증: hub 테스트(슬롯 교체·다른 슬롯 불간섭·옛 토큰 403·구 DB ADD COLUMN·설정 검증 표) + 루트 전체 통과(아래 커밋 메시지 수치).
- **`/code-review 15 high` 10건 전부 반영**: ① 슬롯 이름 변경으로 고아가 된 기기 = 기동 경고 + `stats.devices_orphaned` + 문서(이름은
  정체성), ② 교체 때 관리자 메모는 덧붙임(`… / 재등록 교체 → id`), ③ 별칭 승계, ④ 정원 폐지의 보완 = 슬롯당 등록 상한
  `register_slot_limit_per_hour`(5, 429) + `devices.py list --active`, ⑤ ADD COLUMN 은 PRAGMA 검사 없이 시도하고 중복만 삼킴(서버·CLI
  동시 첫 열기 경쟁 제거), ⑥ `_tx` 가 `BEGIN IMMEDIATE` 로 조회부터 잠금(동시 unrevoke 와 안 엇갈림), ⑦ '정원 자리' 문구 4곳 정리,
  ⑧ 예시값 규칙은 `CHANGE-ME` 접두 하나, ⑨ 슬롯 이름·label 검사 `printable_name` 한 함수, ⑩ `create_device` 는 테스트·수동용임을
  명시 + 슬롯 없는 행은 `devices_orphaned` 로 드러남.
- 배포: Pi `config.json` 을 `invite_codes` 7슬롯으로 이관(secret 유지, 기존 단일 코드는 폐기) → hub/ 재복사 → `docker compose up -d
  --build` → 기동 로그 `registration=open(7 slots)` → 게이트(§0 표) → exe 는 무변경(재빌드 불필요).

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
  않게), `device_id` 는 exe 가 POST 시점에 채움, 취소는 soft. 1차 커밋 1131ac2: hub 테스트 67 passed, 로컬 스모크 17단계 통과.
- **`/code-review 10 high` 13건 전부 반영**(2차 커밋): ① 헤더 부재 = 직접 접속이라는 fail-open → **공개 리스너 `public_port` 8801 분리**
  (`serve()` 가 관리 8800 + 공개 8801 을 한 프로세스에서, DB·limiter 공유; 공개 리스너는 헤더 무관 전부 공개, 8800 의 헤더 검사는 2차
  방어; compose `8801:8801`, Funnel 은 8801), ② 인증 실패 집계는 토큰 검사 **뒤**(공유 NAT 의 정상 기기 안 막힘), ③ XFF 없는 공개
  요청은 `public:?` 버킷 + 경고 1회, ④ 정원 = 활성 정의(`device_idle_days` + 미업로드 하루) — **아래 09-22 사용자 결정으로 폐기**,
  ⑤ 거부(400/403)도 last_seen, ⑥ 기기 기록은 관측과 같은 트랜잭션(커밋 1회, 실패 시 무기록), ⑦ 등록 본문
  4KiB(411/413, 파싱 전), ⑧ 429 가 취소 검사보다 먼저, ⑨ 초대 코드 strip(config·요청), ⑩ CLI 중복 취소는 시각 보존·"이미 활성",
  ⑪ 전각 폭 정렬, ⑫ stats 가 `count_active_devices` 재사용(`devices_active` 추가), ⑬ README 게이트 더미 Bearer·본문 모양·`hub_secret` 문장.
- 문서: HUB-PROTOCOL §0 전송 전제 개정·§1 devices·§3-0·§3-1·§3-4·§3-6·§6·§7, hub/README "공개 노출" 절(Funnel 절차·외부망 게이트·운영 메모,
  `sed` 순서 INVITE 먼저), PLAN §PR-Y3 공개 항목·§PR-Y1b(관측기 키 `hub_url`·`hub_device_id`·`hub_token`, 첫 실행 등록, 429/401/403 규약)·G7.
- **기기 명단 규칙 확정(2026-09-22 사용자 지시, PR #10 에 반영)**: ① 배포 대상 = **아는 사람 최대 7명** → `max_devices` 기본 500 → **7**.
  ② **활성 여부에 따른 자동 제외 없음** → `device_idle_days`·미업로드 유예(`UNSEEN_GRACE_SEC`)·`devices.py revoke --stale` 삭제,
  "등록"의 정의는 `revoked_ts IS NULL` 하나(`_ACTIVE_WHERE`). 한 번도 안 올린 기기도 자리를 차지하고, 자리를 비우는 유일한 길은
  관리자의 `revoke`. ③ 관리자가 **제거**(`revoke`, soft — 행은 감사용으로 남음)와 **별칭**(`devices` 표 `alias` + `devices.py alias`)을
  할 수 있다 — 별칭은 기기가 등록 때 스스로 적은 `label` 과 별개이고 목록·서버 로그에서 앞선다. `stats` 는 `devices_active` 대신
  `devices_max`(설정 정원)를 싣는다. 검증: hub 82 passed, CLI 왕복(별칭 지정·해제·제거로 자리 반환) 실행 확인.
- **고정 기기 전제로 범위 축소(2026-09-22 사용자 지시)**: 배포 대상이 지인 고정 기기(slot1~5 배정, 최대 7)라 슬롯 고갈·
  XFF 우회 방어·자가 치유 로직은 하지 않기로. 남긴 기술 부채는 "서버 먹통 방지" 셋뿐이고 전부 반영: ① compose 공개
  리스너 `127.0.0.1:8801:8801`(Funnel 은 같은 호스트 프록시라 무영향, LAN 직접 접속만 차단 — 제작자 PC 는 Tailscale VPN
  으로 8800 직결이라 8800 은 전 인터페이스 유지), ② `request.json()` 이 `LookupError` 도 받아 charset 폭탄이 500+
  트레이스백 대신 400(무인증 라우트의 로그·디스크 고갈 차단), ③ `Database._tx` 로 모든 쓰기의 commit/rollback 보장 —
  `prune` 두 DELETE·`touch_device` 가 잠금으로 실패해도 트랜잭션이 안 남는다(한 명의 불안정한 업로드가 WAL 스냅샷을
  붙들어 나머지 전원을 멈추던 경로). 검증: 잠긴 DB 상대로 둘 다 `in_transaction=False`, 잠금 해제 직후 CLI 의 제거가
  즉시 보임. charset `bogus-enc` → 400. hub 82 passed · 루트 92 passed.
- **재설치 동작 확인(사용자 질문)**: `device_id` 는 허브가 등록마다 새로 발급 → 재설치 재등록은 409 거부도, 기존 행
  갱신도 아니고 **새 행 추가**(옛 행이 자리를 계속 차지). 실패는 재설치 시점이 아니라 다음 사람 등록 때 `registration_full`
  로 뒤늦게 난다 → 운영 규칙 = "재설치하면 알려줘" + 관리자가 같은 별칭 두 줄 중 옛 `device_id` 를 `revoke`. hub/README 에 기록.
- 남은 것: PR #9 → #10 머지 → Pi: hub/ 재복사 → `config.json` 에 `invite_code`(+`public_port` 8801 기본) 추가 → `docker compose up -d
  --build`(포트 8800·8801) → `sudo tailscale funnel --bg 8801`(관리 콘솔 HTTPS·`funnel` 노드 속성) → 폰 LTE 게이트(`GET /` 200 · stats 403
  (더미 Bearer) · register 200/401) → `devices.py list`. 그 뒤 PR-Y2' → PR-Y1b(등록 UX 포함).

## PR-Y2' 이식 (2026-09-22 저녁, 이 레포 — **PR #11 머지 완료**)

> https://github.com/helperjby/GS-trade-tracker/pull/11 (a780059). PR-Y1b(#12)는 그 위 스택이었고
> 머지와 함께 base 가 `main` 으로 자동 전환됐다.

- **벤더 동결 처리 = 전량 소유 전환**(사용자 결정). 후보 셋 중 (a) 별도 모듈/서브클래스는 불가능에 가깝다 — 육의전 전량 대기
  분기가 `StreamFramer.feed()` 루프 안(`observe_wordinput` 과 같은 자리)에 있어야 해서 서브클래스는 100줄 루프 복제가 된다.
  `tools/sync_seassist_core.py` 는 복사(`sync`)를 잃고 **출처 대비 차이 보고**만 한다: `--check`(차이 == `VENDOR.json` 의
  `diverged` 선언인가, rc 1 = 불일치) · `--upstream`(상류 체크아웃과 파일별 비교, 정보용). `VENDOR.json` 은
  `origin{commit b54cb73, files{sha256,bytes}}` + `repo_own` + `diverged[{file,why}]` 구조.
- **이식은 패치가 아니라 통째 교체**: 동결 시점(b54cb73) 두 파일의 LF 정규화 해시가 기존 핀과 정확히 일치함을 확인한 뒤
  이식 원본의 판으로 갈아끼웠다 → 그 커밋의 diff 가 곧 순수 육의전 델타다. `gersang_protocol.py`(+126) ·
  `packet_state_source.py`(+54) · `seassist/packet_market.py`(신규 218줄, stdlib + `.gersang_protocol` 만).
- 콜백 계약 `market_cb(slot_idx, page, observation, *, pid)` — 파싱 성공분만, 스니퍼 스레드, 예외는 엔진이 삼키고
  `market_callback_errors`. 헬스 7종(`market_frames`·`rows`·`rejected`·`dropped`·`parse_failures`·`anomalies`·`callback_errors`).
- **가져오지 않은 것**(범위 결정): 그림자 원장 `note_market`, SEAssist 전용 도구(`packet_explore`·`check_packet_corpus`·
  `packet_patch_audit`)·코퍼스 매니페스트, GUI 배선.
- 테스트 37건 이식 + 하네스 자작: `tests/packet_frames.py`(합성 빌더 — 실캡처 조철 본문 대신 합성 `jochul_body`),
  `tests/packet_engine_harness.py`(pcap·flow 네임스페이스 스텁으로 DLL 없이 `_capture()` 구동).
- `tools/market_probe.py`(신규): 창 파일 읽기 전용 재생 — **라이브 재생(프로덕션 경로) vs 프레이머 독립 프로브** 2중 대조,
  라벨 대조, 판매자명 **기본 마스킹**(`--raw-seller` 로만 원본). 불일치 rc 1 · 페이지 0 rc 2. 테스트 6건.
- 검증: 루트 137 passed · 검토 폴더 80창 재생 = **9창 40페이지·369행·라벨 8/8·라이브 == 프로브·거부 0·유실 0**(PR-Y2 기록 재현) ·
  고정 코퍼스 45창 = 8창 39페이지(9번째 등록 창은 음성이라 0페이지, 매니페스트의 market 39 와 일치).
- 문서: `docs/PACKET-MARKET.md` 신규(이 트랙의 프로토콜 정본 — 필드 표·라벨 대조·예측 검정·가설 레지스트리 H-2609-07~11·
  재현 기록·PATCH-RECHECK·다음 캡처). 판매자명은 합성(`판매자A`~`H`), 기기명·창 파일명·실캡처 경로는 옮기지 않았다.

## PR-Y1b 관측 모드·등록 UX·업로드 (2026-09-22 저녁, 이 레포)

- 배선: 엔진 `market_cb` → `market_observer`(봉투) → 큐 → `spool`(배치 파일) → 업로더 스레드 → `hub_client`(urllib).
  스니퍼 스레드는 print + enqueue 만 한다(파일·네트워크 금지). `agent.setup_market` 이 전부를 조립하고,
  테스트는 설정 경로·스풀 폴더·등록 함수·POST 함수를 주입한다(사용자 프로필·네트워크 무접촉).
- **등록 UX**: `hub_token` 이 없으면 `--invite-code` 또는 콘솔 프롬프트 → `POST /api/market/register` →
  `hub_device_id`·`hub_token` 을 `%APPDATA%\YukTracker\config.json` 에 저장(초대 코드는 저장하지 않는다).
  실패(401 `bad_invite`·403 `registration_full`/`registration_closed`·429·인증서)는 **사유 한 줄 + 관측 계속**이고
  자동 재등록은 하지 않는다. 빈 입력(EOF 포함)이면 업로드 없이 관측만 한다.
- **`obs_id` 접두사 = `local_id`**(설치본 고유 8자리 hex, 설정에 1회 생성) — 허브 `device_id` 가 아니다. 재등록으로
  기기 id 가 바뀌어도 스풀에 남아 있던 관측의 id 가 흔들리지 않는다. HUB-PROTOCOL §3-1 에 이 정의를 적었다.
- **시각**: 엔진 `now_fn` 이 단조시계라 `agent_ts = time.time() - (monotonic() - observation.ts)` 로 되돌린다
  (스풀에 묵었다 늦게 올라가도 "그때 본 목록"의 시각이 남는다).
- 응답별 행동 한 곳(`hub_client.classify_upload`): 200 삭제 · 400 격리 · 401/403 정지(스풀 유지) · 429 `Retry-After`
  대기 · 413 분할 · 5xx·네트워크 백오프(1s→300s) · 인증서 실패는 무한 재시도 대신 정지 + 안내.
- **빌드 시 허브 주소 주입**: `tools/gen_build_config.py` 가 `%YUKTRACKER_HUB_URL%` → `src/yuktracker/_build_config.py`
  (gitignore). 주소 우선순위 = `--hub-url` > 설정 > 환경변수 > 빌드 주입 > 없음(업로드 비활성).
- CLI 추가: `--hub-url` · `--invite-code` · `--device-label`(기본 호스트명) · `--client-dir` · `--no-upload`.
  모드 문구가 "관측"/"관측 + 수집 N분" 으로 바뀌었고 종료 헬스에 육의전·업로드 카운터가 붙는다.
- 검증: 루트 **199 passed**(신규 62) · hub 82 passed · **종단 스모크 13/13**(로컬 허브 관리·공개 두 리스너:
  공개 stats 403 → 잘못된 초대 코드 401 → 등록 → 업로드 200 → 같은 obs_id 중복 → 검색 `작은바람의속성석 7개
  1,234,000원` → 미해석 행 null 저장 → 잘못된 토큰 401 정지·스풀 유지 → device_mismatch → 400 격리 →
  재시작 뒤 남은 스풀 자동 업로드 → stats 관측 2·기기 1).
- 남은 것: 실기기 G7 — exe 배포 → 초대 코드 등록 → 육의전 1회 열람 → 허브 `search` 반영 ≤10s · 음성 0건.

## PR-Y5 배포 준비 — 자가진단 · 관측 가시성 · 허브 ping (2026-09-22 밤, 이 레포)

- 동기: G7 은 지인 최대 7명의 PC 에서 도는 단계인데 원격 지원 수단이 없었다. ① 관측 모드는 `is_capturing()` 을 수집 모드에서만
  봐서(`_open_window` 안) 거상이 꺼져 있든 Npcap 이 다른 어댑터를 잡았든 **콘솔이 조용**했다. ② 관측기 자격은 기기 토큰 하나인데
  조회 라우트는 공개 리스너에서 403 `not_public`, 빈 배치 업로드는 400 이라 **토큰 유효성을 물을 라우트가 없었다**.
  ③ 지인에게 줄 설치 안내문이 없었다.
- 허브(additive, HUB-PROTOCOL §3-7): `GET /api/market/ping` — `ROUTE_AUTH` 한 줄 + 핸들러(DB 무접촉) + `add_get`. 인증·429 는
  기존 미들웨어 그대로(업로드 버킷 공유), 공개 리스너 통과, 취소 기기는 403 `device_revoked`. 스키마·기존 라우트 무변경.
- 관측기 `--selftest`(`src/yuktracker/selftest.py`): 9줄(관리자 권한·거상 실행·Npcap·패킷 흐름·아이템 표·허브 설정·허브 도달·
  기기 토큰·업로드 대기) × `O/!/X/-`, rc 0/1/2. 판정부는 순수 함수 + `Probes` 주입(테스트가 Npcap·네트워크·`%APPDATA%` 무접촉).
  `hub_client` 에 `get_json`·`status`·`ping`·`describe_status`·`describe_ping` 추가. **옛 판 허브**(404 / 403 `not_public`)를
  "허브가 옛 판입니다"로 묶어 안내한다 — Pi 재배포 전에 지인이 받으면 바로 이 줄이 뜬다.
- 실기기에서 드러나 고친 것: 비승격 실행의 Npcap 줄이 벤더 문구(`관리자 권한이 아닙니다 — 화면 감지를 계속 사용합니다`)에
  "npcap.com 에서 설치하세요"를 붙여 **멀쩡한 설치를 다시 깔라고** 말했다 → 사유 토큰별 안내(`NPCAP_HINTS`)로 교체
  (`not_elevated` 는 "관리자 권한으로 다시 실행").
- 관측 모드 상태 줄(`agent._status_tick`, 순수): 흐름 전이 1회(`흐름을 잡았습니다` / `끊겼습니다` / `다시 잡았습니다`), 미개통
  30초마다, 흐름은 잡았는데 육의전 무관측이면 10분마다. 첫 관측 뒤 멎는다. 5분 SEAssist 헬스 INFO 줄은 그대로(개발자용).
- 문서: `docs/DEPLOY.md` **신규** — 0단계 Pi 판 확인, Funnel(=hub/README 링크), exe 빌드, **지인 복붙 안내문**, G7 게이트 6개,
  운영 확인(G7 2차), 실패 증상표. HUB-PROTOCOL §0 표·§3-7·§7, hub/README(라우트·게이트 ping), README·AGENTS·PLAN(PR-Y5 절).
- 검증: 루트 **228 passed**(신규 29: selftest 18 · ping/status 해석 5 · 상태 줄 6) · hub **87 passed**(신규 5) ·
  진짜 허브 프로세스 2리스너 상대 **종단 스모크 12/12**(공개 stats 403 → 잘못된 토큰 401 → 등록 → ping 200 id 일치 →
  자가진단 rc 0 → `devices.py revoke` → ping 403 → 자가진단 rc 1 + 사유 → 관리 시크릿 ping 200) ·
  실기기 비승격 `--selftest`(거상 3개 검출·아이템 표 4,001행·권한/Npcap X·허브 미설정 경고, rc 1) · `--check` rc 0(벤더 무변경).
- **`/code-review 13 high` 8건 전부 반영(2026-09-23)**: ① `_capturing` = 핸들 + 엔진 헬스 `tracked_flows > 0` — 핸들은 거상을
  꺼도 `stop()`·읽기 오류에서만 닫혀 "끊겼습니다" 가 영영 안 떴다(하우스키핑은 FLOW_MISS_LIMIT 뒤 흐름을 0 으로), ② `Probes.env`
  기본 **None = os.environ**, `{}` = 격리 — 빌드 PC 에 `YUKTRACKER_HUB_URL` 이 있으면 `build.bat` 의 테스트 단계가 깨지던 것,
  `_url_origin` 도 같은 표를 본다, ③ "허브 도달" 은 200 이 아니라 `is_hub`(`service == yuktracker-hub`) — 오타 주소·포털 HTML 의
  200 이 "옛 판" 안내로 이어져 관리자가 Pi 를 재배포하러 가던 경로 차단(`허브가 아닙니다` 줄), ④ 거상 프로세스 0 이면
  `probe_capture` 가 15초를 기다리지 않는다, ⑤ 틱의 첫 줄에서 "캡처 시작" 문구 제거(엔진 상태 줄과 중복) + 루프 진입 때 이미
  흐름이면 seed 해서 수집 모드에서 재알림 없음, ⑥ `--selftest` 에 `--invite-code`/`--device-label`/`--capture` 를 주면 parser
  error(안내문도 "--selftest 없이"), ⑦ 인증서·미도달 문장은 `_describe_transport` 한 곳, ⑧ 엔진 시작 실패는 Npcap 사유 토큰
  없이(빈 토큰) 돌려주고 모르는 사유의 fallback 도 "Npcap 을 설치하라" 가 아니다. 루트 **240 passed**(신규 12) · hub 87.
- **Pi 판 확인 결과(2026-09-23)**: 떠 있는 컨테이너는 09-22 11:23 빌드 = **PR #10 이전 판**(compose 8800 만, `devices.py` 없음,
  `invite_code` 없음, `register`·`ping` 이 `401 unauthorized` — 옛 전역 Bearer 미들웨어). DEPLOY §0 표에 이 응답 행을 추가했다.
  **Funnel 은 443·8443 이 이미 같은 Pi 의 다른 서비스에 걸려 있다** → 사용자 결정: 공개 리스너는 **10000 번**
  (`sudo tailscale funnel --bg --https=10000 8801`, 주소 `https://<pi-node>.<tailnet>.ts.net:10000`). hub/README·DEPLOY·HUB-PROTOCOL·
  PLAN·`gen_build_config.py` 의 443 전제를 이 형태로 고쳤다(관측기는 주소를 통째로 받으므로 코드 무변경).
- **Pi 재배포·Funnel 개통 완료(2026-09-23 11:06, PR #13 머지 뒤)**: hub/ 재복사 → `config.json` 에 `invite_code` 병합(secret 유지) →
  `docker compose up -d --build`(8800 + 127.0.0.1:8801) → `register` 401 `bad_invite`·`devices.py` 동작 → `funnel --bg --https=10000 8801` →
  공개 URL 게이트 전부 통과(GET / 200 · 더미 Bearer stats 403 `not_public` · bad invite 401 · GATE 등록 → ping 200 → revoke → 403
  `device_revoked`, 공개 요청 XFF 경고 0). 폰 LTE 재확인은 사용자.
- **첫 실빌드에서 드러난 결함(2026-09-23, PR #14)**: DEPLOY §2 대로 `YUKTRACKER_HUB_URL` 을 켜고 `build.bat` 을 돌리면 테스트 단계가
  4건 깨진다 — 환경변수와 이전 빌드의 `_build_config.py`(`BUILD_HUB_URL`)가 "허브 주소 없음" 전제 테스트(자가진단 SKIP·관측만·등록
  프롬프트 없음 → stdin 을 먹어 콘솔 훅 테스트까지 흐름이 바뀜)에 샜다. `tests/conftest.py` autouse 픽스처가 둘 다 지운다.
- 남은 것: exe 전달 → 실기기 G7.

## 현재 상태

- 레포: https://github.com/helperjby/GS-trade-tracker (**public** — 2026-09-22 공개. 기기명·Pi 주소·사용자명·
  비공개 레포 경로/브랜치/링크를 문서·PR·커밋 메시지에 적지 않는다). `main` = 부트스트랩(문서만), **PR #1**
  (https://github.com/helperjby/GS-trade-tracker/pull/1, 브랜치 `feat/y1-capture-mode`) 가 코드 전부(28 파일). 이후 변경은 이 레포의 PR 로.
- **공개 정리(PR #9)의 남은 노출 — 작업 트리만 고쳤다**: 기기명·Pi 사용자명/LAN·tailnet 주소·비공개 레포 PR 링크는
  ① **git 이력**(`git show <구커밋>:hub/README.md` 로 그대로 읽힌다), ② **머지된 PR #1~#4 본문**(기기명·비공개 레포 링크)에
  아직 남아 있다. 둘 다 파일 수정으로는 지워지지 않는다 — ①은 이력 재작성(`git filter-repo` + force push, 협업자 합의 필요),
  ②는 `gh pr edit` 로 본문 교체(단 GitHub 은 본문 편집 이력도 보존한다). 노출된 값은 시크릿이 아니라 주소·이름이므로
  회전 대상은 없지만, "공개 레포에서 제거됐다"고 가정하지 않는다. 벤더 사본 잔여는 아래 벤더 절 참조.

- 사용자 결정(2026-09-21): 소비자 봇 = 미루봇-IRIS, 허브 = SEAssist 대시보드 서버(Pi) 확장(→ 09-22 대체), 수집기 =
  **SEAssist GUI 가 아닌 별도 경량 프로그램(이 프로젝트, 별도 실행파일)**, 육의전 opcode 는 모름 → 발굴부터.
- **사용자 결정(2026-09-22)**: ① SEAssist 레포(비공개)에는 더 이상 머지하지 않는다 — 육의전 트랙은
  전부 이 레포에서. ② 열려 있는 SEAssist PR #306(PR-Y2)도 머지하지 않고 이 레포로 **이식**(PR-Y2'). 이식 원본 = 비공개 레포의
  로컬 worktree(경로·브랜치·커밋은 레포에 적지 않는다, 커밋 6개) — #306 닫기는 사용자,
  worktree 는 이식이 끝날 때까지 보존. ③ 허브 = SEAssist dashboard 확장이 아니라 **이 레포의 독립 서버 `hub/`**(aiohttp+sqlite, Pi 별도
  컨테이너·포트 8800). ④ 벤더 사본은 SEAssist main **b54cb73**(= #303·#304·#305 머지 상태; 벤더 모듈 내용은 62e33db 와 동일) 시점에
  동결 — 이후 변경(프레이머 확장·파서)은 이 레포 소유 코드로, 방식(패치 계층 vs 소유 전환)은 PR-Y2' 계획에서. ⑤ 비공개 레포의
  고정 아카이브에 9창을 복사하는 "머지 직전 절차"는 폐기(#306 미머지). 지난 세션의 임시 root
  `%TEMP%\market-y2-corpus`(45창)·`%TEMP%\market-y2-review`(11창)는 이식 검증용으로 남겨 둔다.
- SEAssist 레포 쪽 선행 변경 = **PR #303**(비공개 레포, 커밋 62e33db, 머지됨): 경로 함수를
  `src/core/ledger_paths.py` 로 분리(wordinput_runner 재수출), `packet_state_source` 의 numpy/PIL
  지연 import, `admin.relaunch_as_admin(module=)` — 벤더 사본이 cv2·genai 없이 돌기 위한 것.
- 이 프로젝트: `src/yuktracker/`(cli·agent·game_processes·item_names·paths) + 벤더 사본 + 테스트 + `build.bat`/`run_dev.bat` + **`hub/`**(허브 서버).
  관측기는 수집 모드만 있다(파서·업로드는 PR-Y2'·PR-Y1b).
- 벤더 사본은 SEAssist 62e33db 와 일치(`VENDOR.json`, `--check` 통과). 비승격 스모크(`--no-elevate`)는 rc 2(관리자 아님) 로 정상 종료.
  `--check` 는 로컬 체크아웃이 62e33db/b54cb73 벤더 모듈과 같을 때만 통과한다 —
  09-21 현재 비공개 레포 본체는 다른 브랜치라 "불일치 6건" 이 뜨지만 벤더 사본은 무변경(`tests/test_vendor.py` 해시 핀 green).
  ~~벤더 사본의 실기기명 잔여~~ → **해소(2026-09-22, PR-Y2')**: 소유 전환으로 고칠 수 있게 되어 `gersang_protocol.py` 주석의
  실기기명 5건과 `packet_discovery_ledger.py` 의 운영 대수 서술을 지웠다(`src/` 에 0건). git 이력과 머지된 PR #1~#4 본문의
  잔여는 그대로다(위 항목).
- **실기기 첫 검증 완료(2026-09-21, 실기기 1대, Windows Terminal + 한글 IME)**: `run_dev.bat` 로 창 3개
  (`<OneDrive>\SEAssist\wordinput_review\<디바이스>\packet_discovery\` — `<디바이스>` 폴더명 = 그 PC 의 기기명,
  `ledger_paths._device_name`), 마지막 창(18:08, 547세그, 유실 0, writer 오류 0)에
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
  미루봇(PR-Y4)이 이 값을 쓴다(09-22 오전 계획의 "관측기 `hub_secret` 공유"는 같은 날 오후 폐기 — 관측기는 기기 토큰, 위 "허브 공개 업로드" 절).
  확인: 컨테이너 안·LAN `<pi-lan-ip>:8800`·tailnet `<pi-tailnet-ip>:8800` 에서 `GET /` 200,
  `stats` 인증 200(전부 0·devices []), 무인증 401. **`/mnt/dashdata` 는 이 Pi 에 없다**(루트가 SSD `/dev/sda2` 로 이전, 대시보드도 `~/dashboard/data`)
  → hub/README 의 데이터 폴더 예시를 `./data` 기본으로 정정. 갱신 절차 = hub/ 재복사 → `docker compose up -d --build`(DB 는 볼륨이라 유지).
  PR #5 머지 뒤 #6 base 는 GitHub 이 자동으로 `main` 으로 바꿨다(브랜치 자동 삭제).

## 다음 행동

(2026-09-23 저녁 정리 — PR #5·#6·PR-Y2'·PR-Y1b·PR-Y5·실기기 G7·PR #15·#16·#17 은 전부 완료, 위 절들.)

1. **허브 소멸 추정 PR**(`feat/hub-listing-expiry-20260923`, 위 절) → `/code-review` → 머지 → Pi 재배포(`hub/` 재복사 → `docker compose up -d --build`,
   DB 는 볼륨 유지·스키마 무변경; `config.json` 에 `listing_expiry_days` 는 없어도 기본 2, **단 예시 복사본의 `search_max_age_sec: 86400` 은
   259200 으로 고치거나 키를 지운다** — 안 고치면 기동 경고 + 24h 나이 필터가 살아 있는 행을 먼저 숨긴다) → `stats` 에 `live_listings`·`expiry_days`·`fresh_sec 259200` 확인.
2. 지인 슬롯 3~7 exe 전달(`docs/DEPLOY.md`, 안내문) · 선택: LTE 밖 경로 재확인.
3. 실기기 Step 0 잔여 캡처: 라벨 5열 `@45` 검정(H-2609-11 → B 면 허브 행별 일수 후속) · 용병 탭 열람 · 수량 ≥65,536·타 PC 세션.
4. PR-Y4 미루봇(미루봇-IRIS 레포): `http://127.0.0.1:8800` + 관리 시크릿, HUB-PROTOCOL §3-2/§3-3 — 표시에 소멸 칸(`expires_from/by_ts`), `Lv.` 보류.
