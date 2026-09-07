"""실기 없이 전체 시스템을 굴리기 위한 디바이스 구현.

Mock 으로 먼저 완성한 뒤 어댑터 교체로 실기에 연결한다. 그러려면 Mock 이
실기와 **같은 시간 구조**를 가져야 한다. 그래서 이 구현은 즉시 반환하지 않고
실제로 시간을 소비한다 (DECISIONS.md §15).

시리얼은 쓰지 않는다. 퍼밀 변환도 하지 않는다 — 보낼 곳이 없다.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Iterator, Mapping, Tuple

from server.config import Config
from server.device import (
    DeviceAdapter,
    DeviceBusyError,
    DeviceNotConnectedError,
    Pose,
)

__all__ = ["MockDevice"]

_MS_PER_SECOND = 1000.0


class MockDevice(DeviceAdapter):
    """메모리 안에서만 움직이는 손.

    `sleep` 을 주입하면 시간을 소비하지 않고 같은 순서로 진행시킬 수 있다.
    테스트는 항상 주입해서 쓴다.

    동작마다 세대 번호를 붙인다. `stop()` 이나 새 동작이 세대를 올리면, 이미
    돌고 있던 동작은 다음 틱에서 자기 세대가 지났음을 보고 조용히 물러난다.
    플래그 하나로 처리하면 정지 직후 시작된 동작을 이전 동작이 취소해 버린다.
    """

    def __init__(
        self,
        config: Config,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._lock = threading.Lock()
        self._connected = False
        self._busy = False
        self._generation = 0
        # 연결 전에는 자세랄 것이 없다. connect() 가 안전 자세로 채운다.
        self._flex: Dict[str, float] = dict(config.safe)

    # ----------------------------------------------------------------- #
    # 연결
    # ----------------------------------------------------------------- #

    def connect(self) -> None:
        with self._lock:
            self._connected = True
            self._busy = False
            self._generation += 1
            # 시작 상태는 안전 자세다. 중립이 아니다 (R3).
            self._flex = dict(self._config.safe)

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._busy = False
            self._generation += 1

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def current_flex(self) -> Mapping[str, float]:
        """지금 이 순간의 flex. 보간 중이면 중간값이 나온다.

        나중에 mockhand.html 시각화가 읽어 갈 값이다. 여기서는 조회만 제공하고
        웹소켓 연동은 하지 않는다.
        """
        with self._lock:
            return dict(self._flex)

    # ----------------------------------------------------------------- #
    # 동작
    # ----------------------------------------------------------------- #

    def move_to(self, pose: Pose, transition_ms: int, hold_ms: int) -> None:
        if not isinstance(pose, Pose):
            raise TypeError(
                f"move_to() takes a Pose, got {type(pose).__name__} ({pose!r}). "
                f"The device layer never receives letter names (R2)."
            )
        self._move(pose, transition_ms, hold_ms)

    def neutral(self) -> None:
        """중립 자세로 이동한다. safe() 를 호출하지 않는다 (R3)."""
        self._move(
            Pose.from_config(self._config, self._config.neutral),
            self._config.timing.transition_ms,
            hold_ms=0,
        )

    def safe(self) -> None:
        """안전 자세로 이동해 정착할 때까지 유지한다. neutral() 을 호출하지 않는다 (R3).

        도착 즉시 반환하면 상위 계층이 텐던이 정착하기 전에 다음 동작을 시작할
        수 있다. 안전 자세는 "쉬는" 자세이므로 실제로 쉬어야 한다
        (DECISIONS.md §17).
        """
        self._move(
            Pose.from_config(self._config, self._config.safe),
            self._config.timing.transition_ms,
            hold_ms=self._config.timing.hold_min_ms,
        )

    def stop(self) -> None:
        """즉시 정지하고 안전 자세로 만든다. 보간하지 않고, 기다리지 않는다."""
        with self._lock:
            self._require_connected_locked()
            self._generation += 1
            self._busy = False
            self._flex = dict(self._config.safe)

    # ----------------------------------------------------------------- #
    # 내부
    # ----------------------------------------------------------------- #

    def _require_connected_locked(self) -> None:
        if not self._connected:
            raise DeviceNotConnectedError(
                "device is not connected; call connect() first"
            )

    def _move(self, pose: Pose, transition_ms: int, hold_ms: int) -> None:
        if transition_ms < 0 or hold_ms < 0:
            raise ValueError(
                f"transition_ms={transition_ms}, hold_ms={hold_ms}; "
                f"expected both to be zero or positive"
            )

        with self._lock:
            self._require_connected_locked()
            if self._busy:
                raise DeviceBusyError(
                    "device is already moving; wait for the current motion to "
                    "finish before requesting another"
                )
            self._busy = True
            self._generation += 1
            generation = self._generation
            start = dict(self._flex)

        target = dict(pose.flex)
        tick_ms = self._config.timing.interpolation_tick_ms
        try:
            for is_last, fraction in _interpolation_steps(transition_ms, tick_ms):
                self._sleep(tick_ms / _MS_PER_SECOND)
                with self._lock:
                    if self._generation != generation:
                        return
                    self._flex = _blend(start, target, fraction, is_last=is_last)

            self._sleep(hold_ms / _MS_PER_SECOND)
        finally:
            with self._lock:
                if self._generation == generation:
                    self._busy = False


def _interpolation_steps(
    transition_ms: int, tick_ms: int
) -> Iterator[Tuple[bool, float]]:
    """(마지막 스텝인가, 진행 비율) 을 순서대로 내놓는다.

    전환 시간이 틱보다 짧아도 최소 한 스텝은 돈다 — 목표에 도달하지 않은 채
    끝나는 경로를 만들지 않기 위해서다.
    """
    ticks = max(1, round(transition_ms / tick_ms))
    for i in range(1, ticks + 1):
        yield i == ticks, i / ticks


def _blend(
    start: Mapping[str, float],
    target: Mapping[str, float],
    fraction: float,
    *,
    is_last: bool,
) -> Dict[str, float]:
    if is_last:
        # 마지막 스텝은 계산하지 않고 목표를 그대로 쓴다. 부동소수 오차가 남아
        # "도착했는데 목표와 다른" 상태가 되는 것을 막는다.
        return dict(target)
    return {
        finger: start[finger] + fraction * (target[finger] - start[finger])
        for finger in target
    }
