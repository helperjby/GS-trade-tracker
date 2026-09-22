"""RateLimiter — 고정 창 · retry_after · blocked 는 세지 않음 · 만료 키 정리."""
from __future__ import annotations

import server as server_mod


def _limiter(limit, window, t):
    return server_mod.RateLimiter(limit, window, clock=lambda: t[0])


def test_window_retry_after_and_keys():
    t = [1000.0]
    rl = _limiter(2, 60, t)
    assert rl.blocked("a") == 0                       # 처음 보는 키
    assert rl.hit("a") == 0 and rl.hit("a") == 0
    assert rl.hit("a") == 60 and rl.blocked("a") == 60   # 한도 — 창 끝까지 60초
    t[0] = 1030.0
    assert rl.hit("a") == 30 and rl.blocked("a") == 30   # 거부된 시도는 창을 늘리지 않는다
    assert rl.hit("b") == 0 and rl.blocked("b") == 0     # 다른 키는 별개
    t[0] = 1059.5
    assert rl.hit("a") == 1                              # 올림, 최소 1
    t[0] = 1060.0
    assert rl.blocked("a") == 0 and rl.hit("a") == 0     # 창 리셋


def test_blocked_does_not_count():
    t = [0.0]
    rl = _limiter(1, 10, t)
    for _ in range(5):
        assert rl.blocked("a") == 0
    assert rl.hit("a") == 0 and rl.hit("a") == 10


def test_sweep_drops_expired_keys_once_per_window():
    t = [0.0]
    rl = _limiter(1, 10, t)
    for i in range(100):
        rl.hit(f"k{i}")
    assert len(rl._hits) == 100
    t[0] = 9.0
    rl.hit("x")
    assert len(rl._hits) == 101                          # 창 안 — 정리 안 함
    t[0] = 10.0
    rl.hit("new")
    assert set(rl._hits) == {"new", "x"}                 # 만료(창 10 경과)만 제거 — x 는 9 에 시작, 아직 살아 있다
    t[0] = 25.0
    rl.hit("last")
    assert set(rl._hits) == {"last"}
