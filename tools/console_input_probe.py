r"""콘솔 한글 입력 진단 — 같은 문장을 세 방식으로 읽어 어디서 글자가 빠지는지 본다.

배경(2026-09-21 실기기): 관측기 콘솔(Windows Terminal + 한글 IME)에 '악몽을 피우는 씨앗' 을 쳤는데 라벨에는
'을 는 앗' 이 남았다. 관측기의 `_Console` 은 `for line in sys.stdin`(TextIOWrapper → _WindowsConsoleIO →
ReadConsoleW 라인 모드 = cooked read)으로 읽는데, 이 경로는 줄 단위로 통째로 받으므로 유실은 콘솔 호스트의
cooked read + IME 쪽으로 보인다 — 확정은 여기서. ① 관측기와 같은 경로 ② `input()`(같은 cooked read)
③ `msvcrt.getwch()` raw 루프(라인 모드를 쓰지 않음) 세 방식으로 같은 문장을 받아 `repr` 로 찍고, 입력과
같은지 O/X 를 낸다. 살아남는 방식으로 `_Console` 을 바꾼다(③ 이면 `stdin.isatty()` 일 때만).

사용 — 관측기를 띄우는 것과 **같은 콘솔(같은 호스트·같은 IME)** 에서:

    python tools\console_input_probe.py            # 프롬프트마다 같은 문장을 한글로 직접 타이핑 + Enter
    conhost.exe python tools\console_input_probe.py   # 레거시 콘솔 호스트에서 비교

붙여넣기가 아니라 **타이핑**이어야 한다(IME 조합 경로가 다르다). 관리자 권한은 무관하다.
"""
from __future__ import annotations

import os
import sys

SENTENCE = "악몽을 피우는 씨앗"


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def read_stdin_iter() -> str:
    """관측기 `_Console` 과 같은 경로."""
    for line in sys.stdin:
        return line.rstrip("\r\n")
    return ""


def read_input() -> str:
    return input()


def read_raw_getwch() -> str:
    """`msvcrt.getwch` raw 루프 — 콘솔 라인 모드를 쓰지 않는다. Enter 까지, 백스페이스 처리, 직접 에코."""
    import msvcrt

    chars: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(chars)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x08", "\x7f"):
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if ch in ("\x00", "\xe0"):  # 확장 키(방향키 등) — 뒤따르는 코드를 버린다
            msvcrt.getwch()
            continue
        chars.append(ch)
        sys.stdout.write(ch)
        sys.stdout.flush()


def main() -> int:
    _utf8_console()
    host = "Windows Terminal" if os.environ.get("WT_SESSION") else "conhost/기타"
    tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
    print(f"콘솔 한글 입력 진단 — 프롬프트마다 같은 문장을 한글로 직접 타이핑하고 Enter: {SENTENCE!r}")
    print(f"(호스트: {host}, python {sys.version.split()[0]}, stdin tty={tty}, platform={sys.platform})")
    if not tty:
        print("stdin 이 콘솔이 아니다(파이프/리다이렉트) — 콘솔에서 직접 실행할 것")
        return 2
    methods = [("sys.stdin 반복 (관측기 현행)", read_stdin_iter), ("input()", read_input)]
    if sys.platform == "win32":
        methods.append(("msvcrt.getwch raw 루프", read_raw_getwch))
    results: list[tuple[str, str]] = []
    for name, fn in methods:
        print(f"\n[{name}] > ", end="", flush=True)
        try:
            got = fn()
        except KeyboardInterrupt:
            print("\n중단")
            return 1
        results.append((name, got))
        mark = "O" if got == SENTENCE else "X"
        print(f"   → {got!r}  {mark} ({len(got)}자, 기대 {len(SENTENCE)}자)")
    print("\n요약")
    for name, got in results:
        print(f"  {'O' if got == SENTENCE else 'X'}  {name:<28} {got!r}")
    ok = [name for name, got in results if got == SENTENCE]
    if ok:
        print("\n살아남은 방식: " + ", ".join(ok))
    else:
        print("\n살아남은 방식 없음 — 콘솔 호스트(IME) 쪽 문제일 가능성이 크다. "
              "`conhost.exe python tools\\console_input_probe.py` 로 레거시 호스트에서 다시 비교할 것")
    return 0


if __name__ == "__main__":
    sys.exit(main())
