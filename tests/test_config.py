"""config/poses.json 과 server.config 로더에 대한 검증 테스트.

파괴 케이스는 실제 poses.json 을 deepcopy 해 한 곳만 망가뜨린 뒤 임시 파일로
저장하는 방식이다. 실제 설정 파일은 절대 수정하지 않는다.
"""

import copy
import json
from pathlib import Path

import pytest

from server.config import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = REPO_ROOT / "config" / "poses.json"

LETTERS = ("B", "D", "E", "F", "I", "L", "O", "S", "V", "W", "Y")


@pytest.fixture
def raw():
    """실제 poses.json 의 내용. 테스트마다 새 deepcopy 를 준다."""
    with REAL_CONFIG.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def broken(raw, tmp_path):
    """raw 를 변형해 임시 파일로 저장하고 그 경로를 돌려주는 헬퍼."""

    def _write(mutate):
        data = copy.deepcopy(raw)
        mutate(data)
        path = tmp_path / "poses.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    return _write


# --------------------------------------------------------------------------- #
# 정상 경로
# --------------------------------------------------------------------------- #

def test_real_config_loads(config):
    assert config.schema_version == 2
    assert config.device_id == "hand-01"
    assert config.fingers == ("thumb", "index", "middle", "ring", "pinky")
    assert config.source_path == REAL_CONFIG


def test_poses_contain_exactly_the_eleven_letters(config):
    assert tuple(sorted(config.poses)) == LETTERS


def test_confusion_pairs_count_and_depth_subset(config):
    assert len(config.confusion_pairs) == 15
    depth = config.depth_pairs()
    assert len(depth) == 3
    assert {frozenset(p.pair) for p in depth} == {
        frozenset(("E", "I")),
        frozenset(("O", "S")),
        frozenset(("E", "O")),
    }


def test_every_confusion_pair_has_a_hint(config):
    for pair in config.confusion_pairs:
        assert pair.hint.strip()


def test_neutral_and_safe_are_different_poses(config):
    # 중립(글자 경계 신호)과 안전(부팅·타임아웃·비상정지 복귀)은 목적이 다른
    # 별개 자세다. 같은 값으로 합치면 경계 신호가 사라진다.
    assert config.neutral != config.safe
    assert config.distinct_fingers(config.neutral, config.safe) == ("middle",)


def test_calibration_covers_every_finger(config):
    assert set(config.calibration) == set(config.fingers)
    for finger, cal in config.calibration.items():
        assert cal.open != cal.closed, finger


def test_servo_range_comes_from_the_config_file(config):
    # 서보 가동범위는 하드웨어 종속값이라 코드가 아니라 poses.json 에서 온다.
    # 손을 바꾸면 이 값도 바뀌므로 코드는 손대지 않는다.
    assert config.servo_range.min < config.servo_range.max
    for finger, cal in config.calibration.items():
        for angle in (cal.open, cal.closed):
            assert config.servo_range.min <= angle <= config.servo_range.max, finger


# --------------------------------------------------------------------------- #
# 경계값 회귀 테스트
# --------------------------------------------------------------------------- #

def test_neutral_vs_b_is_exactly_the_minimum_margin(config):
    """중립 vs B 는 정확히 2개 손가락(엄지·중지) 차이 — 전체에서 최소 마진.

    검지·약지·소지는 중립과 B 가 모두 0.00 으로 완전히 동일하다. 즉 B 는
    validation.neutral_min_distinct_fingers(=2) 를 딱 맞추고 있어서, 중립
    자세나 B 자세를 조금이라도 건드리면 곧바로 로딩이 실패해야 한다.
    이 테스트는 그 사고를 즉시 잡으라고 존재한다.
    """
    distinct = config.distinct_fingers(config.neutral, config.poses["B"])
    assert distinct == ("thumb", "middle")
    assert len(distinct) == config.validation.neutral_min_distinct_fingers

    for finger in ("index", "ring", "pinky"):
        assert config.neutral[finger] == config.poses["B"][finger] == 0.00


def test_neutral_vs_other_letters_has_at_least_three_distinct_fingers(config):
    """B 를 뺀 나머지 10자는 중립과 3개 이상 차이 — 여유가 있는 쪽."""
    for letter in LETTERS:
        if letter == "B":
            continue
        distinct = config.distinct_fingers(config.neutral, config.poses[letter])
        assert len(distinct) >= 3, f"{letter}: {distinct}"


def test_no_two_letters_are_indistinguishable(config):
    """min_finger_delta 기준으로 완전히 겹치는 글자 쌍이 없어야 한다."""
    letters = sorted(config.poses)
    for i, a in enumerate(letters):
        for b in letters[i + 1:]:
            distinct = config.distinct_fingers(config.poses[a], config.poses[b])
            assert distinct, f"{a} and {b} are indistinguishable"


# --------------------------------------------------------------------------- #
# 파괴 경로 — 전부 로딩에 실패해야 한다
# --------------------------------------------------------------------------- #

def test_flex_above_one_is_rejected(broken):
    def mutate(data):
        data["poses"]["B"]["index"] = 1.4

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.poses.B.index" in str(exc.value)
    assert "1.4" in str(exc.value)


def test_missing_finger_key_is_rejected(broken):
    def mutate(data):
        del data["poses"]["V"]["ring"]

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.poses.V" in str(exc.value)
    assert "ring" in str(exc.value)


