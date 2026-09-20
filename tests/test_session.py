"""세션 루프와 적응 테스트 (PRD 9.4, 기획서 5.2).

완료 기준: Mock 으로 판별 8 + 단어 4 세션이 끝까지 돌고 요약이 트랙별로 나온다.

시간은 전부 주입한다. 가짜 sleep 이 가짜 시계를 밀고, 게이트와 세션이 같은
시계를 본다 — 그래야 rate limit 이 실제 운용과 같은 조건에서 검사된다.
"""

import random
from pathlib import Path

import pytest

from server.config import load_config, load_words
from server.db import (
    BOUNDARY_DIRECT,
    BOUNDARY_EXPLICIT,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    Item,
    TrainingSession,
)
from server.device import DeviceProtocolError
from server.device.mock import MockDevice
from server.items import Difficulty
from server.safety import LockUnavailableError, SafetyGate, SessionLock
from server.session import (
    DIFFICULTY_LADDER,
    HOLD_DECAY_ON_CORRECT,
    ItemPrompt,
    RuleAdaptation,
    SessionController,
    SessionPlan,
    SessionRunner,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POSES = REPO_ROOT / "config" / "poses.json"
REAL_WORDS = REPO_ROOT / "config" / "words.json"


@pytest.fixture
def words(config):
    return load_words(REAL_WORDS, letters=config.poses)


@pytest.fixture
def device(config, clock):
    dev = MockDevice(config, sleep=clock.sleep)
    dev.connect()
    return dev


@pytest.fixture
def runner(config, words, db, device, clock):
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    return SessionRunner(
        config,
        words,
        db,
        device,
        gate,
        lock,
        clock=clock,
        rng=random.Random(20260907),
    )


def always_correct(prompt: ItemPrompt, expected: str) -> str:
    return expected


def always_wrong(prompt: ItemPrompt, expected: str) -> str:
    """항상 첫 글자를 다른 글자로 바꿔 답한다 — `wrong` 이 나오도록."""
    replacement = "B" if expected[0] != "B" else "W"
    return replacement + expected[1:]


# --------------------------------------------------------------------------- #
# 통합 — 완료 기준
# --------------------------------------------------------------------------- #

def test_a_full_twelve_item_session_runs_to_the_end(runner, db):
    """판별 8 + 단어 4 가 끝까지 돌고 트랙별 요약이 나온다."""
    summary = runner.run("haru", always_correct)

    discrimination = summary.by_track[TRACK_DISCRIMINATION]
    word = summary.by_track[TRACK_WORD]

    assert discrimination.answered == 8
    assert discrimination.correct == 8
    assert discrimination.accuracy == 1.0

    assert word.answered == 4
    assert word.correct == 4
    assert word.accuracy == 1.0

    assert len(summary.outcomes) == 12
    assert [o.seq for o in summary.outcomes] == list(range(1, 13))


def test_the_summary_is_only_ever_per_track(runner):
    """통합 정확도는 존재하지 않는다."""
    summary = runner.run("haru", always_correct)
    assert set(summary.by_track) == {TRACK_DISCRIMINATION, TRACK_WORD}
    for forbidden in ("accuracy", "overall", "total"):
        assert not hasattr(summary, forbidden)


def test_the_session_is_marked_done(runner, db):
    summary = runner.run("haru", always_correct)
    with db.session() as session:
        row = session.get(TrainingSession, summary.session_id)
    assert row.status == "done"
    assert row.ended_at is not None
    assert row.plan_json


def test_the_lock_is_released_at_the_end(config, words, db, device, clock):
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    runner = SessionRunner(
        config, words, db, device, gate, lock, clock=clock,
        rng=random.Random(1),
    )
    runner.run("haru", always_correct)
    assert lock.holder() is None


def test_the_lock_is_released_when_a_move_fails_mid_session(
    config, words, db, clock
):
    """R4: 구동 중 예외가 나도 락은 반납된다 (DECISIONS.md §42).

    가짜 시계라 만료로 풀릴 길이 없다. 명시적으로 놓아야만 통과한다.
    """
    device = MockDevice(config, sleep=clock.sleep)
    device.connect()
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    runner = SessionRunner(
        config, words, db, device, gate, lock, clock=clock,
        rng=random.Random(1),
    )

    def explode(pose, transition_ms, hold_ms):
        raise DeviceProtocolError("expected ACK, got 'RDY -1'")

    device.move_to = explode  # type: ignore[method-assign]

    with pytest.raises(DeviceProtocolError):
        runner.run("haru", always_correct)
    assert lock.holder() is None


def test_the_lock_is_released_when_the_hand_cannot_be_parked(
    config, words, db, clock
):
    """정리 단계(안전 자세)가 실패해도 락은 놓는다. 포트가 이미 죽은 경우다."""
    device = MockDevice(config, sleep=clock.sleep)
    device.connect()
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    controller = SessionController(
        config, words, db, device, gate, lock, clock=clock,
        rng=random.Random(1),
    )
    session_id = controller.start("haru")

    def explode():
        raise DeviceProtocolError("the port is gone")

    device.safe = explode  # type: ignore[method-assign]

    with pytest.raises(DeviceProtocolError):
        controller.end(status="aborted")
    assert lock.holder() is None

    # 두 번째 end() 가 세션을 다시 닫지 않는다.
    controller.end()
    assert lock.holder() is None


def test_a_failed_start_does_not_leave_the_lock_held(config, words, db, clock):
    """락을 잡은 뒤 준비가 실패하면 놓고 나간다.

    호출자는 session_id 를 받지 못하므로 이 세션을 끝낼 방법이 없다. 여기서
    놓지 않으면 만료까지 아무도 손을 못 쓴다 (DECISIONS.md §42).
    """
    device = MockDevice(config, sleep=clock.sleep)
    device.connect()
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    controller = SessionController(
        config, words, db, device, gate, lock, clock=clock,
        rng=random.Random(1),
    )

    def explode(learner_id, track):
        raise RuntimeError("the difficulty table is unreadable")

    controller._restore_difficulty = explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        controller.start("haru")
    assert lock.holder() is None


def test_the_hand_rests_in_the_safe_pose_afterwards(runner, device, config):
    """세션이 끝나면 손은 안전 자세로 돌아간다 (R3)."""
    runner.run("haru", always_correct)
    assert device.current_flex() == dict(config.safe)
    assert device.current_flex() != dict(config.neutral)


def test_two_sessions_can_run_one_after_another(runner):
    first = runner.run("haru", always_correct)
    second = runner.run("haru", always_correct)
    assert second.session_id != first.session_id
    assert second.by_track[TRACK_DISCRIMINATION].answered == 8


def test_a_second_learner_cannot_run_while_the_lock_is_held(
    config, words, db, device, clock
):
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    runner = SessionRunner(
        config, words, db, device, gate, lock, clock=clock, rng=random.Random(2)
    )

    # 다른 세션이 락을 쥐고 있는 상태를 만든다.
    with db.session() as session:
        from server.db import get_or_create_learner

        other_learner = get_or_create_learner(session, "mina")
        other = TrainingSession(
            learner_id=other_learner.id, status="running", source="manual"
        )
        session.add(other)
        session.commit()
        other_id = other.id
    lock.acquire(other_id)

    from server.safety import LockUnavailableError

    with pytest.raises(LockUnavailableError):
        runner.run("haru", always_correct)


# --------------------------------------------------------------------------- #
# 세션 행 누수 회귀 (DECISIONS §49) — 이 테스트는 완화하지 않는다 (R4)
# --------------------------------------------------------------------------- #
#
# `_open_session()` 이 행을 넣은 뒤 `lock.acquire()` 가 거부되면, 그 행이
# `running` 인 채 남는다. 남은 행은 다음 승인에서 또 락을 물어 `409` 를
# 영구화한다 — 승인이 실패할 때마다 한 줄씩 쌓인다.
#
# 아래 테스트는 전부 가짜 시계를 쓴다. 만료(60초)로 우연히 풀려서 통과하는
# 길이 없다 — 락이 실제로 물려 있는 상태에서만 검사된다.

def hold_the_lock_with_another_session(db, lock, name="mina"):
    """다른 세션이 락을 쥔 상태를 만들고 그 세션 id 를 돌려준다."""
    from server.db import get_or_create_learner

    with db.session() as session:
        learner = get_or_create_learner(session, name)
        other = TrainingSession(
            learner_id=learner.id, status="running", source="manual"
        )
        session.add(other)
        session.commit()
        other_id = other.id
    lock.acquire(other_id)
    return other_id


def running_session_ids(db, ignore=()):
    """지금 `running` 인 세션 행. 설정용으로 만든 행은 빼고 센다."""
    with db.session() as session:
        rows = (
            session.query(TrainingSession)
            .filter(TrainingSession.status == "running")
            .all()
        )
        return sorted(row.id for row in rows if row.id not in ignore)


def approve(config, words, db, device, lock, clock, seed=1):
    """승인 한 번 — 스테이션과 같이 컨트롤러를 새로 만들어 시작한다."""
    gate = SafetyGate(config, device, lock, clock=clock)
    controller = SessionController(
        config, words, db, device, gate, lock, clock=clock,
        rng=random.Random(seed),
    )
    return controller.start("haru")


def test_a_start_that_cannot_take_the_lock_closes_its_session_row(
    config, words, db, device, clock
):
    """락 획득이 거부되면 이미 열린 세션 행을 `aborted` 로 닫고 나간다.

    닫지 않으면 `running` 인 채 영영 남아 다음 승인을 막는다 (DECISIONS §49).
    """
    lock = SessionLock(db, clock=clock)
    other_id = hold_the_lock_with_another_session(db, lock)

    with pytest.raises(LockUnavailableError):
        approve(config, words, db, device, lock, clock)

    # 설정용 행 말고 `running` 으로 남은 것이 없다.
    assert running_session_ids(db, ignore={other_id}) == []

    # 그리고 남은 행은 지워진 것이 아니라 `aborted` 로 닫혀 있다 — 승인이
    # 시도된 사실은 기록으로 남는다.
    with db.session() as session:
        mine = (
            session.query(TrainingSession)
            .filter(TrainingSession.id != other_id)
            .all()
        )
        assert [row.status for row in mine] == ["aborted"]
        assert all(row.ended_at is not None for row in mine)


def test_a_start_that_cannot_take_the_lock_leaves_the_holder_alone(
    config, words, db, device, clock
):
    """정리하면서 남의 락을 놓지 않는다 — 잡은 적이 없기 때문이다.

    여기서 `release` 를 부르면 보유자가 손을 쓰는 중에 락이 풀린다.
    """
    lock = SessionLock(db, clock=clock)
    other_id = hold_the_lock_with_another_session(db, lock)

    with pytest.raises(LockUnavailableError):
        approve(config, words, db, device, lock, clock)

    assert lock.holder() == other_id


def test_repeated_failed_starts_do_not_pile_up_running_rows(
    config, words, db, device, clock
):
    """승인이 연속으로 실패해도 유령 행이 쌓이지 않는다.

    409 영구화의 실제 원인이었다: 실패 한 번에 `running` 행 하나가 남고,
    남은 행이 다음 승인을 또 막는다 (DECISIONS §49).
    """
    lock = SessionLock(db, clock=clock)
    other_id = hold_the_lock_with_another_session(db, lock)
    started_at = clock.now

    for _ in range(5):
        with pytest.raises(LockUnavailableError):
            approve(config, words, db, device, lock, clock)

    assert running_session_ids(db, ignore={other_id}) == []

    # 다섯 번 모두 락이 물려 있어서 실패한 것이다. 가짜 시계는 한 틱도
    # 움직이지 않았으므로 만료(60초)가 끼어들 여지가 없다.
    assert clock.now == started_at
    assert lock.holder() == other_id


def test_a_start_succeeds_once_the_lock_is_free_again(
    config, words, db, device, clock
):
    """실패가 쌓인 뒤에도 락이 풀리면 다음 승인은 정상으로 시작한다.

    누수된 행이 남아 있으면 이 시작이 다시 `LOCK_UNAVAILABLE` 로 막혔다.
    """
    lock = SessionLock(db, clock=clock)
    other_id = hold_the_lock_with_another_session(db, lock)

    for _ in range(3):
        with pytest.raises(LockUnavailableError):
            approve(config, words, db, device, lock, clock)

    # 만료가 아니라 명시적 반납으로 푼다. 가짜 시계는 그대로 둔다.
    lock.release(other_id)

    session_id = approve(config, words, db, device, lock, clock)

    assert lock.holder() == session_id
    assert running_session_ids(db, ignore={other_id}) == [session_id]
    with db.session() as session:
        assert session.get(TrainingSession, session_id).status == "running"


# --------------------------------------------------------------------------- #
# 기록
# --------------------------------------------------------------------------- #

def test_every_item_is_recorded_with_its_track(runner, db):
    summary = runner.run("haru", always_correct)
    with db.session() as session:
        items = session.query(Item).filter(
            Item.session_id == summary.session_id
        ).all()

    assert len(items) == 12
    assert sum(1 for i in items if i.track == TRACK_DISCRIMINATION) == 8
    assert sum(1 for i in items if i.track == TRACK_WORD) == 4
    for item in items:
        assert item.ready_at is not None
        assert item.answered_at is not None
        assert item.answered_at >= item.ready_at
        assert item.result == "correct"


def test_word_items_come_from_the_word_pool(runner, db, words):
    summary = runner.run("haru", always_correct)
    pool = {w.letters for w in words.words}
    word_items = [o for o in summary.outcomes if o.track == TRACK_WORD]
    assert len(word_items) == 4
    assert all(o.letters in pool for o in word_items)


def test_discrimination_items_respect_the_locked_depth_gate(runner):
    """신규 학습자는 깊이쌍이 잠겨 있다. 세션 내내 깊이쌍이 나오면 안 된다."""
    summary = runner.run("haru", always_correct)
    for outcome in summary.outcomes:
        if outcome.track != TRACK_DISCRIMINATION:
            continue
        present = set(outcome.letters)
        assert not {"E", "I"} <= present, outcome.letters
        assert not {"O", "S"} <= present, outcome.letters
        assert not {"E", "O"} <= present, outcome.letters


def test_no_discrimination_item_repeats_within_a_session(runner):
    summary = runner.run("haru", always_correct)
    letters = [
        o.letters for o in summary.outcomes if o.track == TRACK_DISCRIMINATION
    ]
    assert len(set(letters)) == len(letters)


# --------------------------------------------------------------------------- #
# 오답 경로
# --------------------------------------------------------------------------- #

def test_a_wrong_answer_is_retried_once(runner, db):
    summary = runner.run(
        "haru", always_wrong, plan=SessionPlan(discrimination_items=1, word_items=0)
    )
    assert [o.attempt for o in summary.outcomes] == [1, 2]
    assert all(o.result == "wrong" for o in summary.outcomes)

    with db.session() as session:
        items = session.query(Item).filter(
            Item.session_id == summary.session_id
        ).order_by(Item.id).all()
    assert [i.attempt for i in items] == [1, 2]
    assert items[0].letters == items[1].letters


def test_the_retry_is_slower_and_explicit(runner, db, config):
    """오답 1회 → 같은 문항을 느린 속도·또박또박으로 재생 (기획서 5.2)."""
    summary = runner.run(
        "haru", always_wrong, plan=SessionPlan(discrimination_items=1, word_items=0)
    )
    with db.session() as session:
        items = session.query(Item).filter(
            Item.session_id == summary.session_id
        ).order_by(Item.id).all()

    assert items[1].transition_ms == config.timing.transition_slow_ms
    assert items[1].transition_ms > items[0].transition_ms
    assert items[1].boundary == BOUNDARY_EXPLICIT
    assert items[1].hold_ms == config.timing.hold_max_ms


def test_a_second_wrong_answer_triggers_the_demo(runner, config):
    summary = runner.run(
        "haru", always_wrong, plan=SessionPlan(discrimination_items=1, word_items=0)
    )
    second = summary.outcomes[-1]
    assert second.demo is not None
    assert second.demo.expected != second.demo.given
    # 힌트는 혼동쌍 목록에 있는 쌍일 때만 붙는다.
    listed = {frozenset(p.pair): p.hint for p in config.confusion_pairs}
    key = frozenset((second.demo.expected, second.demo.given))
    assert second.demo.hint == listed.get(key)


def test_confusions_are_recorded_for_wrong_answers(runner, db):
    from server.stats import confusion_counts

    summary = runner.run(
        "haru", always_wrong, plan=SessionPlan(discrimination_items=2, word_items=0)
    )
    with db.session() as session:
        counts = confusion_counts(
            session,
            load_config(REAL_POSES),
            TRACK_DISCRIMINATION,
            session_id=summary.session_id,
        )
    assert counts
    assert all(c.count >= 1 for c in counts)


def test_an_order_error_gets_no_demo(runner, config):
    """애너그램은 혼동 기록이 없으므로 시범 대상이 없다 (PRD 9.4)."""

    def scrambled(prompt: ItemPrompt, expected: str) -> str:
        return expected[::-1]

    summary = runner.run(
        "haru", scrambled, plan=SessionPlan(discrimination_items=1, word_items=0)
    )
    assert all(o.result == "order" for o in summary.outcomes)
    assert summary.outcomes[-1].demo is None


def test_order_errors_land_in_their_own_column(runner):
    def scrambled(prompt: ItemPrompt, expected: str) -> str:
        return expected[::-1]

    summary = runner.run(
        "haru", scrambled, plan=SessionPlan(discrimination_items=2, word_items=0)
    )
    track = summary.by_track[TRACK_DISCRIMINATION]
    assert track.order_errors == track.answered
    assert track.wrong == 0
    assert track.correct == 0


# --------------------------------------------------------------------------- #
# 적응 규칙 (PRD 9.4)
# --------------------------------------------------------------------------- #

def hold_chain(config):
    """한 단에서 유지 시간이 바닥까지 내려가는 경로. 설정에서 나온다."""
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=DIFFICULTY_LADDER[0][0],
        hold_ms=config.timing.hold_default_ms,
        transition_ms=config.timing.transition_ms,
        boundary=DIFFICULTY_LADDER[0][1],
    )
    holds = [difficulty.hold_ms]
    while True:
        nxt = rule.on_correct(difficulty, config)
        if nxt.hold_ms >= difficulty.hold_ms:
            return holds
        difficulty = nxt
        holds.append(difficulty.hold_ms)


