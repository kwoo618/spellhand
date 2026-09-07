"""flex <-> 각도 변환 테스트 (PRD 17장 1순위).

여기서 확인하는 것은 세 가지다.
1. 실측 캘리브레이션에서 나오는 각도가 정확한가 (경계값·중간값)
2. 손가락마다 반대인 방향을 공식이 가정 없이 처리하는가
3. 클램프가 동작하고, 동작할 때 조용하지 않은가
"""

import copy
import json
import logging
from pathlib import Path

import pytest

from server.calibration import (
    angle_to_flex,
    angles_to_pose,
    flex_to_angle,
    pose_to_angles,
)
from server.config import FLEX_MAX, FLEX_MIN, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POSES = REPO_ROOT / "config" / "poses.json"

CALIBRATION_LOGGER = "server.calibration"


@pytest.fixture
def written(tmp_path):
    def _write(data):
        path = tmp_path / "poses.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    return _write


# --------------------------------------------------------------------------- #
# 경계값
# --------------------------------------------------------------------------- #

def test_flex_zero_gives_the_open_angle(config):
    """flex 0.0 = 완전히 폄 = open 각도."""
    for finger in config.fingers:
        assert flex_to_angle(config, finger, FLEX_MIN) == pytest.approx(
            config.calibration[finger].open
        ), finger


def test_flex_one_gives_the_closed_angle(config):
    """flex 1.0 = 안전 최대 굽힘 = closed 각도."""
    for finger in config.fingers:
        assert flex_to_angle(config, finger, FLEX_MAX) == pytest.approx(
            config.calibration[finger].closed
        ), finger


def test_flex_half_gives_the_midpoint(config):
    """flex 0.5 는 open 과 closed 의 정확한 중간.

    실측 calibration(poses.json)에서 직접 계산한 값이다.
      thumb  180 + 0.5*(80  - 180) = 130.0
      index    0 + 0.5*(120 -   0) =  60.0
      middle   0 + 0.5*(125 -   0) =  62.5
      ring     0 + 0.5*(105 -   0) =  52.5
      pinky  180 + 0.5*(80  - 180) = 130.0
    thumb/pinky 가 index 쪽보다 큰 값이 나오는 것은 두 손가락의 open 이
    반대편에 있기 때문이다. 이 표가 바뀌면 캘리브레이션이 바뀐 것이다.
    """
    expected = {
        "thumb": 130.0,
        "index": 60.0,
        "middle": 62.5,
        "ring": 52.5,
        "pinky": 130.0,
    }
    for finger, angle in expected.items():
        assert flex_to_angle(config, finger, 0.5) == pytest.approx(angle), finger


# --------------------------------------------------------------------------- #
# 방향 역전 — 공식에 abs() 나 방향 가정이 들어가면 즉시 깨진다
# --------------------------------------------------------------------------- #

def test_thumb_and_pinky_angle_decreases_as_flex_grows(config):
    """thumb/pinky 는 open(180) > closed(80) 이라 굽힐수록 각도가 줄어든다."""
    for finger in ("thumb", "pinky"):
        cal = config.calibration[finger]
        assert cal.open > cal.closed, f"{finger}: 캘리브레이션 전제가 바뀌었다"

        angles = [flex_to_angle(config, finger, f / 10) for f in range(11)]
        assert angles == sorted(angles, reverse=True), finger
        assert angles[0] > angles[-1], finger


def test_index_middle_ring_angle_increases_as_flex_grows(config):
    """index/middle/ring 은 open(0) < closed 라 굽힐수록 각도가 커진다."""
    for finger in ("index", "middle", "ring"):
        cal = config.calibration[finger]
        assert cal.open < cal.closed, f"{finger}: 캘리브레이션 전제가 바뀌었다"

        angles = [flex_to_angle(config, finger, f / 10) for f in range(11)]
        assert angles == sorted(angles), finger
        assert angles[0] < angles[-1], finger


def test_the_two_directions_really_are_opposite(config):
    """같은 flex 증가분에 대해 두 무리의 각도 변화 부호가 반대다."""
    delta = {
        finger: flex_to_angle(config, finger, 1.0)
        - flex_to_angle(config, finger, 0.0)
        for finger in config.fingers
    }
    assert delta["thumb"] < 0 and delta["pinky"] < 0
    assert delta["index"] > 0 and delta["middle"] > 0 and delta["ring"] > 0


# --------------------------------------------------------------------------- #
# 클램프
# --------------------------------------------------------------------------- #

def test_negative_flex_clamps_to_open_with_a_warning(config, caplog):
    with caplog.at_level(logging.WARNING, logger=CALIBRATION_LOGGER):
        angle = flex_to_angle(config, "index", -0.5)

    assert angle == pytest.approx(config.calibration["index"].open)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "클램프는 조용히 일어나면 안 된다"
    message = warnings[0].getMessage()
    assert "index" in message and "-0.5" in message


def test_flex_above_one_clamps_to_closed_with_a_warning(config, caplog):
    with caplog.at_level(logging.WARNING, logger=CALIBRATION_LOGGER):
        angle = flex_to_angle(config, "thumb", 1.5)

    assert angle == pytest.approx(config.calibration["thumb"].closed)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    message = warnings[0].getMessage()
    assert "thumb" in message and "1.5" in message


