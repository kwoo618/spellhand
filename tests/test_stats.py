"""트랙별 통계 테스트 (PRD 9.2/9.5, 17장 필수 항목 "트랙 분리").

이 파일의 핵심은 **두 트랙이 어떤 경로로도 섞이지 않는다**는 것이다.
단어 트랙의 오답을 잔뜩 넣어도 판별 트랙 숫자가 흔들리지 않아야 한다.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from server.config import load_config
from server.db import (
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    Confusion,
    Database,
    Item,
    TrainingSession,
    get_or_create_learner,
    utcnow,
)
from server.stats import (
    confusion_counts,
    depth_unlocked,
    item_contains_depth_pair,
    recent_letters,
    track_summary,
    weakest_pairs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_POSES = REPO_ROOT / "config" / "poses.json"


@pytest.fixture
def config():
    return load_config(REAL_POSES)


@pytest.fixture
def db():
    return Database.in_memory()


class Recorder:
    """테스트용 문항 기록기. 시각을 조금씩 밀어 응답 시간을 만든다."""

    def __init__(self, db, learner_name="haru"):
        self.db = db
        self.seq = 0
        with db.session() as session:
            learner = get_or_create_learner(session, learner_name)
            training = TrainingSession(
                learner_id=learner.id,
                status="running",
                source="rule",
                started_at=utcnow(),
            )
            session.add(training)
            session.commit()
            self.learner_id = learner.id
            self.session_id = training.id

    def add(self, track, letters, result, *, confusions=(), response_ms=1000):
        self.seq += 1
        ready = utcnow()
        with self.db.session() as session:
            item = Item(
                session_id=self.session_id,
                seq=self.seq,
                track=track,
                letters=letters,
                hold_ms=3000,
                transition_ms=900,
                boundary="explicit",
                presented_at=ready,
                ready_at=ready,
                answered_at=ready + timedelta(milliseconds=response_ms),
                answer=letters,
                result=result,
                attempt=1,
            )
            session.add(item)
            session.flush()
            for position, (expected, given) in enumerate(confusions):
                session.add(
                    Confusion(
                        item_id=item.id,
                        track=track,
                        position=position,
                        expected=expected,
                        given=given,
                    )
                )
            session.commit()
        return self


# --------------------------------------------------------------------------- #
# 트랙 인자는 필수다
# --------------------------------------------------------------------------- #

def test_summary_requires_a_track(db):
    with db.session() as session:
        with pytest.raises(TypeError):
            track_summary(session)


def test_unknown_track_is_rejected(db):
    with db.session() as session:
        with pytest.raises(ValueError) as exc:
            track_summary(session, "everything")
    assert "everything" in str(exc.value)


def test_there_is_no_combined_accuracy_function():
    """통합 지표를 만들지 않는다. 만드는 순간 두 트랙이 섞이는 경로가 생긴다."""
    import server.stats as stats

    exported = set(stats.__all__)
    for forbidden in ("overall_accuracy", "total_summary", "combined_summary"):
        assert forbidden not in exported


# --------------------------------------------------------------------------- #
# 트랙 분리 고정
# --------------------------------------------------------------------------- #

def test_word_results_never_reach_discrimination_stats(db, config):
    """두 트랙을 섞어 기록해도 집계는 절대 합쳐지지 않는다."""
    rec = Recorder(db)
    rec.add(TRACK_DISCRIMINATION, "VWB", "correct")
    rec.add(TRACK_DISCRIMINATION, "DOS", "correct")
    for _ in range(5):
        rec.add(TRACK_WORD, "WISE", "wrong", confusions=[("I", "O")])

    with db.session() as session:
        discrimination = track_summary(session, TRACK_DISCRIMINATION)
        word = track_summary(session, TRACK_WORD)

    assert discrimination.answered == 2
    assert discrimination.correct == 2
    assert discrimination.accuracy == 1.0
    assert discrimination.wrong == 0

    assert word.answered == 5
    assert word.correct == 0
    assert word.accuracy == 0.0

    # 두 트랙의 문항 수를 더해도 어느 요약에도 그런 숫자는 없다.
    assert discrimination.answered + word.answered == 7
    assert discrimination.answered != 7 and word.answered != 7


def test_confusions_are_separated_by_track(db, config):
    rec = Recorder(db)
    rec.add(TRACK_DISCRIMINATION, "VWB", "wrong", confusions=[("V", "W")])
    rec.add(TRACK_WORD, "WISE", "wrong", confusions=[("V", "W")])
    rec.add(TRACK_WORD, "LOVE", "wrong", confusions=[("V", "W")])

    with db.session() as session:
        discrimination = confusion_counts(session, config, TRACK_DISCRIMINATION)
        word = confusion_counts(session, config, TRACK_WORD)

    assert [(c.pair, c.count) for c in discrimination] == [(("V", "W"), 1)]
    assert [(c.pair, c.count) for c in word] == [(("V", "W"), 2)]


def test_weakest_pairs_ignore_the_other_track(db, config):
    rec = Recorder(db)
    # 단어 트랙에서만 B/F 를 잔뜩 틀린다.
    for _ in range(10):
        rec.add(TRACK_WORD, "FIB", "wrong", confusions=[("B", "F")])
    # 판별 트랙에서는 I/Y 를 틀린다.
    rec.add(TRACK_DISCRIMINATION, "IYB", "wrong", confusions=[("I", "Y")])

    with db.session() as session:
        weakest = weakest_pairs(session, config, TRACK_DISCRIMINATION, top=1)

    assert weakest[0].pair in (("I", "Y"), ("Y", "I"))


# --------------------------------------------------------------------------- #
# 요약 내용
# --------------------------------------------------------------------------- #

def test_order_errors_are_counted_separately(db):
    rec = Recorder(db)
    rec.add(TRACK_DISCRIMINATION, "VWB", "correct")
    rec.add(TRACK_DISCRIMINATION, "DOS", "order")
    rec.add(TRACK_DISCRIMINATION, "LYB", "wrong", confusions=[("L", "Y")])
    rec.add(TRACK_DISCRIMINATION, "BWY", "length")

    with db.session() as session:
        summary = track_summary(session, TRACK_DISCRIMINATION)

    assert summary.answered == 4
    assert summary.correct == 1
    assert summary.order_errors == 1
    assert summary.wrong == 1
    assert summary.length_errors == 1
    assert summary.accuracy == 0.25
    assert summary.order_error_rate == 0.25


def test_average_response_time_uses_ready_at(db):
    rec = Recorder(db)
    rec.add(TRACK_DISCRIMINATION, "VWB", "correct", response_ms=1000)
    rec.add(TRACK_DISCRIMINATION, "DOS", "correct", response_ms=3000)

    with db.session() as session:
        summary = track_summary(session, TRACK_DISCRIMINATION)
    assert summary.avg_response_ms == pytest.approx(2000)


def test_empty_track_has_no_accuracy(db):
    with db.session() as session:
        summary = track_summary(session, TRACK_WORD)
    assert summary.answered == 0
    assert summary.accuracy is None
    assert summary.avg_response_ms is None


def test_unlisted_confusion_pairs_are_marked(db, config):
    """설정에 없는 쌍도 기록은 되지만 목록에 없다고 표시된다."""
    rec = Recorder(db)
    rec.add(TRACK_DISCRIMINATION, "BLY", "wrong", confusions=[("B", "Y")])

    with db.session() as session:
        counts = confusion_counts(session, config, TRACK_DISCRIMINATION)
    assert len(counts) == 1
    assert counts[0].listed is False


def test_unlisted_pairs_are_never_training_targets(db, config):
    rec = Recorder(db)
    for _ in range(20):
        rec.add(TRACK_DISCRIMINATION, "BLY", "wrong", confusions=[("B", "Y")])

    with db.session() as session:
        weakest = weakest_pairs(session, config, TRACK_DISCRIMINATION, top=2)

    listed = {frozenset(p.pair) for p in config.confusion_pairs}
    for pair in weakest:
        assert frozenset(pair.pair) in listed
        assert frozenset(pair.pair) != frozenset(("B", "Y"))


def test_weakest_pairs_fall_back_to_the_config_order(db, config):
    """기록이 없는 첫 세션에도 목표 쌍은 있어야 한다."""
    with db.session() as session:
        weakest = weakest_pairs(session, config, TRACK_DISCRIMINATION, top=2)
    assert len(weakest) == 2
    assert all(not p.depth for p in weakest)


# --------------------------------------------------------------------------- #
# 깊이쌍 게이팅
# --------------------------------------------------------------------------- #

def test_depth_pair_needs_both_letters(config):
    assert item_contains_depth_pair(config, "EI") is True
    assert item_contains_depth_pair(config, "OS") is True
    assert item_contains_depth_pair(config, "EO") is True
    # 한 글자만 있으면 깊이 판별을 요구하지 않는다.
    assert item_contains_depth_pair(config, "EBV") is False
    assert item_contains_depth_pair(config, "IBV") is False
    assert item_contains_depth_pair(config, "SBV") is False


def test_depth_stays_locked_without_enough_history(db, config):
    rec = Recorder(db)
    for _ in range(5):
        rec.add(TRACK_DISCRIMINATION, "VWB", "correct")

    with db.session() as session:
        assert (
            depth_unlocked(
                session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
            )
            is False
        )


def test_depth_unlocks_once_accuracy_is_met(db, config):
    rec = Recorder(db)
    for i in range(20):
        rec.add(TRACK_DISCRIMINATION, f"VW{'BDFLY'[i % 5]}", "correct")

    with db.session() as session:
        assert (
            depth_unlocked(
                session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
            )
            is True
        )


def test_depth_stays_locked_below_the_accuracy_threshold(db, config):
    rec = Recorder(db)
    for i in range(20):
        result = "correct" if i < 15 else "wrong"
        rec.add(TRACK_DISCRIMINATION, f"VW{'BDFLY'[i % 5]}", result)

    # 15/20 = 0.75 < 0.80
    with db.session() as session:
        assert (
            depth_unlocked(
                session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
            )
            is False
        )


def test_word_track_results_do_not_unlock_depth(db, config):
    """단어 트랙 성적이 판별 트랙의 해금을 좌우하면 트랙 분리 위반이다."""
    rec = Recorder(db)
    for _ in range(30):
        rec.add(TRACK_WORD, "WISE", "correct")

    with db.session() as session:
        assert (
            depth_unlocked(
                session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
            )
            is False
        )


def test_depth_items_are_excluded_from_the_unlock_window(db, config):
    """깊이쌍 문항 자체는 해금 판단의 근거가 되지 않는다."""
    rec = Recorder(db)
    for _ in range(20):
        rec.add(TRACK_DISCRIMINATION, "EIB", "correct")  # 깊이쌍 포함

    with db.session() as session:
        assert (
            depth_unlocked(
                session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
            )
            is False
        )


# --------------------------------------------------------------------------- #
# 최근 출제
# --------------------------------------------------------------------------- #

def test_recent_letters_are_newest_first_and_track_scoped(db):
    rec = Recorder(db)
    rec.add(TRACK_DISCRIMINATION, "VWB", "correct")
    rec.add(TRACK_WORD, "WISE", "correct")
    rec.add(TRACK_DISCRIMINATION, "DOS", "correct")

    with db.session() as session:
        recent = recent_letters(session, TRACK_DISCRIMINATION, window=20)
    assert recent == ("DOS", "VWB")
    assert "WISE" not in recent


def test_recent_letters_respects_the_window(db):
    rec = Recorder(db)
    for i in range(30):
        rec.add(TRACK_DISCRIMINATION, f"V{i:02d}", "correct")

    with db.session() as session:
        assert len(recent_letters(session, TRACK_DISCRIMINATION, window=20)) == 20


def test_recent_letters_include_unanswered_items(db):
    """방금 보여 준 문항은 답을 못 했어도 방금 본 문항이다."""
    rec = Recorder(db)
    with db.session() as session:
        session.add(
            Item(
                session_id=rec.session_id,
                seq=99,
                track=TRACK_DISCRIMINATION,
                letters="LYB",
                hold_ms=3000,
                transition_ms=900,
                boundary="explicit",
            )
        )
        session.commit()

    with db.session() as session:
        assert "LYB" in recent_letters(session, TRACK_DISCRIMINATION, window=20)
        # 답이 없으므로 집계에는 들어가지 않는다.
        assert track_summary(session, TRACK_DISCRIMINATION).answered == 0
