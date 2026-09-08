"""flex(0.0~1.0) <-> 서보 각도 변환.

자세는 flex 로 기술되고 서보는 각도로 움직인다. 그 사이를 잇는 곳은 이 모듈
하나뿐이다. 각도의 출처는 언제나 `Config.calibration` 이며, 이 파일에는 각도가
리터럴로 등장하지 않는다 (R1).

여기는 순수 변환 계층이다. 시리얼 전송, 보간, 글자 이름 조회, 퍼밀 변환은
하지 않는다 — 이 모듈은 "B" 가 무엇인지 몰라도 된다.

전역 상태나 모듈 캐시를 두지 않는다. 필요한 값은 전부 인자로 받은 `Config`
에서 읽는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Mapping, Tuple

from server.config import FLEX_MAX, FLEX_MIN, Config

__all__ = [
    "flex_to_angle",
    "angle_to_flex",
    "pose_to_angles",
    "angles_to_pose",
    "canonical_payload",
    "calibration_hash",
    "HASH_LENGTH",
]

# CAL_HASH 앞자리 수. 펌웨어 헤더와 서버가 같은 길이를 써야 대조가 성립한다.
HASH_LENGTH = 8

logger = logging.getLogger(__name__)

# 계산으로 만들어진 값이 경계를 부동소수 오차만큼 넘는 경우까지 경고로 보고하면
# 진짜 버그 신호가 잡음에 묻힌다. 이 폭 안의 초과는 조용히 자르고, 그보다 큰
# 초과만 경고한다. 설정에서 직접 읽은 값(정방향 flex 입력)에는 적용하지 않는다.
_FLOAT_TOLERANCE = 1e-9


def _clamp(
    value: float,
    low: float,
    high: float,
    *,
    finger: str,
    quantity: str,
    unit: str,
    tolerance: float = 0.0,
) -> float:
    """[low, high] 로 자른다. 잘렸으면 경고를 남긴다.

    정상 동작에서는 절대 발생하지 않아야 한다. 발생 자체가 버그 신호이므로
    조용히 자르지 않고 손가락 이름·입력값·잘린 결과를 함께 남긴다.
    """
    if value < low:
        if value < low - tolerance:
            logger.warning(
                "clamped %s for finger %r: %s%s is below the allowed minimum, "
                "using %s%s instead (allowed range %s%s..%s%s)",
                quantity, finger, value, unit, low, unit, low, unit, high, unit,
            )
        return low
    if value > high:
        if value > high + tolerance:
            logger.warning(
                "clamped %s for finger %r: %s%s is above the allowed maximum, "
                "using %s%s instead (allowed range %s%s..%s%s)",
                quantity, finger, value, unit, high, unit, low, unit, high, unit,
            )
        return high
    return value


def _angle_limits(config: Config, finger: str) -> Tuple[float, float]:
    """이 손가락이 실제로 가도 되는 각도 구간.

    캘리브레이션 양 끝점이 만드는 구간과 서보 물리 가동범위의 교집합이다.
    open/closed 의 대소는 손가락마다 다르므로 min/max 로 정규화한다.
    """
    cal = config.calibration[finger]
    low = max(min(cal.open, cal.closed), config.servo_range.min)
    high = min(max(cal.open, cal.closed), config.servo_range.max)
    return float(low), float(high)


def _require_finger(config: Config, finger: str) -> None:
    if finger not in config.calibration:
        raise KeyError(
            f"unknown finger {finger!r}; this hand has {list(config.fingers)}"
        )


def flex_to_angle(config: Config, finger: str, flex: float) -> float:
    """flex 를 서보 각도로 바꾼다.

    `angle = open + flex * (closed - open)`

    공식은 어느 쪽이 큰지 가정하지 않는다. thumb/pinky 는 open 이 closed 보다
    커서 flex 가 커질수록 각도가 **줄고**, index/middle/ring 은 반대로 늘어난다.
    선형 보간이 그 방향을 알아서 처리한다 — abs() 나 방향 분기를 넣으면 절반의
    손가락이 반대로 움직인다.

    클램프는 두 단계다. 입력 flex 를 자르고, 계산된 각도를 다시 자른다.
    (근거는 DECISIONS.md §11)
    """
    _require_finger(config, finger)
    cal = config.calibration[finger]

    safe_flex = _clamp(
        float(flex),
        FLEX_MIN,
        FLEX_MAX,
        finger=finger,
        quantity="flex",
        unit="",
    )
    angle = cal.open + safe_flex * (cal.closed - cal.open)

    low, high = _angle_limits(config, finger)
    return _clamp(
        float(angle),
        low,
        high,
        finger=finger,
        quantity="angle",
        unit=" deg",
        tolerance=_FLOAT_TOLERANCE,
    )


def angle_to_flex(config: Config, finger: str, angle: float) -> float:
    """서보 각도를 flex 로 되돌린다. 디버깅·시각화·왕복 검증용.

    `open == closed` 는 설정 검증에서 이미 걸러지므로 0 으로 나눌 일은 없다.
    """
    _require_finger(config, finger)
    cal = config.calibration[finger]

    low, high = _angle_limits(config, finger)
    safe_angle = _clamp(
        float(angle),
        low,
        high,
        finger=finger,
        quantity="angle",
        unit=" deg",
    )
    flex = (safe_angle - cal.open) / (cal.closed - cal.open)

    # 각도를 이미 잘랐으므로 이 값은 구성상 [0, 1] 안이다. 남는 것은 나눗셈의
    # 부동소수 오차뿐이라 허용폭을 준다.
    return _clamp(
        float(flex),
        FLEX_MIN,
        FLEX_MAX,
        finger=finger,
        quantity="flex",
        unit="",
        tolerance=_FLOAT_TOLERANCE,
    )


def _require_all_fingers(config: Config, values: Mapping[str, float], what: str) -> None:
    expected = set(config.fingers)
    actual = set(values)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"{what} has keys {sorted(actual)} (missing {missing}, "
            f"unexpected {extra}), expected exactly {sorted(expected)}"
        )


def pose_to_angles(config: Config, pose: Mapping[str, float]) -> Dict[str, float]:
    """자세 하나를 손가락별 각도로 바꾼다.

    각도는 float 그대로 돌려주고 **반올림하지 않는다.** 반올림은 전송 직전
    어댑터가 한다 (DECISIONS.md §13).
    """
    _require_all_fingers(config, pose, "pose")
    return {
        finger: flex_to_angle(config, finger, pose[finger])
        for finger in config.fingers
    }


def angles_to_pose(config: Config, angles: Mapping[str, float]) -> Dict[str, float]:
    """손가락별 각도를 자세(flex)로 되돌린다."""
    _require_all_fingers(config, angles, "angles")
    return {
        finger: angle_to_flex(config, finger, angles[finger])
        for finger in config.fingers
    }


# --------------------------------------------------------------------------- #
# 캘리브레이션 해시 (PRD 5.1)
# --------------------------------------------------------------------------- #
#
# 서버와 헤더 생성기가 **같은 정의**를 써야 대조가 성립한다. 그래서 정의는
# 여기 한 곳에 있고 `tools/gen_calibration.py` 가 가져다 쓴다. 두 벌로 두면
# 언젠가 한쪽만 고쳐서, 실제로는 다른데 해시는 같은 상태가 만들어진다.


def canonical_payload(config: Config) -> Dict[str, Any]:
    """해시 대상. **헤더에 실제로 들어가는 값과 정확히 같아야 한다.**

    이 규칙 덕분에 "해시가 다르다" 와 "헤더 내용이 다르다" 가 같은 말이 된다.
    헤더에 새 값을 내보내면 여기에도 넣어야 하고, 그러지 않으면 그 값을 바꿔도
    재업로드가 필요하다는 신호가 뜨지 않는다 (DECISIONS.md §30).
    """
    fingers = list(config.fingers)
    return {
        "device_id": config.device_id,
        "fingers": fingers,
        "servo_range_deg": {
            "min": config.servo_range.min,
            "max": config.servo_range.max,
        },
        "calibration": {
            finger: {
                "open": config.calibration[finger].open,
                "closed": config.calibration[finger].closed,
            }
            for finger in fingers
        },
        "pins": {finger: config.pins[finger] for finger in fingers},
        "neutral": {finger: config.neutral[finger] for finger in fingers},
        "safe": {finger: config.safe[finger] for finger in fingers},
        "timing": {
            "interpolation_tick_ms": config.timing.interpolation_tick_ms,
            "idle_timeout_ms": config.timing.idle_timeout_ms,
            "transition_ms": config.timing.transition_ms,
            "max_transition_ms": config.timing.transition_slow_ms,
            "max_hold_ms": config.timing.hold_max_ms,
        },
    }


def calibration_hash(config: Config) -> str:
    """`CAL_HASH` 값. 펌웨어 헤더에 박히는 것과 같은 문자열이다."""
    blob = json.dumps(
        canonical_payload(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HASH_LENGTH]
