"""세션 루프와 적응 테스트 (PRD 9.4, 기획서 5.2).

완료 기준: Mock 으로 판별 8 + 단어 4 세션이 끝까지 돌고 요약이 트랙별로 나온다.

시간은 전부 주입한다. 가짜 sleep 이 가짜 시계를 밀고, 게이트와 세션이 같은
시계를 본다 — 그래야 rate limit 이 실제 운용과 같은 조건에서 검사된다.
"""

import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from server.config import load_config, load_words
from server.db import (
    BOUNDARY_DIRECT,
    BOUNDARY_EXPLICIT,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    Database,
    Item,
    TrainingSession,
)
from server.device.mock import MockDevice
from server.items import Difficulty
from server.safety import LockNotHeldError, SafetyGate, SessionLock
from server.session import (
    DIFFICULTY_LADDER,
    HOLD_DECAY_ON_CORRECT,
    ItemPrompt,
    RuleAdaptation,
    SessionPlan,
    SessionRunner,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POSES = REPO_ROOT / "config" / "poses.json"
REAL_WORDS = REPO_ROOT / "config" / "words.json"


class FakeClock:
    def __init__(self, start=datetime(2026, 9, 7, 12, 0, 0)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.advance(seconds)


@pytest.fixture
def config():
    return load_config(REAL_POSES)


@pytest.fixture
def words(config):
    return load_words(REAL_WORDS, letters=config.poses)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db():
    return Database.in_memory()


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

def test_hold_time_bottoms_out_in_three_steps(config):
    """3000 → 2550 → 2167 → 2000. ①축은 3단계뿐이다."""
    rule = RuleAdaptation()
    difficulty = Difficulty(
        length=3,
        hold_ms=config.timing.hold_default_ms,
        transition_ms=config.timing.transition_ms,
        boundary=BOUNDARY_EXPLICIT,
    )
    holds = [difficulty.hold_ms]
    for _ in range(3):
        difficulty = rule.on_correct(difficulty, config)
        holds.append(difficulty.hold_ms)

    assert holds == [3000, 2550, 2167, 2000]
    assert difficulty.length == 3
    assert difficulty.boundary == BOUNDARY_EXPLICIT


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
    for _ in range(4):
        difficulty = rule.on_correct(difficulty, config)
        rungs.append((difficulty.length, difficulty.boundary))

    assert rungs == [
        (3, BOUNDARY_EXPLICIT),
        (5, BOUNDARY_EXPLICIT),
        (5, BOUNDARY_DIRECT),
        (7, BOUNDARY_DIRECT),
        (7, BOUNDARY_DIRECT),  # 꼭대기에서는 더 오르지 않는다
    ]
    assert rungs[:4] == list(DIFFICULTY_LADDER)


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