def test_hold_time_walks_down_to_the_configured_floor(config):
    """유지 시간은 hold_default 에서 hold_min 까지 0.85 배씩 내려간다.

    단계 수는 설정이 정한다. 하한이 2000 이던 때는 3단계뿐이라 ①축이 사실상
    죽은 축이었고, 800 으로 내려간 뒤 9단계가 되었다 (DECISIONS.md §7).
    """
    holds = hold_chain(config)

    assert holds[0] == config.timing.hold_default_ms
    assert holds[-1] == config.timing.hold_min_ms
    assert holds == sorted(holds, reverse=True), "단조 감소한다"
    assert len(set(holds)) == len(holds), "같은 값을 두 번 거치지 않는다"
    # 실측 하한 800ms 기준. 하한을 다시 옮기면 여기서 걸린다.
    assert holds == [3000, 2550, 2167, 1841, 1564, 1329, 1129, 959, 815, 800]


def test_the_hold_axis_is_no_longer_a_dead_axis(config):
    """①축이 되살아났다는 것을 숫자로 고정한다.

    하한 2000 일 때는 3000→2550→2167→2000 의 3단계뿐이었다. 조절 폭이 1.5배에
    불과해 순간 판별 훈련이 성립하지 않았다 (기획서 3.2 "①축 약화").
    """
    holds = hold_chain(config)
    assert len(holds) - 1 >= 5, "감소 단계가 최소 5회는 되어야 축이라 할 수 있다"
    assert holds[0] / holds[-1] >= 3.0, "조절 폭이 3배 이상"


