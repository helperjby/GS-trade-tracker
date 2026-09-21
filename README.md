# 육의전 시세트래킹 (YukTracker)

거상(Gersang) 클라이언트가 서버에서 받는 s2c 패킷을 관측해 **육의전(유저 거래소) 목록**을 모으는
경량 프로그램. 목표는 1~7명의 사용자가 육의전을 열 때마다 목록을 공유 저장소(대시보드 서버, Pi)에
합류시키고, 미루봇-IRIS(카카오톡)가 그것을 읽어 `!육의전 <아이템>` 검색·알람을 켜는 것이다.

이 폴더는 세 프로젝트를 잇는 **관측기(수집 클라이언트)** 만 담는다. 전체 설계·단계는
[docs/PLAN.md](docs/PLAN.md), 현재 상태는 [docs/SESSION_STATE.md](docs/SESSION_STATE.md).

Repository: https://github.com/helperjby/GS-trade-tracker (private) — 변경은 PR 로, CI 는 `pytest`(windows).

| 역할 | 어디 |
|---|---|
| 프로토콜 정본·발굴 도구·코퍼스 검사 | SEAssist 레포 `C:\dev\gersang` — `docs/PACKET-PROCESS.md`(게이트·가설 레지스트리 **H-2609-07**), `PACKET-TOOLS.md`, `scripts/packet_explore.py` |
| 관측기(이 프로젝트) | `src/yuktracker/` — SEAssist 패킷 코어의 벤더 사본(`seassist/`) + 프로세스 열거 + 콘솔 |
| 공유 저장소 | SEAssist 레포 `dashboard/`(Pi, aiohttp+sqlite) — market API 는 PR-Y3 |
| 소비자 봇 | `32. 미루봇-IRIS` Geosang GS-01 — PR-Y4 |

> ⚠️ 게임 운영사의 정책에 따라 패킷 관측·자동화 도구 사용은 제재 대상이 될 수 있다. 관측기는 게임에
> 어떤 입력도 보내지 않지만(읽기 전용 스니퍼), 사용 책임은 사용자에게 있다.

## 지금 할 수 있는 것 (PR-Y1 — 수집 모드)

육의전 opcode 는 아직 **모른다**. 첫 일은 발굴용 원시 캡처(`window_*.jsonl`)를 여러 PC·세션에서
모으는 것이고, 관측기는 그 캡처를 SEAssist GUI `[패킷 수집]` 과 **같은 형식·같은 폴더**에 남긴다
(매크로 세션 없이, 게임만 켜져 있으면 된다).

```
run_dev.bat                 # = python -m yuktracker --capture 5  (관리자 승격, 소스 실행)
run_dev.bat --capture 10    # 10분 창
run_dev.bat --capture 0     # 관측 대기 모드(창 없음) — 스니퍼가 붙는지만 확인
dist\YukTracker.exe         # build.bat 산출물(UAC 매니페스트 내장) — 인자 없이 두 번 클릭하면 관측 대기 모드
```

수집 절차(SEAssist `docs/PACKET-TOOLS.md` §1 요령과 같다):

1. 거상 클라이언트를 켜고 서버에 접속한 상태에서 관측기를 관리자로 실행 → `[패킷] 캡처 시작 — 서버 …`
   줄이 뜨면 스니퍼가 붙은 것. `[발굴] 패킷 수집 시작 — 최대 N분 → <폴더>` 가 창 파일 위치다.
2. **정지 30초 → 육의전 열기 → 잘 안 팔리는 고유한 아이템명 1개 검색 → 페이지 넘김 → 카테고리 열람 →
   빈 결과 검색 1회 → 닫기 → 정지 30초.** 각 동작 **직후** 콘솔에 짧은 한 줄(예: `육의전 열기`, `검색 소나무`,
   `2페이지`)을 치고 Enter — `market_manual` 라벨로 창 안에 남는다. `q` + Enter 로 종료(한글 상태의 `ㅂ` 도
   종료, 시간이 다 되면 자동 종료).
   - **라벨은 동작 직후에 짧게.** 분석 도구(SEAssist `mine_packet_discovery.py`)는 라벨 앞 3초까지만 거슬러
     보므로(`market_manual` 은 lookback 미등록), 목록 내용을 옮겨 적는 긴 메모는 짧은 라벨 **다음 줄**에 따로
     친다(실기기 09-21: 상세 메모가 원인 패킷보다 3~35초 늦었다). 분석 때 `--lead 40` 으로 넓힐 수는 있다.
   - 콘솔을 X 로 닫아도 창은 `window_end(console_close)` 로 마감되지만 `q` 가 정석이다. 한글이 잘려 들어가면
     (`[발굴] 라벨 #n 기록 — …` 확인 줄이 친 것과 다르면) 같은 줄을 다시 친다 — 원인 진단은 아래 개발 절의
     `tools\console_input_probe.py`.
3. 양성 창 ≥3(서로 다른 PC/세션) + 음성 창 ≥2(상점·창고처럼 목록 UI 는 열지만 육의전은 아닌 창)를
   모은다. 가격 65,536 이상·수량 1과 여러 개·용병(Lv)과 물품·긴 이름을 섞는다.
