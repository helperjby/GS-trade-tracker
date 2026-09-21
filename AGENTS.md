# Project: 육의전 시세트래킹 (YukTracker)

## 한 줄

거상 클라이언트의 s2c 패킷에서 육의전 목록을 관측해 공유 저장소로 보내는 **경량 관측기**. 프로토콜의
정본과 발굴 절차는 SEAssist 레포(`C:\dev\gersang`, `docs/PACKET-PROCESS.md`)에 있고, 이 프로젝트는 그
코어를 벤더 사본으로 쓴다.

## 먼저 읽을 것

1. `README.md` — 지금 되는 것·수집 절차
2. `docs/PLAN.md` — 전체 설계와 PR 순서(Step 0/1 → Y1 → Y2 → Y3 → Y1b → Y4)
3. `docs/SESSION_STATE.md` — 현재 상태·다음 행동
4. SEAssist `docs/PACKET-PROCESS.md` §1~§3 — 게이트 사다리·등급·가설 레지스트리(H-2609-07)

## 진실의 원천 순서

1. 현재 세션의 사용자 지시
2. `docs/PLAN.md` (승인된 계획)
3. SEAssist `docs/PACKET-*.md` (프로토콜 사실·게이트) — 여기와 어긋나면 SEAssist 쪽이 정본
4. 이 레포의 코드·테스트

## 규칙

- **벤더 사본(`src/yuktracker/seassist/`)을 손으로 고치지 않는다.** 필요한 변경은 SEAssist 레포에 PR 로
  넣고(코퍼스 테스트 포함) `python tools/sync_seassist_core.py` 로 가져온다. `config.py` 만 예외(심).
- **새 opcode·필드 주장은 SEAssist PACKET-PROCESS §3 레지스트리 행 없이는 코드에 넣지 않는다.** 등급 D 는
  파서·업로드에 못 들어간다. 이 트랙의 런타임은 관측 전용이라 목표 등급은 B.
- 런타임 의존성 0(stdlib + ctypes). `tests/test_vendor.py` 의 import 경계가 막는다 — cv2·numpy·PIL·
  pywin32·requests 를 추가하지 않는다.
- 원시 캡처(`window_*.jsonl`)·판매자명 등 실데이터를 레포에 넣지 않는다. 테스트 픽스처는 합성으로.
- 게임에 입력을 보내는 코드는 이 프로젝트에 없다(읽기 전용). 이 경계를 넘는 기능은 여기 두지 않는다.
- 문서: 상태 변화는 `docs/SESSION_STATE.md`, 설계 변경은 `docs/PLAN.md` 에 날짜와 함께.

## 명령

```powershell
python -m pytest
python tools\sync_seassist_core.py --check
python tools\sync_seassist_core.py [--source <SEAssist 체크아웃>]
run_dev.bat [--capture N | --capture 0]
build.bat
```
