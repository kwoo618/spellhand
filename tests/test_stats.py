"""트랙별 통계 테스트 (PRD 9.2/9.5, 17장 필수 항목 "트랙 분리").

이 파일의 핵심은 **두 트랙이 어떤 경로로도 섞이지 않는다**는 것이다.
단어 트랙의 오답을 잔뜩 넣어도 판별 트랙 숫자가 흔들리지 않아야 한다.
"""

from datetime import timedelta

import pytest

from server.db import (
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    Confusion,
    Item,
    TrainingSession,
    count_answered_sessions,
    get_or_create_learner,
    utcnow,
)
from server.stats import (
    accuracy_by_hold_ms,
    confusion_counts,
    default_pairs,
    depth_unlock_progress,
    depth_unlocked,
    item_contains_depth_pair,
    recent_items,
    recent_letters,
    session_accuracy,
    track_summary,
    weakest_pairs,
)

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


# --------------------------------------------------------------------------- #
# 해금 진척도 — 판정과 같은 계산 (DECISIONS §52)
# --------------------------------------------------------------------------- #

def test_depth_progress_agrees_with_the_gate(db, config):
    """진척도와 판정이 두 벌이면 조건을 고칠 때 어긋난다. 같은 값을 내야 한다."""
    rec = Recorder(db)
    window = config.curriculum["depth_pairs_unlock"]["window_items"]
    for i in range(window + 3):
        result = "correct" if i % 4 else "wrong"
        rec.add(TRACK_DISCRIMINATION, "VW" + "BDFLY"[i % 5], result)
        with db.session() as session:
            progress = depth_unlock_progress(
                session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
            )
            assert progress.unlocked == depth_unlocked(
                session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
            )
            assert progress.counted == min(i + 1, window)
            assert progress.items_remaining == max(0, window - (i + 1))


def test_depth_progress_reports_accuracy_before_the_window_fills(db, config):
    """창이 다 차기 전에도 지금까지의 정확도는 낸다. 판정은 아직 잠금이다."""
    rec = Recorder(db)
    for _ in range(5):
        rec.add(TRACK_DISCRIMINATION, "VWB", "correct")

    with db.session() as session:
        progress = depth_unlock_progress(
            session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
        )
    assert progress.counted == 5
    assert progress.correct == 5
    assert progress.accuracy == 1.0
    assert progress.unlocked is False


def test_depth_progress_skips_depth_items_like_the_gate(db, config):
    rec = Recorder(db)
    rec.add(TRACK_DISCRIMINATION, "EIB", "correct")  # 깊이쌍 포함 — 세지 않는다
    rec.add(TRACK_DISCRIMINATION, "VWB", "wrong")

    with db.session() as session:
        progress = depth_unlock_progress(
            session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
        )
    assert (progress.answered, progress.counted, progress.correct) == (1, 1, 0)


def test_depth_progress_without_a_learner_counts_nothing(db, config):
    """`learner_id=None` 은 "기록 없는 학습자" 다. 모든 학습자를 세지 않는다."""
    rec = Recorder(db)
    for _ in range(25):
        rec.add(TRACK_DISCRIMINATION, "VWB", "correct")

    with db.session() as session:
        progress = depth_unlock_progress(
            session, config, TRACK_DISCRIMINATION, learner_id=None
        )
    assert (progress.answered, progress.counted, progress.unlocked) == (0, 0, False)


def test_depth_progress_is_scoped_to_one_track(db, config):
    rec = Recorder(db)
    for _ in range(30):
        rec.add(TRACK_WORD, "WISE", "correct")
    with db.session() as session:
        progress = depth_unlock_progress(
            session, config, TRACK_DISCRIMINATION, learner_id=rec.learner_id
        )
    assert progress.answered == 0
    with pytest.raises(ValueError):
        with db.session() as session:
            depth_unlock_progress(session, config, "both", learner_id=rec.learner_id)


# --------------------------------------------------------------------------- #
# 세션별 추이
# --------------------------------------------------------------------------- #