def test_extra_finger_key_is_rejected(broken):
    def mutate(data):
        data["poses"]["V"]["thmub"] = 0.70

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "thmub" in str(exc.value)


def test_non_zero_safe_pose_is_rejected(broken):
    def mutate(data):
        data["safe"]["pinky"] = 0.30

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.safe.pinky" in str(exc.value)
    assert "0.3" in str(exc.value)


def test_confusion_pair_with_unknown_letter_is_rejected(broken):
    def mutate(data):
        data["confusion_pairs"].append(
            {
                "pair": ["C", "B"],
                "axis": "count",
                "depth": False,
                "hint": "존재하지 않는 글자",
            }
        )

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "'C'" in str(exc.value)


def test_reversed_duplicate_confusion_pair_is_rejected(broken):
    def mutate(data):
        # ["V","W"] 가 이미 있으므로 ["W","V"] 는 같은 쌍이다.
        data["confusion_pairs"].append(
            {
                "pair": ["W", "V"],
                "axis": "count",
                "depth": False,
                "hint": "중복 쌍",
            }
        )

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "confusion_pairs" in str(exc.value)


def test_self_confusion_pair_is_rejected(broken):
    def mutate(data):
        data["confusion_pairs"].append(
            {"pair": ["B", "B"], "axis": "count", "depth": False, "hint": "자기쌍"}
        )

    with pytest.raises(ConfigError):
        load_config(broken(mutate))


def test_empty_hint_is_rejected(broken):
    def mutate(data):
        data["confusion_pairs"][0]["hint"] = "  "

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "hint" in str(exc.value)


def test_hold_min_greater_than_hold_max_is_rejected(broken):
    def mutate(data):
        data["timing"]["hold_min_ms"] = 5000  # hold_max_ms(3000) 보다 크다

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "hold_min_ms" in str(exc.value)


def test_transition_slower_than_slow_transition_is_rejected(broken):
    def mutate(data):
        data["timing"]["transition_ms"] = data["timing"]["transition_slow_ms"] + 1

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "transition_ms" in str(exc.value)


def test_settled_observation_without_settle_time_is_rejected(broken):
    def mutate(data):
        data["timing"]["observe_from"] = "settled"
        data["timing"]["settle_ms"] = 0

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "settle_ms" in str(exc.value)


def test_unknown_observe_from_is_rejected(broken):
    def mutate(data):
        data["timing"]["observe_from"] = "whenever"

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "observe_from" in str(exc.value)


def test_schema_version_one_is_rejected(broken):
    def mutate(data):
        data["schema_version"] = 1

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.schema_version" in str(exc.value)
    assert "1" in str(exc.value)


def test_neutral_too_close_to_a_letter_is_rejected(broken):
    def mutate(data):
        # 중립의 중지를 B 와 같게 만들면 B 와 엄지 하나 차이만 남는다.
        data["neutral"]["middle"] = 0.00

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.neutral vs $.poses.B" in str(exc.value)


def test_duplicate_poses_are_rejected(broken):
    def mutate(data):
        data["poses"]["O"] = dict(data["poses"]["D"])

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "indistinguishable" in str(exc.value) or "0 distinct" in str(exc.value)


def test_calibration_with_equal_endpoints_is_rejected(broken):
    def mutate(data):
        fingers = data["calibration"]["fingers"]
        fingers["index"]["closed"] = fingers["index"]["open"]

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.calibration.fingers.index" in str(exc.value)


def test_calibration_key_mismatch_is_rejected(broken):
    def mutate(data):
        fingers = data["calibration"]["fingers"]
        fingers["pinkie"] = fingers.pop("pinky")

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.calibration.fingers" in str(exc.value)


def test_angle_above_servo_range_is_rejected(broken):
    def mutate(data):
        top = data["calibration"]["servo_range_deg"]["max"]
        data["calibration"]["fingers"]["thumb"]["open"] = top + 1

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.calibration.fingers.thumb.open" in str(exc.value)


def test_angle_below_servo_range_is_rejected(broken):
    def mutate(data):
        floor = data["calibration"]["servo_range_deg"]["min"]
        data["calibration"]["fingers"]["index"]["open"] = floor - 1

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.calibration.fingers.index.open" in str(exc.value)


def test_inverted_servo_range_is_rejected(broken):
    def mutate(data):
        rng = data["calibration"]["servo_range_deg"]
        rng["min"], rng["max"] = rng["max"], rng["min"]

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.calibration.servo_range_deg.min" in str(exc.value)


def test_degenerate_servo_range_is_rejected(broken):
    def mutate(data):
        rng = data["calibration"]["servo_range_deg"]
        rng["min"] = rng["max"]

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.calibration.servo_range_deg.min" in str(exc.value)


def test_missing_servo_range_is_rejected(broken):
    def mutate(data):
        del data["calibration"]["servo_range_deg"]

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "servo_range_deg" in str(exc.value)


def test_a_narrower_servo_range_rejects_the_real_angles(broken):
    """가동범위를 좁히면 실측 각도가 걸린다 — 범위가 실제로 쓰이는지 확인."""

    def mutate(data):
        # 상한을 손가락 최대 각도 아래로 내린다.
        angles = [
            v
            for finger in data["calibration"]["fingers"].values()
            for v in finger.values()
        ]
        data["calibration"]["servo_range_deg"]["max"] = max(angles) - 1

    with pytest.raises(ConfigError) as exc:
        load_config(broken(mutate))
    assert "$.calibration.fingers." in str(exc.value)


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")
