"""Pose 계약과 MockDevice 동작 테스트.

시간은 전부 주입한다. 이 파일 전체가 초 단위로 걸리면 주입이 새고 있는 것이다.
"""

import pytest

from server.device import (
    PERMILLE_MAX,
    DeviceBusyError,
    DeviceCancelledError,
    DeviceError,
    DeviceNotConnectedError,
    DeviceProtocolError,
    Pose,
    flex_to_permille,
    pose_to_permille,
)
from server.device.mock import MockDevice

class Clock:
    """즉시 반환하는 가짜 sleep. 호출될 때마다 훅을 실행한다."""

    def __init__(self, on_tick=None):
        self.slept = []
        self.on_tick = on_tick

    def __call__(self, seconds):
        self.slept.append(seconds)
        if self.on_tick is not None:
            self.on_tick(len(self.slept))

    @property
    def total_seconds(self):
        return sum(self.slept)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def device(config, clock):
    dev = MockDevice(config, sleep=clock)
    dev.connect()
    return dev


def pose_of(config, letter):
    return Pose.from_config(config, config.poses[letter])


# --------------------------------------------------------------------------- #
# Pose
# --------------------------------------------------------------------------- #

def test_pose_rejects_flex_above_one(config):
    values = dict(config.poses["B"])
    values["index"] = 1.4
    with pytest.raises(ValueError) as exc:
        Pose.from_config(config, values)
    assert "index" in str(exc.value)


def test_pose_rejects_negative_flex(config):
    values = dict(config.poses["B"])
    values["thumb"] = -0.1
    with pytest.raises(ValueError):
        Pose.from_config(config, values)


def test_pose_rejects_a_missing_finger(config):
    values = dict(config.poses["B"])
    del values["pinky"]
    with pytest.raises(ValueError) as exc:
        Pose.from_config(config, values)
    assert "pinky" in str(exc.value)


def test_pose_rejects_an_unknown_finger(config):
    values = dict(config.poses["B"])
    values["toe"] = 0.5
    with pytest.raises(ValueError) as exc:
        Pose.from_config(config, values)
    assert "toe" in str(exc.value)


def test_pose_is_immutable(config):
    pose = pose_of(config, "B")

    with pytest.raises(TypeError):
        pose.flex["thumb"] = 0.0
    with pytest.raises(Exception):
        pose.flex = {}


def test_pose_does_not_carry_a_letter_name(config):
    """R2: Pose 는 flex 만 담는다. 어떤 글자인지는 알지 못한다."""
    pose = pose_of(config, "B")
    assert set(pose.flex) == set(config.fingers)
    assert not hasattr(pose, "letter")
    assert not hasattr(pose, "name")


def test_pose_copies_the_source_mapping(config):
    values = dict(config.poses["B"])
    pose = Pose.from_config(config, values)
    values["thumb"] = 0.0
    assert pose.flex["thumb"] == config.poses["B"]["thumb"]


# --------------------------------------------------------------------------- #
# 퍼밀 변환
# --------------------------------------------------------------------------- #

def test_permille_endpoints():
    assert flex_to_permille(0.0) == 0
    assert flex_to_permille(1.0) == PERMILLE_MAX == 1000


def test_permille_of_a_real_flex_value():
    assert flex_to_permille(0.83) == 830
    assert flex_to_permille(0.76) == 760
    assert flex_to_permille(0.48) == 480


def test_permille_rounds_to_the_nearest_integer():
    # 실제 설정값은 소수 둘째 자리까지라 정확히 떨어지지만, 보간 중간값은
    # 그렇지 않다. 경계 양쪽이 각각 어느 쪽으로 가는지 고정해 둔다.
    assert flex_to_permille(0.0004) == 0
    assert flex_to_permille(0.0006) == 1
    assert flex_to_permille(0.9994) == 999
    assert flex_to_permille(0.9996) == 1000


def test_permille_clamps_out_of_range_input():
    assert flex_to_permille(-1.0) == 0
    assert flex_to_permille(2.0) == PERMILLE_MAX


def test_pose_to_permille_covers_every_finger(config):
    values = pose_to_permille(pose_of(config, "I"))
    assert set(values) == set(config.fingers)
    assert values["index"] == 830
    assert values["pinky"] == 0
    assert all(0 <= v <= PERMILLE_MAX for v in values.values())


