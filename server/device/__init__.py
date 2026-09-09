"""디바이스 계층의 계약 — Pose, DeviceAdapter, 예외 타입, 퍼밀 변환.

Mock 과 Serial 이 공유한다. 이 계층 아래로는 **글자 이름이 내려가지 않는다**
(R2). 어댑터는 flex 5개를 담은 Pose 를 받을 뿐이고, 펌웨어는 자기가 무슨
글자를 만들고 있는지 모른다.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Optional

from server.config import FLEX_MAX, FLEX_MIN, Config

__all__ = [
    "Pose",
    "DeviceAdapter",
    "DeviceError",
    "DeviceBusyError",
    "DeviceNotConnectedError",
    "DeviceTimeoutError",
    "DeviceCancelledError",
    "DeviceConfigError",
    "DeviceProtocolError",
    "DeviceRangeError",
    "DeviceTimingError",
    "DeviceSequenceError",
    "CalibrationMismatchError",
    "FIRMWARE_ERRORS",
    "PERMILLE_MAX",
    "flex_to_permille",
    "pose_to_permille",
    "DEVICE_MODE_MOCK",
    "DEVICE_MODE_SERIAL",
    "DEVICE_MODES",
    "resolve_device_mode",
    "create_device",
]

logger = logging.getLogger(__name__)

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


class DeviceCancelledError(DeviceError):
    """진행 중이던 동작이 비상 정지(`Z`)로 취소됐다.

    **통신 오류가 아니다.** 프로토콜은 멀쩡하고 사람이 멈춘 것이다. 상위 계층은
    이 둘을 다르게 다뤄야 한다 — 취소는 다음 문항으로 넘어가면 되지만, 통신
    오류는 세션을 접고 락을 놓아야 한다 (DECISIONS.md §42).
    """


class DeviceConfigError(DeviceError):
    """어댑터를 만들 수 없다 (설정이 빠졌거나 앞뒤가 맞지 않는다)."""


class CalibrationMismatchError(DeviceError):
    """펌웨어에 박힌 CAL_HASH 가 지금 설정과 다르다.

    `poses.json` 을 고치고 펌웨어 재업로드를 잊은 상태다. 이 상태에서 움직이면
    서버가 의도한 자세와 손이 실제로 만드는 자세가 다르고, 그 오차는 어디에도
    남지 않는다 (DECISIONS.md §35).
    """


# --------------------------------------------------------------------------- #
# 펌웨어 거부 (ERR <seq> <code>)
# --------------------------------------------------------------------------- #
#
# 원인이 다르면 예외 타입도 다르다. 상위 계층이 "재시도하면 되는가"를 타입만
# 보고 판단할 수 있어야 한다.


class DeviceProtocolError(DeviceError):
    """펌웨어가 명령을 해석하지 못했다 (EPARSE). 형식이 어긋났다는 뜻이다."""


class DeviceRangeError(DeviceError):
    """‰ 가 0~1000 밖이었다 (ERANGE). 펌웨어는 자르지 않고 거부한다."""


class DeviceTimingError(DeviceError):
    """전이·유지 시간이 펌웨어가 받는 범위 밖이었다 (ETIME)."""


class DeviceSequenceError(DeviceError):
    """seq 가 중복이거나 역행했다 (ESEQ)."""


# 펌웨어 코드 -> 예외. 여기 없는 코드는 DeviceProtocolError 로 올린다.
FIRMWARE_ERRORS = {
    "EPARSE": DeviceProtocolError,
    "ERANGE": DeviceRangeError,
    "ETIME": DeviceTimingError,
    "EBUSY": DeviceBusyError,
    "ESEQ": DeviceSequenceError,
}


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

    def last_arrival_at(self) -> Optional[datetime]:
        """직전 동작에서 손이 목표 자세에 **도달한** 시각.

        관측 시간의 기준점이다 (PRD 8장 `ready_at`). 도달 시점을 실제로 알 수
        있는 어댑터만 값을 돌려준다. 기본은 None — 모른다는 뜻이고, 상위 계층은
        유지 시간을 빼서 역산한다 (DECISIONS.md §24, §36).

        추상 메서드가 아닌 이유: 모르는 것이 정상인 어댑터가 있다. MockDevice 는
        이것을 구현하지 않고 기본값을 그대로 쓴다.
        """
        return None


# --------------------------------------------------------------------------- #
# 어댑터 선택
# --------------------------------------------------------------------------- #

DEVICE_MODE_MOCK = "mock"
DEVICE_MODE_SERIAL = "serial"
DEVICE_MODES = (DEVICE_MODE_MOCK, DEVICE_MODE_SERIAL)


def resolve_device_mode(mode: Optional[str] = None) -> str:
    """어떤 어댑터로 뜰지 정한다. **이 판단은 여기 한 곳에만 있다.**

    두 곳에서 정하면 한쪽이 조용히 mock 으로 떨어뜨리고 다른 쪽의 경고가
    묻힌다. 그러면 오타 하나로 실기 대신 Mock 이 뜬 것을 아무도 모른다
    (DECISIONS.md §43).
    """
    resolved = (
        mode or os.environ.get("DEVICE_MODE") or DEVICE_MODE_MOCK
    ).strip().lower()
    if resolved not in DEVICE_MODES:
        logger.warning(
            "DEVICE_MODE is %r, expected one of %s; falling back to %r",
            resolved,
            list(DEVICE_MODES),
            DEVICE_MODE_MOCK,
        )
        return DEVICE_MODE_MOCK
    return resolved


def create_device(
    config: Config,
    *,
    mode: Optional[str] = None,
    port: Optional[str] = None,
    baud: Optional[int] = None,
) -> DeviceAdapter:
    """`DEVICE_MODE` 에 맞는 어댑터를 만든다.

    **선택 로직은 여기 한 곳에만 있다.** 두 곳에 두면 한쪽만 고쳐서 개발에서는
    Mock 이고 시연에서는 실기인 상태가 만들어진다.

    모드를 못 알아보면 Mock 이다 (PRD 6.4: 기본 mock). 다만 조용히 넘어가지
    않고 경고를 남긴다 — 오타로 Mock 이 뜬 것을 손이 고장 난 것으로 오해하지
    않도록.
    """
    from server.device.mock import MockDevice

    resolved = resolve_device_mode(mode)

    if resolved == DEVICE_MODE_MOCK:
        logger.info("device adapter: mock")
        return MockDevice(config)

    # 지연 import: pyserial 이 없는 환경에서도 mock 은 떠야 한다.
    from server.device.serial import DEFAULT_BAUD, SerialDevice

    resolved_port = port or os.environ.get("SERIAL_PORT") or ""
    if not resolved_port.strip():
        raise DeviceConfigError(
            "DEVICE_MODE=serial requires a serial port, but SERIAL_PORT is empty. "
            "Set SERIAL_PORT (for example COM3) in .env or the environment, "
            "or run with DEVICE_MODE=mock."
        )

    resolved_baud = baud or int(os.environ.get("SERIAL_BAUD") or DEFAULT_BAUD)
    logger.info(
        "device adapter: serial on %s @ %d baud", resolved_port.strip(), resolved_baud
    )
    return SerialDevice(config, port=resolved_port.strip(), baud=resolved_baud)
