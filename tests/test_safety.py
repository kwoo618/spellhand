"""안전 게이트와 세션 락 테스트 (PRD 10장, R4).

**이 파일의 테스트는 구현을 통과시키려고 완화하지 않는다.** clamp·세션 락은
최소 방어선이고, 여기가 뚫리면 하드웨어가 부서지거나 시연이 무너진다.
"""

import logging

import pytest

from server.db import (
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    TrainingSession,
    get_or_create_learner,
)
from server.device import Pose
from server.device.mock import MockDevice
from server.safety import (
    LOCK_EXPIRY_SECONDS,
    LOCK_HEARTBEAT_SECONDS,
    MAX_DRIVE_REQUESTS_PER_SESSION,
    PURPOSE_DEMO,
    CurriculumViolationError,
    DeviceNotIdleError,
    LockNotHeldError,
    LockUnavailableError,
    RateLimitError,
    SafetyError,
    SafetyGate,
    SessionLock,
    SessionQuotaExceededError,
    UnsupportedLettersError,
)

@pytest.fixture
def device(config, clock):
    dev = MockDevice(config, sleep=clock.sleep)
    dev.connect()
    return dev


@pytest.fixture
def lock(db, clock):
    return SessionLock(db, clock=clock)


@pytest.fixture
def gate(config, device, lock, clock):
    return SafetyGate(config, device, lock, clock=clock)


def make_session(db, name="haru"):
    with db.session() as session:
        learner = get_or_create_learner(session, name)
        row = TrainingSession(learner_id=learner.id, status="running", source="rule")
        session.add(row)
        session.commit()
        return row.id


@pytest.fixture
def session_id(db):
    return make_session(db)


@pytest.fixture
def held(lock, session_id):
    lock.acquire(session_id)
    return session_id


# --------------------------------------------------------------------------- #
# 세션 락 (R4)
# --------------------------------------------------------------------------- #

def test_lock_starts_unheld(lock):
    assert lock.holder() is None


def test_acquire_and_release(lock, session_id):
    lock.acquire(session_id)
    assert lock.holder() == session_id
    lock.release(session_id)
    assert lock.holder() is None


def test_a_second_session_cannot_take_a_live_lock(db, lock, session_id):
    other = make_session(db, name="mina")
    lock.acquire(session_id)
    with pytest.raises(LockUnavailableError) as exc:
        lock.acquire(other)
    assert exc.value.code == "LOCK_UNAVAILABLE"
    assert lock.holder() == session_id


def test_reacquiring_your_own_lock_is_allowed(lock, session_id):
    lock.acquire(session_id)
    lock.acquire(session_id)
    assert lock.holder() == session_id


def test_lock_expires_without_heartbeat(lock, clock, session_id):
    lock.acquire(session_id)
    clock.advance(LOCK_EXPIRY_SECONDS + 1)
    assert lock.holder() is None


def test_heartbeat_keeps_the_lock_alive(lock, clock, session_id):
    lock.acquire(session_id)
    for _ in range(10):
        clock.advance(LOCK_HEARTBEAT_SECONDS)
        lock.heartbeat(session_id)
    assert lock.holder() == session_id


def test_heartbeat_from_a_non_holder_is_rejected(db, lock, session_id):
    other = make_session(db, name="mina")
    lock.acquire(session_id)
    with pytest.raises(LockNotHeldError):
        lock.heartbeat(other)


def test_release_by_a_non_holder_does_nothing(db, lock, session_id):
    other = make_session(db, name="mina")
    lock.acquire(session_id)
    lock.release(other)
    assert lock.holder() == session_id


def test_expired_lock_can_be_taken_over(db, lock, clock, session_id):
    other = make_session(db, name="mina")
    lock.acquire(session_id)
    clock.advance(LOCK_EXPIRY_SECONDS + 1)
    lock.acquire(other)
    assert lock.holder() == other


def test_orphan_lock_cleanup(lock, clock, session_id, caplog):
    """서버 기동 시 고아 락 정리 (PRD 10장)."""
    lock.acquire(session_id)
    clock.advance(LOCK_EXPIRY_SECONDS + 1)

    with caplog.at_level(logging.WARNING, logger="server.safety"):
        cleared = lock.clear_expired()

    assert cleared == session_id
    assert lock.holder() is None
    assert any("orphan" in r.getMessage() for r in caplog.records)


def test_cleanup_leaves_a_live_lock_alone(lock, session_id):
    lock.acquire(session_id)
    assert lock.clear_expired() is None
    assert lock.holder() == session_id


# --------------------------------------------------------------------------- #
# 게이트 ① 세션 락 (R4)
# --------------------------------------------------------------------------- #

def test_drive_without_a_session_is_rejected(gate, session_id):
    """활성 세션 없이 들어온 구동 요청은 거부한다."""
    with pytest.raises(LockNotHeldError) as exc:
        gate.authorize_sequence(
            session_id, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True
        )
    assert exc.value.code == "LOCK_NOT_HELD"
    assert exc.value.http_status == 409