def test_pose_to_permille_rejects_a_letter_name(config):
    """R2: 퍼밀 변환도 Pose 만 받는다."""
    with pytest.raises(TypeError):
        pose_to_permille("B")


# --------------------------------------------------------------------------- #
# MockDevice — 연결과 상태
# --------------------------------------------------------------------------- #

def test_starts_at_the_safe_pose_not_neutral(config, clock):
    """연결 직후는 안전 자세다. 중립이 아니다 (R3)."""
    device = MockDevice(config, sleep=clock)
    device.connect()

    assert device.current_flex() == dict(config.safe)
    assert device.current_flex() != dict(config.neutral)
    assert device.is_busy is False


def test_calls_before_connect_are_rejected(config, clock):
    device = MockDevice(config, sleep=clock)
    pose = pose_of(config, "B")

    with pytest.raises(DeviceNotConnectedError):
        device.move_to(pose, transition_ms=100, hold_ms=0)
    with pytest.raises(DeviceNotConnectedError):
        device.neutral()
    with pytest.raises(DeviceNotConnectedError):
        device.safe()
    with pytest.raises(DeviceNotConnectedError):
        device.stop()


def test_calls_after_disconnect_are_rejected(device, config):
    device.disconnect()
    with pytest.raises(DeviceNotConnectedError):
        device.move_to(pose_of(config, "B"), transition_ms=100, hold_ms=0)


# --------------------------------------------------------------------------- #
# MockDevice — 이동
# --------------------------------------------------------------------------- #

def test_move_to_reaches_the_target_exactly(device, config):
    target = pose_of(config, "I")
    device.move_to(target, transition_ms=900, hold_ms=0)

    assert device.current_flex() == dict(target.flex)
    assert device.is_busy is False


def test_move_to_consumes_transition_and_hold_time(device, config, clock):
    device.move_to(pose_of(config, "B"), transition_ms=900, hold_ms=3000)

    tick_ms = config.timing.interpolation_tick_ms
    expected_ticks = 900 // tick_ms
    # 보간 틱 + 유지 1회
    assert len(clock.slept) == expected_ticks + 1
    assert clock.total_seconds == pytest.approx((900 + 3000) / 1000)


def test_device_is_busy_while_moving(config):
    busy_during = []

    def record(seconds):
        busy_during.append(device.is_busy)

    device = MockDevice(config, sleep=record)
    device.connect()
    device.move_to(pose_of(config, "B"), transition_ms=100, hold_ms=0)

    assert busy_during and all(busy_during), "이동 중에는 is_busy 가 True 여야 한다"
    assert device.is_busy is False


def test_second_move_while_busy_raises(config):
    """이동 중 새 요청은 DeviceBusyError.

    주입한 sleep 안에서 다시 부르면, 아직 첫 이동이 끝나지 않은 시점을
    스레드 없이 정확히 잡을 수 있다.
    """
    raised = []

    def reentrant(seconds):
        if raised:
            return
        try:
            device.move_to(pose_of(config, "W"), transition_ms=100, hold_ms=0)
        except DeviceBusyError as exc:
            raised.append(exc)

    device = MockDevice(config, sleep=reentrant)
    device.connect()
    device.move_to(pose_of(config, "B"), transition_ms=100, hold_ms=0)

    assert raised, "이동 중 재호출은 DeviceBusyError 여야 한다"
    assert device.current_flex() == dict(config.poses["B"])


def test_move_to_rejects_a_letter_name(device):
    """R2 고정: 어댑터에 글자 이름을 넘길 수 없다."""
    with pytest.raises(TypeError) as exc:
        device.move_to("B", transition_ms=900, hold_ms=0)
    assert "Pose" in str(exc.value)


def test_move_to_rejects_a_plain_dict(device, config):
    """dict 도 안 된다. Pose 로 감싸야 검증을 거친다."""
    with pytest.raises(TypeError):
        device.move_to(dict(config.poses["B"]), transition_ms=900, hold_ms=0)


# --------------------------------------------------------------------------- #
# MockDevice — 보간
# --------------------------------------------------------------------------- #