4. 분석은 SEAssist 레포에서: `python scripts/packet_explore.py --root <packet 폴더> grep "<아이템명>"`
   → `timeline` → `stats` → `sub` → `body` → `bg` (PLAN.md Step 1).

창 파일 위치: SEAssist 가 설치된 PC 는 그 설정(`%APPDATA%\SEAssist\settings.json` 의 `packet_data_dir`,
없으면 `<OneDrive>\SEAssist\wordinput_review`) 아래 `<디바이스>\packet_discovery\`. SEAssist 가 없는 PC 도
같은 규칙(OneDrive 있으면 그 아래, 없으면 실행 폴더) — 콘솔 첫 줄에 찍힌다. OneDrive 미연동 PC 는
파일을 직접 복사해야 한다(`<디바이스>` 폴더째).

## 개발

```powershell
python -m pytest                  # 설치 없이 돈다(tests/conftest.py 가 src 를 경로에 넣는다)
python tools\sync_seassist_core.py --check     # 벤더 사본 == SEAssist 소스 ?
python tools\sync_seassist_core.py             # SEAssist(C:\dev\gersang) 에서 재동기화 → VENDOR.json 갱신
build.bat                         # 드리프트 검사 → 테스트 → PyInstaller → dist\YukTracker.exe
python tools\console_input_probe.py   # 콘솔 한글 입력 진단 — 관측기와 같은 콘솔·같은 IME 에서 직접 타이핑
python tools\dump_item_names.py --check   # 아이템 id→이름 표(클라 gersang.gcs) 추출 + 라벨 앵커 8/8 대조 (실기기 게이트)
python tools\dump_item_names.py --search 봉인 · --ids 853,3506 · --ids-from probe.txt · --out item_names.json
```

- Python 3.11+, **런타임 의존성 0**(stdlib + ctypes). Npcap 은 시스템 설치(SEAssist `docs/INSTALL.md` §5).
- `src/yuktracker/seassist/` 는 손으로 고치지 않는다 — `tools/sync_seassist_core.py` 만이 바꾸고
  `VENDOR.json` 에 출처 커밋·해시를 남긴다(`tests/test_vendor.py` 가 핀). 예외는 `config.py`(이 프로젝트가
  소유하는 심)뿐.
- 프로토콜 판정·육의전 파서는 SEAssist 레포에 코퍼스 테스트와 함께 들어간 뒤(PR-Y2, `packet_market.py`) 동기화로 가져온다.
- **아이템 id → 이름 표**(2026-09-21 결정): 육의전 응답에는 아이템 **id** 만 있고 이름이 없다(SEAssist H-2609-08/10).
  이름은 클라 리소스 `gersang.gcs` 안의 `육의전 검색 기능 리스트`(4,001행)에서 뽑는다 — `src/yuktracker/item_names.py`
  (stdlib `zlib`, **읽기 전용**, 스트림 오프셋이 아니라 내용 마커로 찾고 파일 크기·mtime 이 바뀌면 자동 재스캔 —
  패치 추종). 클라 폴더는 실행 중 `gersang.exe` 의 경로 → `C:\AKInteractive\Gersang*` 순으로 찾고
  `--client-dir` / `%YUKTRACKER_CLIENT_DIR%` 로 지정할 수 있다. 캐시는 `%APPDATA%\YukTracker\item_names.json`
  (기기 로컬), `--out` 덤프는 레포 밖에 둔다(`.gitignore`) — 표를 재배포하지 않는다. 표시명은 선두 `[M]` 만 지우고
  (게임 UI 와 같음), 검색 키는 공백 제거 + casefold(대시보드·봇과 한 정의). 관측기는 PR-Y1b 에서 행마다
  `item_id` + `item_name`(미해석은 null + 카운터)을 함께 올린다.

## 구조

```
run_yuktracker.py        # PyInstaller 엔트리 / 개발 실행 (src 를 경로에)
run_dev.bat build.bat    # 관리자 소스 실행 / exe 빌드
src/yuktracker/
  cli.py                 # 인자 · UAC 승격 · run()
  agent.py               # PidIndexer · 엔진 배선 · 수집 창 · 콘솔 라벨
  game_processes.py      # Toolhelp32 → gersang.exe PID · QueryFullProcessImageNameW → 실행 경로 (pywin32 없이)
  item_names.py          # 클라 gersang.gcs → 아이템 id→이름 표 (순차 zlib 스캔·정규화·캐시·클라 폴더 탐색)
  paths.py               # %APPDATA%\YukTracker (캐시·스풀)
  seassist/              # SEAssist src/core 벤더 사본 (+ config.py 심, VENDOR.json)
tools/sync_seassist_core.py tools/dump_item_names.py
tests/                   # pytest — 엔진 스텁 + 진짜 recorder, 벤더 해시, import 경계, 합성 gcs 아카이브
docs/PLAN.md docs/SESSION_STATE.md
```

## 다음 단계 (요약 — 상세는 docs/PLAN.md)

Step 0 수집 → Step 1 분석(H-2609-07 필드 표) → PR-Y2 SEAssist 프로토콜·파서·코퍼스 → PR-Y3 대시보드
market API → PR-Y1b 관측 모드(파서 → 스풀 → 업로드) → 배포·실기기 게이트 → PR-Y4 미루봇 GS-01.