def test_session_accuracy_is_per_track_and_oldest_first(db):
    first = Recorder(db)
    first.add(TRACK_DISCRIMINATION, "VWB", "correct")
    first.add(TRACK_DISCRIMINATION, "VWD", "wrong")
    first.add(TRACK_WORD, "WISE", "wrong")

    second = Recorder(db)  # 같은 학습자, 새 세션
    second.add(TRACK_DISCRIMINATION, "VWF", "correct")

    with db.session() as session:
        disc = session_accuracy(
            session, TRACK_DISCRIMINATION, learner_id=first.learner_id
        )
        word = session_accuracy(session, TRACK_WORD, learner_id=first.learner_id)
        latest = session_accuracy(
            session, TRACK_DISCRIMINATION, learner_id=first.learner_id, limit=1
        )

    assert [p.session_id for p in disc] == [first.session_id, second.session_id]
    assert [(p.answered, p.correct) for p in disc] == [(2, 1), (1, 1)]
    assert disc[0].accuracy == 0.5
    # 단어 트랙의 오답이 판별 트랙의 줄에 들어가지 않는다.
    assert [(p.session_id, p.answered, p.correct) for p in word] == [
        (first.session_id, 1, 0)
    ]
    assert [p.session_id for p in latest] == [second.session_id]


def test_session_accuracy_requires_a_track(db):
    with pytest.raises(ValueError):
        with db.session() as session:
            session_accuracy(session, "both", learner_id=1)


def test_answered_sessions_skip_sessions_with_no_answers(db):
    """승인만 되고 아무도 참여하지 않은 세션은 "누적 세션" 이 아니다."""
    rec = Recorder(db)
    rec.add(TRACK_WORD, "WISE", "correct")
    Recorder(db)  # 빈 세션

    with db.session() as session:
        assert count_answered_sessions(session, rec.learner_id) == 1


# --------------------------------------------------------------------------- #
# 학습자 범위 — 학습자가 둘 이상일 때 (DECISIONS §54)
# --------------------------------------------------------------------------- #

def test_every_learner_scoped_aggregate_counts_only_that_learner(db, config):
    """위의 학습자별 테스트는 전부 학습자가 **하나뿐인** DB 에서 돈다. 그러면
    `learner_id` 필터가 빠져도 결과가 같아 통과한다 — §53 의 결함이 그렇게
    숨었다. 여기서는 기록이 다른 두 학습자를 심고 함수마다 자기 것만 세는지
    본다."""
    target = [p for p in config.confusion_pairs if not p.depth][-1]
    a, b = target.pair

    haru = Recorder(db, learner_name="haru")
    haru.add(TRACK_DISCRIMINATION, "VWB", "correct")

    mina = Recorder(db, learner_name="mina")
    for _ in range(3):
        mina.add(TRACK_DISCRIMINATION, f"{a}{b}L", "wrong", confusions=((a, b),))

    with db.session() as session:
        def scoped(learner):
            return dict(
                summary=track_summary(
                    session, TRACK_DISCRIMINATION, learner_id=learner.learner_id
                ).answered,
                holds=sum(
                    bucket.answered
                    for bucket in accuracy_by_hold_ms(
                        session, TRACK_DISCRIMINATION, learner_id=learner.learner_id
                    )
                ),
                recent=[
                    r.letters
                    for r in recent_items(
                        session, TRACK_DISCRIMINATION, limit=20,
                        learner_id=learner.learner_id,
                    )
                ],
                letters=list(
                    recent_letters(
                        session, TRACK_DISCRIMINATION, window=20,
                        learner_id=learner.learner_id,
                    )
                ),
                confusions=[
                    c.pair
                    for c in confusion_counts(
                        session, config, TRACK_DISCRIMINATION,
                        learner_id=learner.learner_id,
                    )
                ],
                weakest=[
                    tuple(p.pair)
                    for p in weakest_pairs(
                        session, config, TRACK_DISCRIMINATION, top=2,
                        learner_id=learner.learner_id,
                    )
                ],
                depth=depth_unlock_progress(
                    session, config, TRACK_DISCRIMINATION,
                    learner_id=learner.learner_id,
                ).answered,
                sessions=[
                    p.answered
                    for p in session_accuracy(
                        session, TRACK_DISCRIMINATION,
                        learner_id=learner.learner_id,
                    )
                ],
            )

        mine, theirs = scoped(haru), scoped(mina)

    assert mine == dict(
        summary=1, holds=1, recent=["VWB"], letters=["VWB"], confusions=[],
        weakest=[tuple(p.pair) for p in default_pairs(config, top=2)],
        depth=1, sessions=[1],
    )
    assert theirs["summary"] == 3
    assert theirs["confusions"] == [tuple(sorted((a, b)))]
    assert tuple(target.pair) in theirs["weakest"]
    assert tuple(target.pair) not in mine["weakest"]