def test_interpolation_passes_through_intermediate_values(config):
    """중간 시점의 flex 는 시작과 목표 사이에 있다."""
    samples = []
    device = MockDevice(config, sleep=lambda s: samples.append(device.current_flex()))
    device.connect()

    start = dict(config.safe)
    target = pose_of(config, "I")
    device.move_to(target, transition_ms=900, hold_ms=0)

    # 샘플은 sleep 직전에 찍히므로 i 번째 샘플은 i 번째 틱이 적용되기 **전** 상태다.
    # 보간 틱 45회 + 유지 1회 = 46회 호출이고, 마지막 샘플이 목표 도달 상태다.
    ticks = 900 // config.timing.interpolation_tick_ms
    assert len(samples) == ticks + 1

    moving_finger = "index"  # safe 0.00 -> I 0.83
    values = [s[moving_finger] for s in samples]
    assert values[0] == pytest.approx(start[moving_finger])
    assert any(
        start[moving_finger] < v < target.flex[moving_finger] for v in values
    ), "중간값이 하나도 없으면 보간이 아니라 점프다"
    assert values == sorted(values), "단조 증가해야 한다"


def test_interpolation_never_leaves_the_flex_range(config):
    """보간 중 어느 시점에도 flex 가 0.0~1.0 밖으로 나가지 않는다."""
    samples = []
    device = MockDevice(config, sleep=lambda s: samples.append(device.current_flex()))
    device.connect()

    previous = pose_of(config, "S")
    device.move_to(previous, transition_ms=900, hold_ms=0)
    for letter in sorted(config.poses):
        device.move_to(pose_of(config, letter), transition_ms=900, hold_ms=0)

    assert samples
    for snapshot in samples:
        for finger, value in snapshot.items():
            assert 0.0 <= value <= 1.0, f"{finger} = {value}"


def test_a_transition_shorter_than_one_tick_still_arrives(device, config):
    target = pose_of(config, "B")
    device.move_to(target, transition_ms=0, hold_ms=0)
    assert device.current_flex() == dict(target.flex)


# --------------------------------------------------------------------------- #
# MockDevice — 중립 / 안전 / 정지
# --------------------------------------------------------------------------- #

def test_neutral_and_safe_produce_different_poses(device, config):
    """R3 고정: 둘은 별개 자세다. 하나가 다른 하나를 대신하지 않는다."""
    device.neutral()
    neutral_flex = device.current_flex()

    device.safe()
    safe_flex = device.current_flex()

    assert neutral_flex == dict(config.neutral)
    assert safe_flex == dict(config.safe)
    assert neutral_flex != safe_flex


def test_stop_lands_on_the_safe_pose(device, config):
    device.move_to(pose_of(config, "S"), transition_ms=900, hold_ms=0)
    device.stop()

    assert device.current_flex() == dict(config.safe)
    assert device.is_busy is False


def test_stop_during_a_move_abandons_the_target(config):
    """정지가 걸리면 진행 중이던 이동은 목표에 도달하지 않는다.

    그리고 **조용히 끝나지 않는다.** 완료처럼 반환하면 상위 계층이 손이 중간에
    멈춘 문항을 학습자에게 내민다. SerialDevice 와 같은 예외를 던진다
    (DECISIONS.md §42).
    """
    stopped = []

    def stop_on_first_tick(seconds):
        if not stopped:
            stopped.append(True)
            device.stop()

    device = MockDevice(config, sleep=stop_on_first_tick)
    device.connect()
    with pytest.raises(DeviceCancelledError):
        device.move_to(pose_of(config, "S"), transition_ms=900, hold_ms=0)

    assert device.current_flex() == dict(config.safe)
    assert device.current_flex() != dict(config.poses["S"])
    assert device.is_busy is False


def test_a_cancelled_move_is_not_a_communication_error(config):
    """취소는 통신 오류가 아니다. 상위 계층이 둘을 구분할 수 있어야 한다."""
    assert issubclass(DeviceCancelledError, DeviceError)
    assert not issubclass(DeviceCancelledError, DeviceProtocolError)


def test_stop_during_the_hold_also_raises(config):
    """도착 후 유지 중에 정지가 걸려도 마찬가지다."""
    ticks = []

    def stop_after_arrival(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:
            # 전환 1틱 + 유지 sleep. 유지 sleep 에서 정지가 걸린다.
            device.stop()

    device = MockDevice(config, sleep=stop_after_arrival)
    device.connect()
    with pytest.raises(DeviceCancelledError):
        device.move_to(pose_of(config, "S"), transition_ms=1, hold_ms=900)


def test_a_new_move_after_stop_is_accepted(device, config):
    device.stop()
    device.move_to(pose_of(config, "W"), transition_ms=100, hold_ms=0)
    assert device.current_flex() == dict(config.poses["W"])