def test_after_the_floor_the_second_and_third_axes_advance(config):
    """②·③축 우선 순서: length 3→5 → direct → length 5→7 (PRD 9.4)."""
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=3,
        hold_ms=config.timing.hold_min_ms,
        transition_ms=config.timing.transition_ms,
        boundary=BOUNDARY_EXPLICIT,
    )
    rungs = [(difficulty.length, difficulty.boundary)]
    for _ in range(3):
        # 각 단에서 ①축이 되살아나므로, 다음 단으로 오르려면 다시 바닥까지
        # 내려가야 한다.
        difficulty = rule.on_correct(difficulty, config)
        rungs.append((difficulty.length, difficulty.boundary))
        while difficulty.hold_ms > config.timing.hold_min_ms:
            difficulty = rule.on_correct(difficulty, config)

    assert rungs == list(DIFFICULTY_LADDER)


def test_the_top_rung_stays_put(config):
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=7,
        hold_ms=config.timing.hold_min_ms,
        transition_ms=config.timing.transition_ms,
        boundary=BOUNDARY_DIRECT,
    )
    for _ in range(5):
        difficulty = rule.on_correct(difficulty, config)
        assert (difficulty.length, difficulty.boundary) == DIFFICULTY_LADDER[-1]
        assert difficulty.hold_ms == config.timing.hold_min_ms


