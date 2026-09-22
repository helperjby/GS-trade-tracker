# 육의전 시세트래킹 (YukTracker)

거상(Gersang) 클라이언트가 서버에서 **받는** 패킷을 읽어 육의전(유저 거래소) 목록을 모으고, 여러 사용자의 관측을
허브 서버에 합쳐 챗봇이 아이템 이름으로 시세를 검색·알림할 수 있게 하는 프로젝트입니다.

> ⚠️ 게임 운영사의 정책에 따라 패킷 관측·자동화 도구 사용은 제재 대상이 될 수 있습니다. 관측기는 게임에 어떤 입력도
> 보내지 않는 읽기 전용 스니퍼이지만, 사용에 따른 책임은 사용자에게 있습니다.

## 구성

| 구성 요소 | 위치 | 역할 |
|---|---|---|
| 관측기 | `src/yuktracker/` | Windows 콘솔 프로그램(관리자 권한, Npcap 필요). 실행 중인 거상 프로세스를 찾아 s2c 패킷을 관측하고, 클라이언트 리소스에서 아이템 id→이름 표를 읽어 목록에 이름을 붙인다. 런타임 의존성 없음(stdlib + ctypes), PyInstaller 단일 exe |
| 허브 | `hub/` | aiohttp + SQLite 독립 서버(Docker). 관측기가 올린 목록을 저장·중복 제거·등록 단위 최신 상태로 갱신하고 검색·증분 조회·통계 API 를 제공 |
| 소비자 | 별도 프로젝트 | 챗봇이 허브 API 로 `!육의전 <아이템>` 검색·알림 |

관측기 ↔ 허브 ↔ 봇 사이의 계약(엔드포인트·필드·정규화·신선도)은 [docs/HUB-PROTOCOL.md](docs/HUB-PROTOCOL.md),
설계와 단계는 [docs/PLAN.md](docs/PLAN.md).

## 현재 상태

- 관측기: 육의전 목록 관측 → 아이템 이름 해석 → 로컬 스풀 → **허브 업로드**까지 동작한다(첫 실행에 초대 코드로 등록).
  발굴용 수집 모드(`--capture`)와 아이템 표 추출 도구도 그대로 있다.
- 허브: API·저장 계층·테스트·Docker 배포 완료.

## 실행

```
run_dev.bat                               # 관측기 소스 실행(관리자 승격) — 관측·업로드
run_dev.bat --invite-code CODE            # 첫 실행: 초대 코드로 기기 등록(코드는 저장하지 않는다)
run_dev.bat --capture 5                   # 관측 + 5분 발굴 수집 창
build.bat                                 # 출처 선언 검사 → 테스트 → PyInstaller → dist\YukTracker.exe
python tools\dump_item_names.py --check   # 클라이언트의 아이템 id→이름 표 추출 확인
python tools\market_probe.py --root DIR   # 수집한 창을 재생해 육의전 페이지 판독(읽기 전용)
```

배포용 exe 는 허브 주소를 빌드 때 받는다(공개 레포 소스에 주소를 두지 않는다):

```
set YUKTRACKER_HUB_URL=https://<허브 공개 주소>
build.bat
```

받는 사람은 `YukTracker.exe` 를 관리자 권한으로 실행하고 **초대 코드만** 입력하면 된다 —
기기 토큰·기기 id 는 `%APPDATA%\YukTracker\config.json` 에 저장된다(초대 코드는 저장하지 않는다).

허브 실행·배포(Docker compose)와 API 예시는 [hub/README.md](hub/README.md).

## 개발

```
python -m pytest                          # 관측기 테스트(설치 없이 돈다)
python -X utf8 -m pytest hub/tests -q     # 허브 테스트 (pip install -r hub/requirements.txt)
```

- Python 3.11+. 관측기는 런타임 의존성 0, 허브는 `aiohttp` 하나.
- `src/yuktracker/seassist/` 는 같은 작성자의 다른 프로젝트(SEAssist)에서 온 패킷 코어이고 **지금은 이 레포가 소유**한다 —
  `VENDOR.json` 에 출처 커밋과 그 시점의 해시가 있고, 달라진 파일은 사유와 함께 선언한다(테스트가 선언을 검사).
- 실데이터(캡처 파일·판매자명·추출한 아이템 표·허브 DB·시크릿)는 레포에 넣지 않는다. 테스트는 합성 데이터만 쓴다.
  실기기명·호스트명, Pi 주소·사용자명, 로컬 절대 경로도 같은 규칙 — 문서·PR 본문·커밋 메시지에 적지 않는다.

## 구조

```
src/yuktracker/   cli · agent(수집) · game_processes · item_names · paths · seassist/(벤더 사본)
hub/              server.py · db.py · Dockerfile · docker-compose.yml · tests/
tools/            dump_item_names.py · market_probe.py · sync_seassist_core.py · console_input_probe.py
tests/            관측기 테스트
docs/             PLAN.md · HUB-PROTOCOL.md · PACKET-MARKET.md · SESSION_STATE.md
```
