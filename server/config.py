"""자세 설정(config/poses.json, schema v2) 로딩과 검증.

이 모듈은 spellhand의 유일한 자세 테이블 진입점이다. 자세 flex 값과 손가락별
서보 각도(calibration)는 config/poses.json 에서만 오며, 코드는 그것을 읽기만 한다.

검증에 실패하면 ConfigError 를 던진다. 경고 로그로 넘어가지 않는다 — 잘못된
자세 테이블로 서버가 뜨는 것보다 뜨지 않는 편이 안전하다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

__all__ = [
    "ConfigError",
    "Config",
    "ServoRange",
    "FingerCalibration",
    "Timing",
    "Validation",
    "ConfusionPair",
    "load_config",
    "DEFAULT_CONFIG_PATH",
    "SUPPORTED_SCHEMA_VERSION",
    "Word",
    "ExcludedWord",
    "WordSet",
    "load_words",
    "DEFAULT_WORDS_PATH",
]

logger = logging.getLogger(__name__)

# 이 모듈이 읽을 수 있는 스키마 버전. 다른 값이면 로딩을 거부한다.
SUPPORTED_SCHEMA_VERSION = 2

# 리포지토리 루트 기준 기본 설정 경로.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "poses.json"
DEFAULT_WORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "words.json"

# 서보 각도는 이 모듈 어디에도 리터럴로 등장하지 않는다. 가동범위조차
# calibration.servo_range_deg 에서 읽는다 — 하드웨어를 바꾸면 poses.json 만
# 고치면 되도록 (DECISIONS.md §2).

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
class ServoRange:
    """서보 기종의 물리 가동범위(도). 하드웨어를 바꾸면 이 값도 바뀐다."""

    min: int
    max: int


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
    servo_range: ServoRange
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


def _parse_servo_range(node: Mapping[str, Any]) -> ServoRange:
    """서보 가동범위. 각도 상·하한은 코드가 아니라 여기서만 온다."""
    path = "$.calibration.servo_range_deg"
    entry = _require_mapping(_require_key(node, "servo_range_deg", "$.calibration"), path)
    low = _require_int(_require_key(entry, "min", path), f"{path}.min")
    high = _require_int(_require_key(entry, "max", path), f"{path}.max")
    if low >= high:
        raise _fail(
            f"{path}.min",
            low,
            f"a value strictly below {path}.max ({high})",
        )
    return ServoRange(min=low, max=high)


def _parse_calibration(
    raw: Mapping[str, Any], fingers: Tuple[str, ...]
) -> Tuple[ServoRange, Dict[str, FingerCalibration]]:
    node = _require_mapping(_require_key(raw, "calibration", "$"), "$.calibration")
    servo_range = _parse_servo_range(node)

    per_finger = _require_mapping(
        _require_key(node, "fingers", "$.calibration"), "$.calibration.fingers"
    )
    expected = set(fingers)
    actual = set(per_finger.keys())
    if actual != expected:
        raise ConfigError(
            f"$.calibration.fingers: keys {sorted(actual)}, "
            f"expected exactly the fingers listed in $.fingers {sorted(expected)}"
        )

    out: Dict[str, FingerCalibration] = {}
    for finger in fingers:
        finger_path = f"$.calibration.fingers.{finger}"
        entry = _require_mapping(per_finger[finger], finger_path)
        angles: Dict[str, int] = {}
        for endpoint in ("open", "closed"):
            angles[endpoint] = _require_int(
                _require_key(entry, endpoint, finger_path),
                f"{finger_path}.{endpoint}",
                minimum=servo_range.min,
                maximum=servo_range.max,
            )
        if angles["open"] == angles["closed"]:
            raise _fail(
                finger_path,
                angles,
                "open and closed to be different angles "
                "(a finger with no travel cannot be driven)",
            )
        out[finger] = FingerCalibration(open=angles["open"], closed=angles["closed"])
    return servo_range, out


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
    servo_range, calibration = _parse_calibration(root, fingers)
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
        servo_range=servo_range,
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


# --------------------------------------------------------------------------- #
# 통글자 트랙 단어 목록 (config/words.json)
# --------------------------------------------------------------------------- #
#
# poses.json 검증과 성격이 다르다. poses.json 은 틀리면 서버가 뜨면 안 되는
# 설정이지만, words.json 의 문자 집합 필터는 **경고 후 계속**이다. 3주차 제3자
# 판별 테스트에서 글자가 탈락해 세트가 줄어드는 것은 예상된 시나리오이지 설정
# 오류가 아니다. 자세한 근거는 DECISIONS.md §8.

# 단어는 ASCII 알파벳으로만 이루어진다. 공백·숫자·기호·비ASCII 문자 불허.
_WORD_PATTERN = re.compile(r"^[A-Za-z]+$")


@dataclass(frozen=True)
class Word:
    """출제 풀에 남은 단어 하나와, 출제 시점에 매번 다시 계산하지 않을 속성들."""

    letters: str
    has_repeat: bool
    length: int


@dataclass(frozen=True)
class ExcludedWord:
    """현재 문자 집합으로 표현할 수 없어 풀에서 빠진 단어."""

    word: str
    unsupported: Tuple[str, ...]


@dataclass(frozen=True)
class WordSet:
    words: Tuple[Word, ...]
    hook_word: str
    excluded: Tuple[ExcludedWord, ...]
    source_path: Path

    def of_length(self, length: int) -> Tuple[Word, ...]:
        return tuple(word for word in self.words if word.length == length)

    def direct_mode_words(self) -> Tuple[Word, ...]:
        """연결(direct) 모드로 낼 수 있는 단어.

        인접한 두 글자가 같으면 연결 모드에서 손이 움직이지 않아 글자 경계가
        사라진다. 그런 단어는 explicit(또박또박) 모드로만 출제한다.
        """
        return tuple(word for word in self.words if not word.has_repeat)


def _has_adjacent_repeat(word: str) -> bool:
    """인접한 위치에 같은 글자가 오는가. 떨어져 있는 중복은 세지 않는다."""
    return any(a == b for a, b in zip(word, word[1:]))


def _normalize_word(value: Any, path: str) -> str:
    text = _require_nonempty_str(value, path)
    if not _WORD_PATTERN.match(text):
        raise _fail(
            path,
            text,
            "a word of ASCII letters only (no spaces, digits or punctuation)",
        )
    return text.upper()


def _unsupported_letters(word: str, available: Set[str]) -> Tuple[str, ...]:
    return tuple(sorted({letter for letter in word if letter not in available}))


def _resolve_hook_word(
    preferred: str,
    fallbacks: Tuple[str, ...],
    available: Set[str],
    pool: Set[str],
) -> str:
    """훅 단어를 정한다. 표현 불가하면 폴백을 순서대로 시도한다.

    훅은 발표 시연에서 손이 단독으로 표현하는 단어다(PRD 15.1). 전부 표현할 수
    없으면 시연 자체가 성립하지 않으므로 조용히 넘기지 않고 예외를 던진다.
    """
    candidates = (preferred,) + fallbacks
    for index, candidate in enumerate(candidates):
        missing = _unsupported_letters(candidate, available)
        if missing:
            logger.warning(
                "$.hook_word: %r cannot be expressed with the current letter set "
                "(missing %s); trying the next fallback",
                candidate,
                ", ".join(missing),
            )
            continue
        if index:
            logger.warning(
                "$.hook_word: falling back to %r (candidate %d of %d)",
                candidate,
                index + 1,
                len(candidates),
            )
        if candidate not in pool:
            # 실패가 아니다. 훅은 시연용 단독 표현이고 $.words 는 출제 풀이라
            # 목적이 다르다 (DECISIONS.md §10).
            logger.info(
                "$.hook_word: %r is not in the question pool ($.words). "
                "That is allowed - the hook word is a demo utterance, not an item.",
                candidate,
            )
        return candidate

    raise _fail(
        "$.hook_word",
        list(candidates),
        "at least one hook word expressible with the current letter set "
        f"{sorted(available)} (the demo cannot run without one)",
    )


def load_words(
    path: "str | Path" = DEFAULT_WORDS_PATH,
    *,
    letters: Iterable[str],
) -> WordSet:
    """words.json 을 읽어 현재 문자 집합으로 표현 가능한 단어만 남긴다.

    `letters` 는 지금 손이 만들 수 있는 글자들이다. 보통 `Config.poses` 를
    그대로 넘긴다.

    표현할 수 없는 단어는 **경고를 남기고 제외**할 뿐 예외를 던지지 않는다.
    다음의 경우에만 ConfigError 를 던진다.

    - 파일이 없거나 JSON 이 깨졌을 때
    - `$.words` 가 없거나, 비었거나, 알파벳이 아닌 단어나 중복이 있을 때
    - `$.hook_word` 가 없을 때
    - 필터링 후 남은 단어가 하나도 없을 때 (통글자 트랙이 성립하지 않는다)
    - 훅 단어와 모든 폴백이 표현 불가할 때 (시연이 성립하지 않는다)
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"$: cannot read word list at {source}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"$: {source} is not valid JSON: {exc}") from exc

    root = _require_mapping(raw, "$")
    available = {str(letter).upper() for letter in letters}

    entries = _require_array(
        _require_key(root, "words", "$"), "$.words", "an array of words"
    )
    if not entries:
        raise _fail("$.words", list(entries), "a non-empty array of words")

    normalized: List[str] = []
    seen: Dict[str, int] = {}
    for i, entry in enumerate(entries):
        word = _normalize_word(entry, f"$.words[{i}]")
        if word in seen:
            raise _fail(
                f"$.words[{i}]",
                word,
                f"a word not already listed at $.words[{seen[word]}] "
                f"(comparison is case-insensitive)",
            )
        seen[word] = i
        normalized.append(word)

    hook_preferred = _normalize_word(
        _require_key(root, "hook_word", "$"), "$.hook_word"
    )
    fallback_entries = _require_array(
        root.get("hook_fallbacks", []), "$.hook_fallbacks", "an array of words"
    )
    fallbacks = tuple(
        _normalize_word(entry, f"$.hook_fallbacks[{i}]")
        for i, entry in enumerate(fallback_entries)
    )

    kept: List[Word] = []
    excluded: List[ExcludedWord] = []
    for word in normalized:
        missing = _unsupported_letters(word, available)
        if missing:
            excluded.append(ExcludedWord(word=word, unsupported=missing))
            continue
        kept.append(
            Word(
                letters=word,
                has_repeat=_has_adjacent_repeat(word),
                length=len(word),
            )
        )

    if excluded:
        logger.warning(
            "%s: dropped %d of %d word(s) not expressible with the current "
            "letter set %s: %s",
            source,
            len(excluded),
            len(normalized),
            sorted(available),
            "; ".join(
                f"{item.word} (needs {', '.join(item.unsupported)})"
                for item in excluded
            ),
        )

    if not kept:
        raise _fail(
            "$.words",
            f"0 of {len(normalized)} words survived filtering",
            f"at least one word expressible with the current letter set "
            f"{sorted(available)} (the word track cannot run on an empty pool)",
        )

    hook_word = _resolve_hook_word(
        hook_preferred, fallbacks, available, {word.letters for word in kept}
    )

    return WordSet(
        words=tuple(kept),
        hook_word=hook_word,
        excluded=tuple(excluded),
        source_path=source,
    )
