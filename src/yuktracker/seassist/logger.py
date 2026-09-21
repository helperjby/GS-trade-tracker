from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .log_history import LogHistory

_LOGGER_NAME = "gersang_auto_eating"
_initialized = False

# ERROR 파일 싱크 opt-in 플래그 — 프로덕션 엔트리(app.run)만 켠다. 테스트가
# 러너 크래시 경로를 실제 로거로 태워도(RunnerCrashAlertTest 등) 리포 log/ 에
# 노이즈 파일을 쓰지 않게 하는 가드.
_error_file_sink_enabled = False


def enable_error_file_sink() -> None:
    global _error_file_sink_enabled
    _error_file_sink_enabled = True


def disable_error_file_sink() -> None:
    """테스트 정리용 — 프로덕션에선 호출할 일 없음."""
    global _error_file_sink_enabled
    _error_file_sink_enabled = False


def _crash_log_dir() -> Path:
    """크래시 덤프·ERROR 파일 전용 디렉터리 — 사용자가 수동 로그 저장에 쓰는
    프로젝트 루트의 log/ (frozen 시 exe 옆 log/).

    %APPDATA% 데이터 폴더가 아니라 사용자가 이미 들여다보는 위치에 남긴다 —
    2026-07-14 사고에서 크래시 원인이 프로세스 종료와 함께 소실된 후속.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parents[2]
    p = base / "log"
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_crash_dump(exc: BaseException) -> Path | None:
    """비정상 종료(예외의 mainloop 탈출 등) 순간의 LogHistory 전체 + traceback 을
    log/log_YYYYMMDD_HHMMSS_crash.txt 로 저장하고 경로를 반환 (실패 시 None).

    LogHistory 는 메모리 전용이라 프로세스가 죽으면 크래시 라인까지 함께 증발
    (2026-07-14 사고 — 사용자가 본 "Wordinput 러너 크래시" 텍스트 소실). 포맷은
    LogViewerDialog 수동 저장과 동일해 기존 덤프와 나란히 읽힌다. 정상 종료
    경로에서는 호출되지 않으므로 평상시 파일 생성은 0 (Issue #53 취지 유지).
    어떤 경우에도 예외를 전파하지 않는다.
    """
    try:
        path = _crash_log_dir() / f"log_{datetime.now():%Y%m%d_%H%M%S}_crash.txt"
        with open(path, "w", encoding="utf-8") as f:
            for line in LogHistory.instance().iter_format():
                f.write(line + "\n")
            f.write("\n===== crash traceback =====\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        return path
    except Exception:
        return None


class LogHistoryHandler(logging.Handler):
    """WARNING+ 로그 레코드를 LogHistory(인앱 가시 로그)로 라우팅하는 브리지.

    logger.py 는 파일 핸들러를 두지 않으므로(Issue #53 PR-F), 그동안 러너들의
    `log.warning/error(...)` 는 어디에도 보이지 않았다 — 2026-06-16 monster ~1h44m
    무보고 동결을 키운 한 요인. 이 핸들러가 핵심 WARN/ERROR 를 LogHistory 에 적재해
    LogViewer·LAN 로그서버에서 보이게 한다(파일 재도입 없이, Fix 3 동결 후속).

    emit 은 어떤 경우에도 예외를 전파하지 않는다 — 로깅이 앱(러너 스레드)을 죽이면 안 됨.

    ERROR 레벨(러너 크래시 log.exception, dead session finalized 등)만은 추가로
    log/error_YYYYMMDD.txt 에 어펜드 — LogHistory 는 프로세스와 함께 증발하므로
    (2026-07-14 사고) 사후 판별용 영구 사본. WARN 은 파일 제외(스팸 방지, Issue #53
    취지 유지 — 평상시 파일 생성 0).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            severity = "error" if record.levelno >= logging.ERROR else "detection"
            LogHistory.instance().append(f"⚠ {record.getMessage()}", severity=severity)
        except Exception:
            pass
        if _error_file_sink_enabled and record.levelno >= logging.ERROR:
            try:
                path = _crash_log_dir() / f"error_{datetime.now():%Y%m%d}.txt"
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%H:%M:%S} {record.getMessage()}\n")
                    if record.exc_info and record.exc_info[0] is not None:
                        f.write("".join(traceback.format_exception(*record.exc_info)))
            except Exception:
                pass


def format_exc_brief(e: BaseException) -> str:
    """예외 1건을 '타입: 메시지 @ 파일:라인' 한 줄로 — 러너 크래시 상태줄용.

    전체 traceback 은 log.exception 몫. 상태바/LogHistory 에는 원인 특정에 필요한
    최소 정보(예외 타입 + 최심부 발생 위치)만 싣는다 (2026-07-13 사고 후속 — 크래시
    원인이 LogHistory 에만 남는데 그마저 타입/위치가 없어 사후 판별이 안 됐다).
    """
    loc = ""
    tb = e.__traceback__
    if tb is not None:
        frames = traceback.extract_tb(tb)
        if frames:
            last = frames[-1]
            loc = f" @ {Path(last.filename).name}:{last.lineno}"
    return f"{type(e).__name__}: {e}{loc}"


def get_logger() -> logging.Logger:
    # 종료 시 자동 파일 저장은 사용자 요청으로 제거됨 (Issue #53 PR-F).
    # 필요 시 LogViewer 의 [저장…] 버튼으로 수동 저장.
    # 핵심 WARN/ERROR 는 LogHistoryHandler 로 인앱 가시화(Fix 3) — 파일 재도입 아님.
    global _initialized
    logger = logging.getLogger(_LOGGER_NAME)
    if _initialized:
        return logger

    # WARNING — 유일 소비자(LogHistoryHandler)가 WARNING+ 만 emit 하므로, INFO
    # 레벨이면 러너 핫루프의 모든 log.info 가 LogRecord 생성 + findCaller 스택
    # 워크 비용만 내고 emit 에서 버려진다(2026-07-20 전체 분석 M3). WARNING 으로
    # isEnabledFor 단락 — log.info 호출부는 그대로 두되 근사-무비용이 된다.
    # 불변식: INFO 로 되돌릴 땐 반드시 INFO 를 소비하는 핸들러와 함께 (없으면
    # 같은 블랙홀 재현). LAN 로그서버는 LogHistory.subscribe 경유 — logging
    # 레코드 소비자 아님.
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    handler = LogHistoryHandler()
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    _initialized = True
    return logger