def test_drive_from_the_wrong_session_is_rejected(db, gate, lock, session_id):
    other = make_session(db, name="mina")
    lock.acquire(session_id)
    with pytest.raises(LockNotHeldError):
        gate.authorize_sequence(
            other, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True
        )


def test_drive_after_the_lock_expires_is_rejected(gate, lock, clock, session_id):
    lock.acquire(session_id)
    clock.advance(LOCK_EXPIRY_SECONDS + 1)
    with pytest.raises(LockNotHeldError):
        gate.authorize_sequence(
            session_id, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True
        )


# --------------------------------------------------------------------------- #
# 게이트 ② 글자 존재 (R4)
# --------------------------------------------------------------------------- #

def test_unsupported_letters_are_rejected(gate, held):
    """LLM 이 잘못된 글자를 내놓는 경로를 여기서 막는다."""
    with pytest.raises(UnsupportedLettersError) as exc:
        gate.authorize_sequence(
            held, "CAT", TRACK_DISCRIMINATION, depth_unlocked=True
        )
    assert exc.value.code == "UNSUPPORTED_LETTERS"
    assert exc.value.http_status == 400
    assert exc.value.letters == ("A", "C", "T")


def test_rejection_offers_the_supported_alternatives(gate, held, config):
    with pytest.raises(UnsupportedLettersError) as exc:
        gate.authorize_sequence(
            held, "CAB", TRACK_DISCRIMINATION, depth_unlocked=True
        )
    assert exc.value.alternatives == tuple(sorted(config.poses))


def test_one_bad_letter_blocks_the_whole_sequence(gate, held):
    with pytest.raises(UnsupportedLettersError):
        gate.authorize_sequence(
            held, "VWX", TRACK_DISCRIMINATION, depth_unlocked=True
        )


# --------------------------------------------------------------------------- #
# 게이트 ③ 커리큘럼 (R4)
# --------------------------------------------------------------------------- #

def test_locked_depth_pair_item_is_rejected(gate, held):
    """잠긴 상태에서 깊이쌍 출제 요청은 거부한다.

    조용히 다른 문항으로 바꾸지 않는다 — 무엇이 막혔는지 호출자가 알아야 한다.
    """
    with pytest.raises(CurriculumViolationError) as exc:
        gate.authorize_sequence(
            held, "EIB", TRACK_DISCRIMINATION, depth_unlocked=False
        )
    assert exc.value.code == "CURRICULUM_VIOLATION"
    assert exc.value.http_status == 400


def test_unlocked_depth_pair_item_passes(gate, held):
    poses = gate.authorize_sequence(
        held, "EIB", TRACK_DISCRIMINATION, depth_unlocked=True
    )
    assert len(poses) == 3


def test_demo_is_exempt_from_the_curriculum_check(gate, held):
    """시범은 채점 문항이 아니라 "이 둘이 이렇게 다르다"를 보여 주는 것이다."""
    poses = gate.authorize_sequence(
        held, "EI", TRACK_DISCRIMINATION, depth_unlocked=False, purpose=PURPOSE_DEMO
    )
    assert len(poses) == 2


def test_word_track_is_not_depth_gated(gate, held):
    """PRD 9.2: 깊이쌍 게이팅은 판별 트랙 제약이다."""
    poses = gate.authorize_sequence(
        held, "WISE", TRACK_WORD, depth_unlocked=False
    )
    assert len(poses) == 4


def test_an_unknown_track_is_rejected(gate, held):
    with pytest.raises(CurriculumViolationError):
        gate.authorize_sequence(held, "VWB", "practice", depth_unlocked=True)


# --------------------------------------------------------------------------- #
# 게이트 ④⑤ 자세와 각도
# --------------------------------------------------------------------------- #

def test_authorized_poses_match_the_config(gate, held, config):
    poses = gate.authorize_sequence(
        held, "BW", TRACK_DISCRIMINATION, depth_unlocked=True
    )
    assert [dict(p.flex) for p in poses] == [
        dict(config.poses["B"]),
        dict(config.poses["W"]),
    ]
    assert all(isinstance(p, Pose) for p in poses)


def test_authorized_poses_carry_no_letter(gate, held):
    """R2: 게이트를 통과한 뒤로는 글자가 내려가지 않는다."""
    poses = gate.authorize_sequence(
        held, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True
    )
    for pose in poses:
        assert not hasattr(pose, "letter")
        assert set(pose.flex) == {"thumb", "index", "middle", "ring", "pinky"}


def test_every_authorized_flex_is_inside_the_range(gate, held, clock, config):
    for letter in sorted(config.poses):
        (pose,) = gate.authorize_sequence(
            held, letter, TRACK_DISCRIMINATION, depth_unlocked=True
        )
        for value in pose.flex.values():
            assert 0.0 <= value <= 1.0
        clock.advance(config.timing.transition_ms / 1000)


# --------------------------------------------------------------------------- #
# 게이트 ⑥ rate limit (R4)
# --------------------------------------------------------------------------- #

