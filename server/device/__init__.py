"""디바이스 계층의 계약 — Pose, DeviceAdapter, 예외 타입, 퍼밀 변환.

Mock 과 Serial 이 공유한다. 이 계층 아래로는 **글자 이름이 내려가지 않는다**
(R2). 어댑터는 flex 5개를 담은 Pose 를 받을 뿐이고, 펌웨어는 자기가 무슨
글자를 만들고 있는지 모른다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from server.config import FLEX_MAX, FLEX_MIN, Config

__all__ = [
    "Pose",
    "DeviceAdapter",
    "DeviceError",
    "DeviceBusyError",
    "DeviceNotConnectedError",
    "DeviceTimeoutError",
    "PERMILLE_MAX",
    "flex_to_permille",
    "pose_to_permille",
]

# PRD 7장 프로토콜. 펌웨어에는 각도도 flex 실수도 아닌 퍼밀 정수가 간다.
# 하드웨어 종속값이 아니라 프로토콜 상수이므로 설정이 아니라 여기 둔다.
PERMILLE_MAX = 1000


# --------------------------------------------------------------------------- #
# 예외
# --------------------------------------------------------------------------- #

class DeviceError(Exception):
    """디바이스 계층의 모든 실패의 베이스."""


class DeviceBusyError(DeviceError):
    """동작 중인 디바이스에 새 동작을 요청했다."""


class DeviceNotConnectedError(DeviceError):
    """연결되지 않은 디바이스에 동작을 요청했다."""


class DeviceTimeoutError(DeviceError):
    """디바이스가 기대한 시간 안에 응답하지 않았다."""


# --------------------------------------------------------------------------- #
# Pose
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Pose:
    """손 하나의 자세. flex 5개뿐이며 **글자 이름을 담지 않는다** (R2).

    어떤 글자인지는 상위 계층이 알고 있으면 된다. 디바이스는 모른다.
    """

    flex: Mapping[str, float]

    def __post_init__(self) -> None:
        values = dict(self.flex)
        if not values:
            raise ValueError("pose has no fingers, expected at least one")
        for finger, value in values.items():
            if not isinstance(finger, str) or not finger:
                raise ValueError(
                    f"pose has a non-string finger name {finger!r}, "
                    f"expected finger names"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"pose[{finger!r}] is {value!r}, expected a number"
                )
            if not (FLEX_MIN <= value <= FLEX_MAX):
                raise ValueError(
                    f"pose[{finger!r}] is {value!r}, "
                    f"expected a flex value in [{FLEX_MIN}, {FLEX_MAX}]"
                )
            values[finger] = float(value)
        # dataclass 는 frozen 이지만 안에 든 dict 는 아니다. 읽기 전용으로 감싼다.
        object.__setattr__(self, "flex", MappingProxyType(values))

    @classmethod
    def from_config(cls, config: Config, values: Mapping[str, float]) -> "Pose":
        """설정에서 읽은 dict 로부터 만든다. 손가락 키가 정확히 맞아야 한다."""
        expected = set(config.fingers)
        actual = set(values)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                f"pose has keys {sorted(actual)} (missing {missing}, "
                f"unexpected {extra}), expected exactly {sorted(expected)}"
            )
        return cls(flex=values)


# --------------------------------------------------------------------------- #
# 퍼밀 변환
# --------------------------------------------------------------------------- #

def flex_to_permille(flex: float) -> int:
    """flex 를 퍼밀 정수로 바꾼다 (PRD 7장).

    이 변환이 어댑터 계층에 있는 이유는 DECISIONS.md §16 참조.
    """
    if isinstance(flex, bool) or not isinstance(flex, (int, float)):
        raise ValueError(f"flex is {flex!r}, expected a number")
    bounded = min(max(float(flex), FLEX_MIN), FLEX_MAX)
    return round(bounded * PERMILLE_MAX)


def pose_to_permille(pose: Pose) -> Mapping[str, int]:
    """자세 전체를 퍼밀로 바꾼다. 전송 직전 어댑터가 쓴다."""
    if not isinstance(pose, Pose):
        raise TypeError(
            f"expected a Pose, got {type(pose).__name__} ({pose!r}). "
            f"The device layer never takes letter names (R2)."
        )
    return MappingProxyType(
        {finger: flex_to_permille(value) for finger, value in pose.flex.items()}
    )


# --------------------------------------------------------------------------- #
# DeviceAdapter
# --------------------------------------------------------------------------- #

class DeviceAdapter(ABC):
    """Mock 과 Serial 이 공유하는 계약.

    **동기(sync) 인터페이스다.** async 를 쓰지 않는다 — 근거는
    DECISIONS.md §14.

    중립과 안전은 목적이 다른 별개 자세이므로 별개 메서드다. 하나가 다른
    하나를 호출하지 않는다 (R3).
    """

    @abstractmethod
    def connect(self) -> None:
        """디바이스를 연다. 연결 직후 상태는 안전 자세다."""

    @abstractmethod
    def disconnect(self) -> None:
        """디바이스를 닫는다."""

    @property
    @abstractmethod
    def is_busy(self) -> bool:
        """이동 또는 유지 중인가."""

    @abstractmethod
    def move_to(self, pose: Pose, transition_ms: int, hold_ms: int) -> None:
        """자세로 이동한 뒤 유지한다. 완료될 때까지 돌아오지 않는다.

        Pose 객체만 받는다. 글자 이름을 넘기면 거부한다 (R2).
        """

    @abstractmethod
    def neutral(self) -> None:
        """중립 자세로 이동한다. 글자와 글자 사이의 경계 신호다."""

    @abstractmethod
    def safe(self) -> None:
        """안전 자세로 이동한다. 텐던 장력이 가장 낮은 상태다."""

    @abstractmethod
    def stop(self) -> None:
        """즉시 정지하고 안전 자세로 만든다."""
