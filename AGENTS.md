# Project: 육의전 시세트래킹 (YukTracker)

## 한 줄

거상 클라이언트의 s2c 패킷에서 육의전 목록을 관측해 공유 저장소로 보내는 **경량 관측기**(`src/yuktracker/`)와, 그것을
모아 미루봇이 검색하는 **시세 허브**(`hub/`, Pi 독립 서버). 패킷 코어는 SEAssist 레포(비공개, 위치는 환경변수
`SEASSIST_REPO`)의 벤더 사본을 쓰되 **2026-09-22 부터 SEAssist 레포에는 변경을 내지 않는다** — 육의전 트랙의
코드·문서는 전부 여기.

> **이 레포는 공개다(2026-09-22~).** 실기기명·호스트명, Pi 사용자명·LAN/tailnet 주소, 로컬 절대 경로
> (`C:\Users\…`·`C:\dev\…`·`NN. <폴더>`), 비공개 레포의 링크·브랜치·worktree 경로는 코드·문서·PR 본문·커밋
> 메시지 어디에도 적지 않는다. 자리표시자(`<user>`·`<pi-lan-ip>`·`<디바이스>`)나 환경변수로 쓴다.

## 먼저 읽을 것

1. `README.md` — 지금 되는 것·수집 절차
2. `docs/PLAN.md` — 전체 설계와 PR 순서(Step 0/1 → Y1 → Y2(SEAssist, 미머지) → Y2b → **Y3 허브** → Y2' 이식 → Y1b → Y4)
3. `docs/SESSION_STATE.md` — 현재 상태·다음 행동
4. `docs/HUB-PROTOCOL.md` — 관측기 ↔ 허브 ↔ 미루봇 계약(엔드포인트·필드·정규화·신선도)의 정본
5. `docs/PACKET-MARKET.md` — 육의전 프로토콜 판정의 정본(필드 표·가설 레지스트리 H-2609-07~11·재현·PATCH-RECHECK).
   게이트 사다리·등급 정의 자체는 SEAssist `docs/PACKET-PROCESS.md` §1~§3 **동결 시점(main b54cb73) 참조**.

## 진실의 원천 순서

1. 현재 세션의 사용자 지시
2. `docs/PLAN.md` (승인된 계획) · `docs/HUB-PROTOCOL.md` (허브 계약)
3. SEAssist `docs/PACKET-*.md` (프로토콜 사실·게이트, 동결 시점 참조) → 이식 뒤에는 이 레포 `docs/PACKET-MARKET.md`
4. 이 레포의 코드·테스트

## 규칙

- **SEAssist 레포에 PR 을 내지 않는다(2026-09-22).** 패킷 코어(`src/yuktracker/seassist/`)는 **이 레포 소유**다 —
  PR-Y2' 에서 전량 소유 전환했고 복사 경로는 없다(프레이머 확장·엔진 콜백이 `StreamFramer.feed()` 루프 안에 들어가야 해서).
  `VENDOR.json` 이 출처 커밋(b54cb73)과 그 시점의 해시를 기록하고, 내용이 달라진 파일은 `diverged` 에 **사유와 함께 선언**한다 —
  선언 없는 변경은 `tools/sync_seassist_core.py --check` 와 `tests/test_vendor.py` 가 잡는다(상류 비교는 `--upstream`, 정보용).
  `config.py` 는 이 프로젝트 소유 심.
- **새 opcode·필드 주장은 가설 레지스트리 행(SEAssist PACKET-PROCESS §3 형식) 없이는 코드에 넣지 않는다.** 등급 D 는
  파서·업로드에 못 들어간다. 이 트랙의 런타임은 관측 전용이라 목표 등급은 B. 레지스트리의 후속 기록은 이 레포 `docs/PACKET-MARKET.md`.
- 관측기(`src/yuktracker/`)는 런타임 의존성 0(stdlib + ctypes). `tests/test_vendor.py` 의 import 경계가 막는다 — cv2·numpy·PIL·
  pywin32·requests 를 추가하지 않는다. **허브(`hub/`)는 별개 배포 단위**: `aiohttp` 하나 + sqlite3, `src/` 를 import 하지 않는다
  (Docker 빌드 컨텍스트 = `hub/`). 허브 스키마는 `CREATE … IF NOT EXISTS` 로 additive 하게만(배포 DB 무마이그레이션), API 는 additive-only.
- 원시 캡처(`window_*.jsonl`)·판매자명 등 실데이터를 레포에 넣지 않는다. **실기기명·Pi 주소·사용자명도 실데이터로 본다**
  (`device_id` 는 기본값이 그 PC 의 hostname 이다 — `ledger_paths._device_name`). 테스트 픽스처·문서 예시는 합성으로
  (`판매자A`, `DEV-1`). 허브 시크릿·DB(`hub/config.json`, `hub/data/`, `.env`, `*.db`)도 `.gitignore` — 예시 파일(`.example`)만 커밋.
- 게임에 입력을 보내는 코드는 이 프로젝트에 없다(읽기 전용). 이 경계를 넘는 기능은 여기 두지 않는다.
- 클라 데이터 파일(`gersang.gcs` 등)은 **읽기 전용**으로만 연다(프로세스·메모리·네트워크 무접촉). 추출한
  아이템 표·캐시는 레포 밖(`%APPDATA%\YukTracker`, `--out` 은 `.gitignore`)에 두고 재배포하지 않는다.
  테스트는 합성 아카이브만 쓴다(실제 클라 경로는 항상 명시 주입).
- 문서: 상태 변화는 `docs/SESSION_STATE.md`, 설계 변경은 `docs/PLAN.md` 에 날짜와 함께, 허브 계약 변경은 `docs/HUB-PROTOCOL.md`(판·이력).

## 명령

```powershell
python -m pytest                                   # 관측기 테스트 (tests/)
python -X utf8 -m pytest hub/tests -q              # 허브 테스트 (aiohttp 필요: pip install -r hub/requirements.txt)
python tools\sync_seassist_core.py --check         # 출처 대비 차이 == VENDOR.json diverged 선언 ? (rc 1 = 불일치)
python tools\sync_seassist_core.py --upstream      # 상류 체크아웃과 파일별 비교(정보용, %SEASSIST_REPO% 없으면 rc 2)
run_dev.bat [--invite-code CODE] [--capture N]     # 관측·업로드(+ 발굴 수집 창)
python tools\market_probe.py --root <패킷 루트>     # 수집 창 오프라인 재생(라이브 vs 프로브 대조)
build.bat                                          # %YUKTRACKER_HUB_URL% 이 있으면 exe 에 주소를 주입
python hub\server.py --config hub\config.json      # 허브 로컬 실행 (배포는 hub/README.md)
```