def test_the_hold_axis_is_restored_when_a_rung_is_climbed(config):
    """①축은 양방향이다 — 단을 올라갈 때 유지 시간이 되돌아온다.

    되돌리지 않으면 새 단의 첫 문항이 곧바로 최단 유지로 나온다. 길이가 3에서
    5로 늘어난 그 문항이 동시에 ①축 최대 난이도라는 뜻이고, 두 축이 한꺼번에
    올라가 버린다. 그러면 어느 축 때문에 틀렸는지 알 수 없다 (DECISIONS.md §23).
    """
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=3,
        hold_ms=config.timing.hold_min_ms,
        transition_ms=config.timing.transition_ms,
        boundary=BOUNDARY_EXPLICIT,
    )
    climbed = rule.on_correct(difficulty, config)

    assert climbed.length == 5, "②축이 올라간다"
    assert climbed.hold_ms == config.timing.hold_default_ms, "①축은 되돌아온다"
    assert climbed.hold_ms > difficulty.hold_ms


def test_the_full_climb_walks_every_rung_and_every_hold(config):
    """정답만 계속하면 (단 x 유지 단계) 자리를 모두 지나고 꼭대기에서 멈춘다."""
    rule = RuleAdaptation()
    steps_per_rung = len(hold_chain(config))
    total = len(DIFFICULTY_LADDER) * steps_per_rung

    difficulty = Difficulty(
        length=DIFFICULTY_LADDER[0][0],
        hold_ms=config.timing.hold_default_ms,
        transition_ms=config.timing.transition_ms,
        boundary=DIFFICULTY_LADDER[0][1],
    )
    seen = []
    for _ in range(total):
        seen.append((difficulty.length, difficulty.boundary, difficulty.hold_ms))
        difficulty = rule.on_correct(difficulty, config)

    assert len(set(seen)) == total, f"{total}개 자리가 모두 서로 다르다"
    assert seen[0] == (
        DIFFICULTY_LADDER[0][0],
        DIFFICULTY_LADDER[0][1],
        config.timing.hold_default_ms,
    )
    assert seen[-1] == (
        DIFFICULTY_LADDER[-1][0],
        DIFFICULTY_LADDER[-1][1],
        config.timing.hold_min_ms,
    )
    for length, boundary, _hold in seen:
        assert (length, boundary) in DIFFICULTY_LADDER

    # 꼭대기에서는 더 오르지 않는다.
    assert rule.on_correct(difficulty, config) == difficulty


