"""자세 설정(config/poses.json, schema v2) 로딩과 검증.

이 모듈은 spellhand의 유일한 자세 테이블 진입점이다. 자세 flex 값과 손가락별
서보 각도(calibration)는 config/poses.json 에서만 오며, 코드는 그것을 읽기만 한다.

검증에 실패하면 ConfigError 를 던진다. 경고 로그로 넘어가지 않는다 — 잘못된
자세 테이블로 서버가 뜨는 것보다 뜨지 않는 편이 안전하다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

__all__ = [
    "ConfigError",
    "Config",
    "FingerCalibration",
    "Timing",
    "Validation",
    "ConfusionPair",
    "load_config",
    "DEFAULT_CONFIG_PATH",
    "SUPPORTED_SCHEMA_VERSION",
]

# 이 모듈이 읽을 수 있는 스키마 버전. 다른 값이면 로딩을 거부한다.
SUPPORTED_SCHEMA_VERSION = 2

# 리포지토리 루트 기준 기본 설정 경로.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "poses.json"

# 서보 기종의 물리 가동범위(도). 자세 값도 캘리브레이션 값도 아닌 하드웨어 상수이며,
# calibration 의 open/closed 가 이 범위 안에 있는지 확인하는 데에만 쓴다.
# 각도 리터럴 금지 원칙의 유일한 의도된 예외다 (DECISIONS.md "서보 가동범위 상수").
SERVO_ANGLE_MIN_DEG = 0
SERVO_ANGLE_MAX_DEG = 180

# flex 는 0.0~1.0 정규화 굽힘 정도다. 0.0 = 완전히 폄, 1.0 = 안전 최대 굽힘.
FLEX_MIN = 0.0
FLEX_MAX = 1.0

# 관측 시점 허용값.
OBSERVE_FROM_ARRIVAL = "arrival"
OBSERVE_FROM_SETTLED = "settled"
OBSERVE_FROM_VALUES = (OBSERVE_FROM_ARRIVAL, OBSERVE_FROM_SETTLED)

# min_finger_delta 비교용 부동소수 허용오차. 임계값이 0.20 일 때 실제 flex 값의
# 차가 0.1999999999999999... 로 계산되어 버리는 이진 표현 오차를 흡수한다. 설정에
# 적힌 임계값의 의미("이상")를 지키기 위한 것이지 임계값 자체를 바꾸는 것이 아니다.
# 구체적인 반례는 DECISIONS.md §5 "부동소수 주의" 참조.
_DELTA_EPSILON = 1e-9

Pose = Dict[str, float]


class ConfigError(Exception):
    """자세 설정 파일이 스키마를 만족하지 못할 때 발생한다."""


def _fail(path: str, actual: Any, expected: str) -> ConfigError:
    return ConfigError(f"{path}: got {actual!r}, expected {expected}")


# --------------------------------------------------------------------------- #
# 원시 타입 헬퍼
# --------------------------------------------------------------------------- #

def _require_mapping(node: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(node, Mapping):
        raise _fail(path, node, "an object")
    return node


def _require_key(node: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in node:
        raise ConfigError(f"{path}.{key}: missing, expected this key to be present")
    return node[key]


def _require_int(
    value: Any,
    path: str,
    *,
    minimum: "int | None" = None,
    maximum: "int | None" = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, value, "an integer")
    if minimum is not None and value < minimum:
        raise _fail(path, value, f"an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise _fail(path, value, f"an integer <= {maximum}")
    return value


def _require_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, value, "a number")
    return float(value)


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, value, "a boolean")
    return value


def _require_nonempty_str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, value, "a non-empty string")
    return value


def _require_array(value: Any, path: str, expected: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _fail(path, value, expected)
    return value


# --------------------------------------------------------------------------- #
# 자료 구조
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FingerCalibration:
    """한 손가락의 서보 각도 양 끝점. flex 0.0 -> open, flex 1.0 -> closed."""

    open: int
    closed: int


@dataclass(frozen=True)
class Timing:
    transition_ms: int
    transition_slow_ms: int
    hold_min_ms: int
    hold_max_ms: int
    hold_default_ms: int
    interpolation_tick_ms: int
    observe_from: str
    settle_ms: int
    idle_timeout_ms: int


@dataclass(frozen=True)
class Validation:
    """두 자세가 얼마나 달라야 '다른 자세'로 치는지에 대한 기준."""

    neutral_min_distinct_fingers: int
    min_finger_delta: float


@dataclass(frozen=True)
class ConfusionPair:
    pair: Tuple[str, str]
    axis: str
    depth: bool
    hint: str


@dataclass(frozen=True)
class Config:
    schema_version: int
    device_id: str
    dof: Mapping[str, Any]
    fingers: Tuple[str, ...]
    calibration: Mapping[str, FingerCalibration]
    timing: Timing
    poses: Mapping[str, Pose]
    neutral: Pose
    safe: Pose
    validation: Validation
    confusion_pairs: Tuple[ConfusionPair, ...]
    curriculum: Mapping[str, Any]
    source_path: Path

    def distinct_fingers(
        self, a: Mapping[str, float], b: Mapping[str, float]
    ) -> Tuple[str, ...]:
        """두 자세에서 flex 차이가 min_finger_delta 이상인 손가락 이름들."""
        threshold = self.validation.min_finger_delta
        return tuple(
            finger
            for finger in self.fingers
            if abs(a[finger] - b[finger]) >= threshold - _DELTA_EPSILON
        )

    def depth_pairs(self) -> Tuple[ConfusionPair, ...]:
        """굽힘 깊이로만 갈리는 혼동쌍 (커리큘럼에서 늦게 푸는 쌍)."""
        return tuple(p for p in self.confusion_pairs if p.depth)


# --------------------------------------------------------------------------- #
# 블록별 파싱 + 검증
# --------------------------------------------------------------------------- #

def _parse_fingers(raw: Mapping[str, Any]) -> Tuple[str, ...]:
    value = _require_array(
        _require_key(raw, "fingers", "$"), "$.fingers", "an array of finger names"
    )
    if not value:
        raise _fail("$.fingers", value, "a non-empty array of finger names")
    fingers = [
        _require_nonempty_str(name, f"$.fingers[{i}]") for i, name in enumerate(value)
    ]
    if len(set(fingers)) != len(fingers):
        raise _fail("$.fingers", fingers, "unique finger names")
    return tuple(fingers)


def _parse_pose(raw: Any, path: str, fingers: Tuple[str, ...]) -> Pose:
    node = _require_mapping(raw, path)
    expected = set(fingers)
    actual = set(node.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        raise ConfigError(
            f"{path}: keys {sorted(actual)} ({', '.join(detail)}), "
            f"expected exactly the fingers {sorted(expected)}"
        )
    pose: Pose = {}
    for finger in fingers:
        flex = _require_number(node[finger], f"{path}.{finger}")
        if not (FLEX_MIN <= flex <= FLEX_MAX):
            raise _fail(
                f"{path}.{finger}", flex, f"a flex value in [{FLEX_MIN}, {FLEX_MAX}]"
            )
        pose[finger] = flex
    return pose


def _parse_calibration(
    raw: Mapping[str, Any], fingers: Tuple[str, ...]
) -> Dict[str, FingerCalibration]:
    node = _require_mapping(_require_key(raw, "calibration", "$"), "$.calibration")
    expected = set(fingers)
    actual = set(node.keys())
    if actual != expected:
        raise ConfigError(
            f"$.calibration: keys {sorted(actual)}, "
            f"expected exactly the fingers listed in $.fingers {sorted(expected)}"
        )
    out: Dict[str, FingerCalibration] = {}
    for finger in fingers:
        entry = _require_mapping(node[finger], f"$.calibration.{finger}")
        angles: Dict[str, int] = {}
        for endpoint in ("open", "closed"):
            angles[endpoint] = _require_int(
                _require_key(entry, endpoint, f"$.calibration.{finger}"),
                f"$.calibration.{finger}.{endpoint}",
                minimum=SERVO_ANGLE_MIN_DEG,
                maximum=SERVO_ANGLE_MAX_DEG,
            )
        if angles["open"] == angles["closed"]:
            raise _fail(
                f"$.calibration.{finger}",
                angles,
                "open and closed to be different angles "
                "(a finger with no travel cannot be driven)",
            )
        out[finger] = FingerCalibration(open=angles["open"], closed=angles["closed"])
    return out


def _parse_timing(raw: Mapping[str, Any]) -> Timing:
    node = _require_mapping(_require_key(raw, "timing", "$"), "$.timing")
    values: Dict[str, Any] = {}
    for name in (
        "transition_ms",
        "transition_slow_ms",
        "hold_min_ms",
        "hold_max_ms",
        "hold_default_ms",
        "interpolation_tick_ms",
        "settle_ms",
        "idle_timeout_ms",
    ):
        values[name] = _require_int(
            _require_key(node, name, "$.timing"), f"$.timing.{name}", minimum=0
        )

    observe_from = _require_nonempty_str(
        _require_key(node, "observe_from", "$.timing"), "$.timing.observe_from"
    )
    if observe_from not in OBSERVE_FROM_VALUES:
        raise _fail(
            "$.timing.observe_from", observe_from, f"one of {list(OBSERVE_FROM_VALUES)}"
        )
    values["observe_from"] = observe_from

    if values["transition_ms"] > values["transition_slow_ms"]:
        raise _fail(
            "$.timing.transition_ms",
            values["transition_ms"],
            f"a value <= $.timing.transition_slow_ms ({values['transition_slow_ms']})",
        )
    if values["hold_min_ms"] > values["hold_default_ms"]:
        raise _fail(
            "$.timing.hold_min_ms",
            values["hold_min_ms"],
            f"a value <= $.timing.hold_default_ms ({values['hold_default_ms']})",
        )
    if values["hold_default_ms"] > values["hold_max_ms"]:
        raise _fail(
            "$.timing.hold_default_ms",
            values["hold_default_ms"],
            f"a value <= $.timing.hold_max_ms ({values['hold_max_ms']})",
        )
    if observe_from == OBSERVE_FROM_SETTLED and values["settle_ms"] <= 0:
        raise _fail(
            "$.timing.settle_ms",
            values["settle_ms"],
            f"a positive value when $.timing.observe_from is {OBSERVE_FROM_SETTLED!r}",
        )
    return Timing(**values)


def _parse_validation(raw: Mapping[str, Any]) -> Validation:
    node = _require_mapping(_require_key(raw, "validation", "$"), "$.validation")
    min_distinct = _require_int(
        _require_key(node, "neutral_min_distinct_fingers", "$.validation"),
        "$.validation.neutral_min_distinct_fingers",
        minimum=1,
    )
    min_delta = _require_number(
        _require_key(node, "min_finger_delta", "$.validation"),
        "$.validation.min_finger_delta",
    )
    if not (FLEX_MIN < min_delta <= FLEX_MAX):
        raise _fail(
            "$.validation.min_finger_delta",
            min_delta,
            f"a value in ({FLEX_MIN}, {FLEX_MAX}]",
        )
    return Validation(
        neutral_min_distinct_fingers=min_distinct, min_finger_delta=min_delta
    )


def _parse_confusion_pairs(
    raw: Mapping[str, Any], poses: Mapping[str, Pose]
) -> Tuple[ConfusionPair, ...]:
    entries = _require_array(
        _require_key(raw, "confusion_pairs", "$"), "$.confusion_pairs", "an array"
    )

    out: List[ConfusionPair] = []
    seen: Dict[frozenset, int] = {}
    for i, entry in enumerate(entries):
        path = f"$.confusion_pairs[{i}]"
        node = _require_mapping(entry, path)

        letters = _require_array(
            _require_key(node, "pair", path),
            f"{path}.pair",
            "an array of exactly 2 letters",
        )
        if len(letters) != 2:
            raise _fail(f"{path}.pair", list(letters), "an array of exactly 2 letters")
        a = _require_nonempty_str(letters[0], f"{path}.pair[0]")
        b = _require_nonempty_str(letters[1], f"{path}.pair[1]")

        for pos, letter in ((0, a), (1, b)):
            if letter not in poses:
                raise _fail(
                    f"{path}.pair[{pos}]",
                    letter,
                    f"a letter present in $.poses {sorted(poses)}",
                )
        if a == b:
            raise _fail(
                f"{path}.pair",
                [a, b],
                "two different letters (a letter is never confused with itself)",
            )

        key = frozenset((a, b))
        if key in seen:
            raise _fail(
                f"{path}.pair",
                [a, b],
                f"a pair not already declared at $.confusion_pairs[{seen[key]}] "
                f"(pair order is not significant)",
            )
        seen[key] = i

        out.append(
            ConfusionPair(
                pair=(a, b),
                axis=_require_nonempty_str(
                    _require_key(node, "axis", path), f"{path}.axis"
                ),
                depth=_require_bool(_require_key(node, "depth", path), f"{path}.depth"),
                hint=_require_nonempty_str(
                    _require_key(node, "hint", path), f"{path}.hint"
                ),
            )
        )
    return tuple(out)


def _check_pose_relationships(config: Config) -> None:
    """자세들 사이의 관계 검증 — 자세 하나만 봐서는 알 수 없는 것들."""
    threshold = config.validation.min_finger_delta

    # 안전 자세는 텐던 최저 장력 상태여야 한다: 전 손가락 완전히 폄.
    for finger in config.fingers:
        flex = config.safe[finger]
        if flex != FLEX_MIN:
            raise _fail(
                f"$.safe.{finger}",
                flex,
                f"exactly {FLEX_MIN} (the safe pose must leave every tendon at rest)",
            )

    # 중립 자세는 글자 경계 신호이므로 모든 글자와 충분히 구별되어야 한다.
    required = config.validation.neutral_min_distinct_fingers
    for letter in sorted(config.poses):
        distinct = config.distinct_fingers(config.neutral, config.poses[letter])
        if len(distinct) < required:
            raise _fail(
                f"$.neutral vs $.poses.{letter}",
                f"{len(distinct)} distinct finger(s) {list(distinct)}",
                f">= {required} fingers differing by at least {threshold} "
                f"(both thresholds come from $.validation)",
            )

    # 서로 구별되지 않는 두 글자가 있으면 채점이 불가능하다.
    letters = sorted(config.poses)
    for i, a in enumerate(letters):
        for b in letters[i + 1:]:
            distinct = config.distinct_fingers(config.poses[a], config.poses[b])
            if not distinct:
                raise _fail(
                    f"$.poses.{a} vs $.poses.{b}",
                    "0 distinct fingers",
                    f">= 1 finger differing by at least {threshold} "
                    f"(identical poses cannot be told apart)",
                )


# --------------------------------------------------------------------------- #
# 진입점
# --------------------------------------------------------------------------- #

def load_config(path: "str | Path" = DEFAULT_CONFIG_PATH) -> Config:
    """poses.json 을 읽어 검증한 Config 를 돌려준다.

    검증에 실패하면 ConfigError 를 던진다. 예외 메시지에는 문제가 있는 필드
    경로, 실제로 들어 있던 값, 기대한 범위가 함께 들어간다.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"$: cannot read pose config at {source}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"$: {source} is not valid JSON: {exc}") from exc

    root = _require_mapping(raw, "$")

    schema_version = _require_key(root, "schema_version", "$")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise _fail(
            "$.schema_version", schema_version, f"exactly {SUPPORTED_SCHEMA_VERSION}"
        )

    device_id = _require_nonempty_str(_require_key(root, "device_id", "$"), "$.device_id")
    dof = _require_mapping(_require_key(root, "dof", "$"), "$.dof")

    fingers = _parse_fingers(root)
    calibration = _parse_calibration(root, fingers)
    timing = _parse_timing(root)
    validation = _parse_validation(root)

    poses_node = _require_mapping(_require_key(root, "poses", "$"), "$.poses")
    if not poses_node:
        raise _fail("$.poses", poses_node, "at least one letter pose")
    poses = {
        letter: _parse_pose(pose, f"$.poses.{letter}", fingers)
        for letter, pose in poses_node.items()
    }

    neutral = _parse_pose(_require_key(root, "neutral", "$"), "$.neutral", fingers)
    safe = _parse_pose(_require_key(root, "safe", "$"), "$.safe", fingers)

    confusion_pairs = _parse_confusion_pairs(root, poses)
    curriculum = _require_mapping(_require_key(root, "curriculum", "$"), "$.curriculum")

    config = Config(
        schema_version=schema_version,
        device_id=device_id,
        dof=dof,
        fingers=fingers,
        calibration=calibration,
        timing=timing,
        poses=poses,
        neutral=neutral,
        safe=safe,
        validation=validation,
        confusion_pairs=confusion_pairs,
        curriculum=curriculum,
        source_path=source,
    )
    _check_pose_relationships(config)
    return config