def test_back_to_back_requests_are_rate_limited(gate, held, clock):
    gate.authorize_sequence(held, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True)
    with pytest.raises(RateLimitError) as exc:
        gate.authorize_sequence(
            held, "DOS", TRACK_DISCRIMINATION, depth_unlocked=True
        )
    assert exc.value.code == "RATE_LIMIT"
    assert exc.value.http_status == 429


def test_rate_limit_clears_after_the_transition_time(gate, held, clock, config):
    gate.authorize_sequence(held, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True)
    clock.advance(config.timing.transition_ms / 1000)
    gate.authorize_sequence(held, "DOS", TRACK_DISCRIMINATION, depth_unlocked=True)


def test_rate_limit_counts_requests_not_letters(gate, held, clock, config):
    """**요청 단위**로 센다. 글자 단위로 걸면 시퀀스가 스스로 차단된다.

    7글자 시퀀스 하나가 요청 1건이고, 그 안의 글자 수는 rate limit 과 무관하다.
    """
    gate.authorize_sequence(
        held, "BWVLYDF", TRACK_DISCRIMINATION, depth_unlocked=True
    )
    assert gate.requests_for(held) == 1

    clock.advance(config.timing.transition_ms / 1000)
    gate.authorize_sequence(held, "B", TRACK_DISCRIMINATION, depth_unlocked=True)
    assert gate.requests_for(held) == 2


def test_a_rejected_request_is_not_counted(gate, held, clock):
    gate.authorize_sequence(held, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True)
    with pytest.raises(RateLimitError):
        gate.authorize_sequence(
            held, "DOS", TRACK_DISCRIMINATION, depth_unlocked=True
        )
    assert gate.requests_for(held) == 1


# --------------------------------------------------------------------------- #
# 세션당 구동 요청 상한
# --------------------------------------------------------------------------- #

def test_session_quota_is_enforced(config, device, lock, clock, session_id):
    gate = SafetyGate(
        config, device, lock, clock=clock, max_requests_per_session=3
    )
    lock.acquire(session_id)
    for _ in range(3):
        gate.authorize_sequence(
            session_id, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True
        )
        clock.advance(config.timing.transition_ms / 1000)

    with pytest.raises(SessionQuotaExceededError) as exc:
        gate.authorize_sequence(
            session_id, "VWB", TRACK_DISCRIMINATION, depth_unlocked=True
        )
    assert exc.value.code == "SESSION_QUOTA"


def test_default_quota_matches_the_prd():
    assert MAX_DRIVE_REQUESTS_PER_SESSION == 200
    assert LOCK_HEARTBEAT_SECONDS == 15
    assert LOCK_EXPIRY_SECONDS == 60


# --------------------------------------------------------------------------- #
# 게이트 ⑦ 디바이스 idle
# --------------------------------------------------------------------------- #

def test_a_busy_device_is_rejected(config, lock, clock, session_id):
    """이동 중인 손에 새 요청을 보내지 않는다.

    게이트 순서상 ⑥ rate limit 이 ⑦ device idle 보다 먼저다 (PRD 10장). 그래서
    전이 시간이 지난 뒤에 다시 요청해야 ⑦ 에 도달한다 — 그 전에는 ⑥ 이 먼저
    막는다.
    """
    rejected = []
    elapsed = []

    def while_moving(seconds):
        clock.advance(seconds)
        elapsed.append(seconds)
        if rejected or sum(elapsed) * 1000 < config.timing.transition_ms:
            return
        try:
            gate.authorize_sequence(
                session_id, "DOS", TRACK_DISCRIMINATION, depth_unlocked=True
            )
        except DeviceNotIdleError as exc:
            rejected.append(exc)

    device = MockDevice(config, sleep=while_moving)
    device.connect()
    gate = SafetyGate(config, device, lock, clock=clock)
    lock.acquire(session_id)

    (pose,) = gate.authorize_sequence(
        session_id, "B", TRACK_DISCRIMINATION, depth_unlocked=True
    )
    device.move_to(pose, config.timing.transition_ms, config.timing.hold_min_ms)

    assert rejected, "이동 중 요청은 DeviceNotIdleError 여야 한다"
    assert rejected[0].code == "DEVICE_BUSY"
    assert rejected[0].http_status == 409


# --------------------------------------------------------------------------- #
# 구조화된 실패
# --------------------------------------------------------------------------- #

def test_every_rejection_is_a_safety_error():
    """상위 계층이 에러 문자열을 새로 지어내지 않도록 코드가 붙어 있다."""
    for error in (
        LockNotHeldError,
        LockUnavailableError,
        UnsupportedLettersError,
        CurriculumViolationError,
        RateLimitError,
        SessionQuotaExceededError,
        DeviceNotIdleError,
    ):
        assert issubclass(error, SafetyError)
        assert error.code != SafetyError.code
        assert isinstance(error.http_status, int)


def test_error_codes_are_unique():
    codes = [
        LockNotHeldError.code,
        LockUnavailableError.code,
        UnsupportedLettersError.code,
        CurriculumViolationError.code,
        RateLimitError.code,
        SessionQuotaExceededError.code,
        DeviceNotIdleError.code,
    ]
    assert len(set(codes)) == len(codes)