def test_a_learner_who_retreated_can_climb_back(config):
    """오답 2회로 길이가 사다리 밖(4글자)으로 내려가도 다시 올라갈 수 있다.

    사다리에 정확히 일치하는 칸만 인정하면 후퇴한 학습자가 영영 갇힌다.
    """
    rule = RuleAdaptation()
    at_five = Difficulty(
        length=5,
        hold_ms=config.timing.hold_min_ms,
        transition_ms=config.timing.transition_ms,
        boundary=BOUNDARY_EXPLICIT,
    )
    retreated = rule.on_wrong_second(at_five, config)
    assert retreated.length == 4
    assert (retreated.length, retreated.boundary) not in DIFFICULTY_LADDER

    # 한 단을 올라가려면 유지 시간을 바닥까지 내린 뒤 한 번 더 맞혀야 한다.
    # 필요한 정답 수는 설정이 정한다 — 하한이 낮을수록 길어진다.
    needed = len(hold_chain(config))
    difficulty = retreated
    for _ in range(needed):
        difficulty = rule.on_correct(difficulty, config)
    assert difficulty.length >= 5, "사다리 밖에서도 다시 올라온다"


def test_hold_never_leaves_the_configured_band(config):
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=3,
        hold_ms=config.timing.hold_default_ms,
        transition_ms=config.timing.transition_ms,
        boundary=BOUNDARY_EXPLICIT,
    )
    for _ in range(20):
        difficulty = rule.on_correct(difficulty, config)
        assert config.timing.hold_min_ms <= difficulty.hold_ms
        assert difficulty.hold_ms <= config.timing.hold_max_ms


def test_wrong_first_slows_down_and_goes_explicit(config):
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=5, hold_ms=2000, transition_ms=900, boundary=BOUNDARY_DIRECT
    )
    after = rule.on_wrong_first(difficulty, config)
    assert after.hold_ms == config.timing.hold_max_ms
    assert after.transition_ms == config.timing.transition_slow_ms
    assert after.boundary == BOUNDARY_EXPLICIT
    assert after.length == 5, "1회 오답에서는 길이를 줄이지 않는다"