def test_the_boundaries_themselves_are_not_warnings(config, caplog):
    """0.0 과 1.0 은 정상값이다. 여기서 경고가 나면 오탐이다."""
    with caplog.at_level(logging.WARNING, logger=CALIBRATION_LOGGER):
        for finger in config.fingers:
            flex_to_angle(config, finger, FLEX_MIN)
            flex_to_angle(config, finger, FLEX_MAX)

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_out_of_range_angle_clamps_on_the_way_back(config, caplog):
    """역변환도 같은 방식으로 막는다."""
    with caplog.at_level(logging.WARNING, logger=CALIBRATION_LOGGER):
        flex = angle_to_flex(config, "index", 999.0)

    assert flex == pytest.approx(FLEX_MAX)
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


def test_clamped_angle_never_leaves_the_servo_range(config):
    """flex 가 아무리 이상해도 각도는 물리 한계를 넘지 않는다."""
    for finger in config.fingers:
        for flex in (-100.0, -0.5, 1.5, 100.0):
            angle = flex_to_angle(config, finger, flex)
            assert config.servo_range.min <= angle <= config.servo_range.max


# --------------------------------------------------------------------------- #
# 왕복
# --------------------------------------------------------------------------- #

def test_round_trip_for_every_letter(config):
    """11자 × 5손가락 flex -> angle -> flex 복원."""
    for letter, pose in config.poses.items():
        angles = pose_to_angles(config, pose)
        restored = angles_to_pose(config, angles)
        for finger in config.fingers:
            assert restored[finger] == pytest.approx(
                pose[finger], abs=1e-9
            ), f"{letter}.{finger}"


def test_round_trip_for_neutral_and_safe(config):
    for name, pose in (("neutral", config.neutral), ("safe", config.safe)):
        restored = angles_to_pose(config, pose_to_angles(config, pose))
        for finger in config.fingers:
            assert restored[finger] == pytest.approx(
                pose[finger], abs=1e-9
            ), f"{name}.{finger}"


# --------------------------------------------------------------------------- #
# 실측 검증
# --------------------------------------------------------------------------- #

def test_safe_pose_maps_to_every_open_angle(config):
    """안전 자세는 전 손가락 flex 0.0 이므로 각도가 곧 open 이다."""
    angles = pose_to_angles(config, config.safe)
    for finger in config.fingers:
        assert angles[finger] == pytest.approx(config.calibration[finger].open)


def test_every_letter_stays_inside_the_servo_range(config):
    for letter, pose in config.poses.items():
        for finger, angle in pose_to_angles(config, pose).items():
            assert (
                config.servo_range.min <= angle <= config.servo_range.max
            ), f"{letter}.{finger} = {angle}"


def test_no_clamp_warning_for_any_real_pose(config, caplog):
    """실측 자세 전체를 변환해도 경고가 하나도 나오면 안 된다.

    경고가 난다면 설정이 물리 한계를 넘었다는 뜻이고, 그것은 버그다.
    """
    with caplog.at_level(logging.WARNING, logger=CALIBRATION_LOGGER):
        for pose in list(config.poses.values()) + [config.neutral, config.safe]:
            pose_to_angles(config, pose)

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_angles_are_not_rounded(config):
    """반올림은 전송 직전 어댑터가 한다. 변환 계층은 float 를 그대로 넘긴다."""
    angle = flex_to_angle(config, "middle", 0.5)
    assert isinstance(angle, float)
    assert angle == pytest.approx(62.5)
    assert angle != int(angle), "반올림되면 62.5 가 62 나 63 이 된다"


# --------------------------------------------------------------------------- #
# 주입 — 전역 의존이 없는지
# --------------------------------------------------------------------------- #

def test_works_with_an_artificial_calibration(written):
    """실측값과 무관한 캘리브레이션으로도 동작한다.

    모듈이 실제 poses.json 을 몰래 참조하고 있으면 여기서 깨진다.
    """
    with REAL_POSES.open(encoding="utf-8") as fh:
        data = json.load(fh)
    data = copy.deepcopy(data)
    for finger in data["calibration"]["fingers"]:
        data["calibration"]["fingers"][finger] = {"open": 10, "closed": 20}

    config = load_config(written(data))

    for finger in config.fingers:
        assert flex_to_angle(config, finger, 0.0) == pytest.approx(10.0)
        assert flex_to_angle(config, finger, 0.5) == pytest.approx(15.0)
        assert flex_to_angle(config, finger, 1.0) == pytest.approx(20.0)
        assert angle_to_flex(config, finger, 15.0) == pytest.approx(0.5)


def test_unknown_finger_is_rejected(config):
    with pytest.raises(KeyError) as exc:
        flex_to_angle(config, "toe", 0.5)
    assert "toe" in str(exc.value)


def test_pose_with_missing_finger_is_rejected(config):
    incomplete = dict(config.poses["B"])
    del incomplete["ring"]
    with pytest.raises(ValueError) as exc:
        pose_to_angles(config, incomplete)
    assert "ring" in str(exc.value)