def test_wrong_second_retreats(config):
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=5, hold_ms=2000, transition_ms=1800, boundary=BOUNDARY_EXPLICIT
    )
    after = rule.on_wrong_second(difficulty, config)
    assert after.length == 4
    assert after.hold_ms == config.timing.hold_default_ms
    assert after.transition_ms == config.timing.transition_ms


def test_retreat_stops_at_the_shortest_length(config):
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=3, hold_ms=2000, transition_ms=900, boundary=BOUNDARY_EXPLICIT
    )
    assert rule.on_wrong_second(difficulty, config).length == 3


def test_the_decay_factor_is_the_documented_one():
    assert HOLD_DECAY_ON_CORRECT == 0.85


def test_adaptation_can_be_swapped_out(config, words, db, device, clock):
    """LLM 이 나중에 이 자리를 대신할 수 있게 분리되어 있다."""

    class NeverChanges:
        def on_correct(self, difficulty, config):
            return difficulty

        def on_wrong_first(self, difficulty, config):
            return difficulty

        def on_wrong_second(self, difficulty, config):
            return difficulty

    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    runner = SessionRunner(
        config, words, db, device, gate, lock,
        adaptation=NeverChanges(), clock=clock, rng=random.Random(3),
    )
    summary = runner.run(
        "haru", always_correct, plan=SessionPlan(discrimination_items=4, word_items=0)
    )
    with db.session() as session:
        items = session.query(Item).filter(
            Item.session_id == summary.session_id
        ).all()
    assert {i.hold_ms for i in items} == {config.timing.hold_default_ms}
    assert {len(i.letters) for i in items} == {3}


# --------------------------------------------------------------------------- #
# 트랙 분리 (R4 인접)
# --------------------------------------------------------------------------- #

def test_word_answers_do_not_move_discrimination_numbers(runner, db):
    """단어는 전부 틀리고 판별은 전부 맞히면, 두 숫자가 따로 나와야 한다."""

    def word_wrong(prompt: ItemPrompt, expected: str) -> str:
        if prompt.track == TRACK_WORD:
            return always_wrong(prompt, expected)
        return expected

    summary = runner.run("haru", word_wrong)

    discrimination = summary.by_track[TRACK_DISCRIMINATION]
    word = summary.by_track[TRACK_WORD]

    assert discrimination.accuracy == 1.0
    assert discrimination.wrong == 0
    assert word.correct == 0
    assert word.wrong > 0


def test_confusions_stay_in_their_own_track(runner, db, config):
    from server.stats import confusion_counts

    def word_wrong(prompt: ItemPrompt, expected: str) -> str:
        if prompt.track == TRACK_WORD:
            return always_wrong(prompt, expected)
        return expected

    summary = runner.run("haru", word_wrong)
    with db.session() as session:
        discrimination = confusion_counts(
            session, config, TRACK_DISCRIMINATION, session_id=summary.session_id
        )
        word = confusion_counts(
            session, config, TRACK_WORD, session_id=summary.session_id
        )
    assert discrimination == ()
    assert word


# --------------------------------------------------------------------------- #
# 프롬프트 계약
# --------------------------------------------------------------------------- #

def test_the_prompt_describes_the_item(runner):
    seen = []

    def record(prompt: ItemPrompt, expected: str) -> str:
        seen.append(prompt)
        return expected

    runner.run("haru", record, plan=SessionPlan(discrimination_items=2, word_items=1))

    assert [p.seq for p in seen] == [1, 2, 3]
    assert [p.track for p in seen] == [
        TRACK_DISCRIMINATION,
        TRACK_DISCRIMINATION,
        TRACK_WORD,
    ]
    for prompt in seen:
        assert prompt.length > 0
        assert prompt.attempt == 1
        assert prompt.boundary in (BOUNDARY_EXPLICIT, BOUNDARY_DIRECT)


def test_the_second_attempt_says_so(runner):
    attempts = []

    def record(prompt: ItemPrompt, expected: str) -> str:
        attempts.append(prompt.attempt)
        return always_wrong(prompt, expected)

    runner.run(
        "haru", record, plan=SessionPlan(discrimination_items=1, word_items=0)
    )
    assert attempts == [1, 2]


def test_the_lock_survives_a_long_demanding_item(config, words, db, device, clock):
    """긴 문항 하나가 락 만료 시간을 넘기지 않아야 한다.

    7글자 문항을 느린 재생(1800ms)·최대 유지(3000ms)로 내면 한 문항이
    7 x 4800ms + 중립 = 40초를 넘고, 시범까지 붙으면 락 만료 60초를 넘긴다.
    heartbeat 를 문항 단위로 찍으면 세션이 자기 락을 잃는다 — 그래서 글자마다
    찍는다 (DECISIONS.md §25).
    """
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)
    runner = SessionRunner(
        config, words, db, device, gate, lock, clock=clock,
        rng=random.Random(11),
    )

    # 앞의 4문항을 맞혀 난이도를 7글자·direct 까지 올린 뒤 틀린다.
    def climb_then_fail(prompt: ItemPrompt, expected: str) -> str:
        if prompt.seq <= 5:
            return expected
        return always_wrong(prompt, expected)

    summary = runner.run(
        "haru", climb_then_fail, plan=SessionPlan(discrimination_items=8, word_items=4)
    )
    assert len(summary.outcomes) >= 12
    assert summary.by_track[TRACK_DISCRIMINATION].answered >= 8


def test_a_heartbeat_is_sent_for_every_letter(config, words, db, device, clock):
    """락 heartbeat 는 글자마다 찍힌다."""
    lock = SessionLock(db, clock=clock)
    beats = []
    original = lock.heartbeat

    def counting(session_id):
        beats.append(session_id)
        return original(session_id)

    lock.heartbeat = counting
    gate = SafetyGate(config, device, lock, clock=clock)
    runner = SessionRunner(
        config, words, db, device, gate, lock, clock=clock, rng=random.Random(12)
    )
    summary = runner.run(
        "haru", always_correct, plan=SessionPlan(discrimination_items=2, word_items=0)
    )
    letters = sum(len(o.letters) for o in summary.outcomes)
    # 문항 시작마다 1회 + 글자마다 1회.
    assert len(beats) >= letters


# --------------------------------------------------------------------------- #
# 정답 유출 회귀 (DECISIONS §22) — 이 테스트는 완화하지 않는다
# --------------------------------------------------------------------------- #

def test_the_client_prompt_has_no_answer_field(runner):
    """클라이언트 표현을 직렬화한 결과에 정답이 등장하지 않는다.

    ItemPrompt 는 그대로 브라우저로 나간다. 정답이 필드로 들어 있으면 화면에
    띄우지 않아도 응답 본문에서 읽힌다.
    """
    import dataclasses
    import json

    seen = []

    def capture(prompt: ItemPrompt, expected: str):
        seen.append((prompt, expected))
        return expected

    runner.run("haru", capture, plan=SessionPlan(discrimination_items=4, word_items=2))

    assert seen
    for prompt, expected in seen:
        payload = json.dumps(dataclasses.asdict(prompt), ensure_ascii=False)
        assert expected not in payload, payload
        # 필드 값 어디에도 정답이 들어 있지 않다.
        for value in dataclasses.asdict(prompt).values():
            assert value != expected
        assert not hasattr(prompt, "expected")
        assert not hasattr(prompt, "letters")
        assert not hasattr(prompt, "answer")


def test_item_prompt_fields_are_fixed():
    """필드가 늘어나면 여기서 걸린다. 늘릴 값이 정답을 되돌리지 않는지 확인할 것."""
    import dataclasses

    assert {f.name for f in dataclasses.fields(ItemPrompt)} == {
        "seq",
        "track",
        "length",
        "boundary",
        "hold_ms",
        "transition_ms",
        "attempt",
    }


def test_length_alone_cannot_reveal_the_answer(runner):
    """길이는 화면에 표시되는 값이다 (PRD 14.1). 길이만으로는 정답이 복원되지 않는다."""
    prompts = []

    def capture(prompt: ItemPrompt, expected: str):
        prompts.append((prompt.length, expected))
        return expected

    runner.run("haru", capture, plan=SessionPlan(discrimination_items=6, word_items=0))
    lengths = {length for length, _ in prompts}
    answers = {expected for _, expected in prompts}
    assert len(answers) > len(lengths), "같은 길이에 서로 다른 정답이 있어야 한다"


# --------------------------------------------------------------------------- #
# 난이도 이력 지속 (DECISIONS §40)
# --------------------------------------------------------------------------- #

def first_prompt_of(runner, learner="haru", **plan_kwargs):
    """세션을 돌리고 첫 문항의 난이도를 돌려준다."""
    seen = []

    def capture(prompt: ItemPrompt, expected: str):
        seen.append(prompt)
        return expected

    runner.run(learner, capture, plan=SessionPlan(**plan_kwargs))
    return seen[0]


def saved_difficulty(db, learner_id, track):
    from server.db import load_difficulty

    with db.session() as session:
        return load_difficulty(session, learner_id, track)


def test_a_new_learner_starts_at_the_initial_difficulty(runner, config):
    prompt = first_prompt_of(runner, discrimination_items=1, word_items=0)
    assert prompt.length == DIFFICULTY_LADDER[0][0]
    assert prompt.boundary == DIFFICULTY_LADDER[0][1]
    assert prompt.hold_ms == config.timing.hold_default_ms


def test_the_final_difficulty_is_saved(runner, db, config):
    summary = runner.run(
        "haru", always_correct, plan=SessionPlan(discrimination_items=3, word_items=0)
    )
    row = saved_difficulty(db, summary.learner_id, TRACK_DISCRIMINATION)

    assert row is not None
    # 3문항 연속 정답: 3000 -> 2550 -> 2167 -> 1841
    assert row.hold_ms == 1841
    assert row.length == DIFFICULTY_LADDER[0][0]
    assert row.updated_at is not None


def test_the_next_session_resumes_where_the_last_one_ended(runner, db):
    """10세션을 해도 매번 처음부터 시작하던 문제를 고정한다."""
    runner.run(
        "haru", always_correct, plan=SessionPlan(discrimination_items=3, word_items=0)
    )
    ended_at = saved_difficulty(db, 1, TRACK_DISCRIMINATION).hold_ms

    resumed = first_prompt_of(runner, discrimination_items=1, word_items=0)
    assert resumed.hold_ms == ended_at == 1841


def test_repeated_sessions_keep_climbing(runner, db, config):
    """세션을 거듭하면 난이도가 계속 올라간다 — 반복 훈련의 경로."""
    holds = []
    for _ in range(4):
        holds.append(
            first_prompt_of(runner, discrimination_items=3, word_items=0).hold_ms
        )
    assert holds == sorted(holds, reverse=True), holds
    assert holds[0] == config.timing.hold_default_ms
    assert holds[-1] < holds[0], "네 번째 세션은 첫 세션보다 어렵게 시작한다"


def test_the_two_tracks_are_stored_and_restored_independently(runner, db, config):
    """판별에서 오른 난이도가 단어 난이도에 섞이지 않는다."""
    runner.run(
        "haru", always_correct, plan=SessionPlan(discrimination_items=6, word_items=0)
    )

    disc = saved_difficulty(db, 1, TRACK_DISCRIMINATION)
    word = saved_difficulty(db, 1, TRACK_WORD)
    assert disc.hold_ms < config.timing.hold_default_ms
    assert word is None, "단어 문항을 내지 않았으므로 단어 난이도는 생기지 않는다"

    # 이제 단어만 낸다. 단어는 초기값에서 시작하고 판별 기록은 그대로다.
    seen = []

    def capture(prompt: ItemPrompt, expected: str):
        seen.append(prompt)
        return expected

    runner.run("haru", capture, plan=SessionPlan(discrimination_items=0, word_items=2))

    assert seen[0].hold_ms == config.timing.hold_default_ms, "단어는 처음부터"
    assert saved_difficulty(db, 1, TRACK_DISCRIMINATION).hold_ms == disc.hold_ms


def test_a_wrong_word_answer_does_not_ease_the_discrimination_track(runner, db, config):
    """단어에서 틀렸다고 판별 난이도가 내려가면 두 트랙을 섞는 것이다."""

    def word_wrong(prompt: ItemPrompt, expected: str) -> str:
        if prompt.track == TRACK_WORD:
            return always_wrong(prompt, expected)
        return expected

    runner.run(
        "haru", word_wrong, plan=SessionPlan(discrimination_items=3, word_items=2)
    )

    disc = saved_difficulty(db, 1, TRACK_DISCRIMINATION)
    word = saved_difficulty(db, 1, TRACK_WORD)

    # 판별은 3연속 정답만큼 내려갔다.
    assert disc.hold_ms == 1841
    # 단어는 오답 처리로 되돌아갔다. 판별 값과 무관하다.
    assert word.hold_ms != disc.hold_ms


def test_progress_survives_an_aborted_session(runner, db, config):
    """중간에 끊겨도 그때까지 오른 만큼은 남는다.

    문항 기록(items)은 답할 때마다 남는다. 난이도만 버리면 같은 사건을 두 가지
    규칙으로 다루는 셈이라, 난이도도 문항마다 저장한다 (DECISIONS.md §40).
    """
    boom = RuntimeError("device exploded")

    def fail_on_third(prompt: ItemPrompt, expected: str) -> str:
        if prompt.seq == 3:
            raise boom
        return expected

    with pytest.raises(RuntimeError):
        runner.run(
            "haru", fail_on_third,
            plan=SessionPlan(discrimination_items=5, word_items=0),
        )

    row = saved_difficulty(db, 1, TRACK_DISCRIMINATION)
    assert row is not None, "중단되어도 진행한 만큼은 남아야 한다"
    # 2문항을 맞힌 상태: 3000 -> 2550 -> 2167
    assert row.hold_ms == 2167


def test_an_aborted_session_is_marked_aborted(runner, db):
    def fail_immediately(prompt: ItemPrompt, expected: str) -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        runner.run(
            "haru", fail_immediately,
            plan=SessionPlan(discrimination_items=2, word_items=0),
        )
    with db.session() as session:
        row = session.query(TrainingSession).one()
    assert row.status == "aborted"
    # 한 문항도 답하지 못했으므로 저장된 난이도도 없다.
    assert saved_difficulty(db, 1, TRACK_DISCRIMINATION) is None


def test_the_slow_replay_speed_is_not_carried_over(runner, db, config):
    """오답 재생의 느린 전이(1800ms)가 다음 세션으로 이월되면 안 된다.

    transition_ms 는 난이도 축이 아니다 (PRD 9.1).
    """
    runner.run(
        "haru", always_wrong, plan=SessionPlan(discrimination_items=1, word_items=0)
    )
    resumed = first_prompt_of(runner, discrimination_items=1, word_items=0)
    assert resumed.transition_ms == config.timing.transition_ms
    assert resumed.transition_ms != config.timing.transition_slow_ms


def test_two_learners_do_not_share_progress(runner, db, config):
    runner.run(
        "haru", always_correct, plan=SessionPlan(discrimination_items=4, word_items=0)
    )
    fresh = first_prompt_of(runner, learner="mina", discrimination_items=1, word_items=0)
    assert fresh.hold_ms == config.timing.hold_default_ms
