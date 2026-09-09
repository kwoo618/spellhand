"""스키마와 세션 관리 테스트 (PRD 8장)."""

from datetime import datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from server.db import (
    BOUNDARIES,
    DEVICE_LOCK_ID,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    RESULTS,
    TRACKS,
    Confusion,
    Database,
    DeviceLock,
    Item,
    Learner,
    LearnerDifficulty,
    TrainingSession,
    load_difficulty,
    save_difficulty,
    get_or_create_learner,
    utcnow,
)


def make_session(session, *, name="haru", status="running"):
    learner = get_or_create_learner(session, name)
    row = TrainingSession(
        learner_id=learner.id, status=status, source="rule", started_at=utcnow()
    )
    session.add(row)
    session.flush()
    return learner, row


# --------------------------------------------------------------------------- #
# 스키마
# --------------------------------------------------------------------------- #

def test_all_tables_are_created(db):
    tables = set(inspect(db.engine).get_table_names())
    assert tables == {
        "learners",
        "sessions",
        "items",
        "confusions",
        "device_lock",
        "learner_difficulty",
    }


def test_constants_match_the_schema_comments():
    assert TRACKS == ("discrimination", "word")
    assert RESULTS == ("correct", "wrong", "length", "order")
    assert BOUNDARIES == ("explicit", "direct")


def test_device_lock_row_exists_after_create_all(db):
    with db.session() as session:
        row = session.get(DeviceLock, DEVICE_LOCK_ID)
        assert row is not None
        assert row.session_id is None


def test_learner_names_are_unique(db):
    with db.session() as session:
        session.add(Learner(name="haru", created_at=utcnow()))
        session.commit()
    with pytest.raises(IntegrityError):
        with db.session() as session:
            session.add(Learner(name="haru", created_at=utcnow()))
            session.commit()


def test_get_or_create_learner_is_idempotent(db):
    with db.session() as session:
        first = get_or_create_learner(session, "haru")
        session.commit()
    with db.session() as session:
        again = get_or_create_learner(session, "haru")
        session.commit()
        assert again.id == first.id


# --------------------------------------------------------------------------- #
# 문항과 혼동
# --------------------------------------------------------------------------- #

def test_item_round_trips(db):
    with db.session() as session:
        _, training = make_session(session)
        now = utcnow()
        session.add(
            Item(
                session_id=training.id,
                seq=1,
                track="discrimination",
                letters="VWB",
                hold_ms=3000,
                transition_ms=900,
                boundary="explicit",
                presented_at=now,
                ready_at=now,
                answered_at=now,
                answer="VWS",
                result="wrong",
                attempt=1,
            )
        )
        session.commit()

    with db.session() as session:
        item = session.scalar(select(Item))
        assert item.letters == "VWB"
        assert item.result == "wrong"
        assert item.attempt == 1
        assert isinstance(item.ready_at, datetime)


def test_attempt_defaults_to_one(db):
    with db.session() as session:
        _, training = make_session(session)
        session.add(
            Item(
                session_id=training.id,
                seq=1,
                track="word",
                letters="WISE",
                hold_ms=3000,
                transition_ms=900,
                boundary="explicit",
            )
        )
        session.commit()
        assert session.scalar(select(Item)).attempt == 1


def test_confusion_carries_its_own_track(db):
    """PRD 8장이 items.track 을 confusions 에 복제해 둔 이유는 집계에서 JOIN 을
    피하기 위해서다. 이 복제가 유지되는지 고정한다."""
    with db.session() as session:
        _, training = make_session(session)
        item = Item(
            session_id=training.id,
            seq=1,
            track="word",
            letters="WISE",
            hold_ms=3000,
            transition_ms=900,
            boundary="explicit",
            result="wrong",
        )
        session.add(item)
        session.flush()
        session.add(
            Confusion(
                item_id=item.id,
                track=item.track,
                position=1,
                expected="I",
                given="O",
            )
        )
        session.commit()

        confusion = session.scalar(select(Confusion))
        assert confusion.track == "word"


def test_confusions_have_no_listed_column(db):
    """혼동쌍 목록 여부는 저장하지 않는다.

    `confusion_pairs` 설정이 바뀌면 과거 기록의 의미가 달라져야 하므로,
    저장하지 않고 집계 시점에 계산한다 (DECISIONS.md §18).
    """
    columns = {c["name"] for c in inspect(db.engine).get_columns("confusions")}
    assert columns == {"id", "item_id", "track", "position", "expected", "given"}


# --------------------------------------------------------------------------- #
# 세션 관리
# --------------------------------------------------------------------------- #

def test_session_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.session() as session:
            session.add(Learner(name="temporary", created_at=utcnow()))
            session.flush()
            raise RuntimeError("boom")

    with db.session() as session:
        assert session.scalars(select(Learner)).all() == []


def test_uncommitted_work_does_not_persist(db):
    with db.session() as session:
        session.add(Learner(name="ghost", created_at=utcnow()))
        # 커밋하지 않는다.

    with db.session() as session:
        assert session.scalars(select(Learner)).all() == []


def test_create_all_is_safe_to_run_twice(db):
    db.create_all()
    with db.session() as session:
        rows = session.scalars(select(DeviceLock)).all()
        assert len(rows) == 1


def test_file_backed_database_persists(tmp_path):
    path = tmp_path / "station.db"
    first = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    first.create_all()
    with first.session() as session:
        get_or_create_learner(session, "haru")
        session.commit()

    second = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    with second.session() as session:
        assert session.scalar(select(Learner).where(Learner.name == "haru"))


# --------------------------------------------------------------------------- #
# 학습자 난이도 이력
# --------------------------------------------------------------------------- #

def test_difficulty_is_absent_for_a_new_learner(db):
    with db.session() as session:
        learner = get_or_create_learner(session, "haru")
        session.commit()
        assert load_difficulty(session, learner.id, TRACK_DISCRIMINATION) is None


def test_difficulty_round_trips(db):
    with db.session() as session:
        learner = get_or_create_learner(session, "haru")
        save_difficulty(
            session,
            learner.id,
            TRACK_DISCRIMINATION,
            length=5,
            hold_ms=1564,
            boundary="direct",
        )
        session.commit()

    with db.session() as session:
        row = load_difficulty(session, learner.id, TRACK_DISCRIMINATION)
        assert (row.length, row.hold_ms, row.boundary) == (5, 1564, "direct")
        assert isinstance(row.updated_at, datetime)


def test_saving_twice_overwrites_rather_than_appends(db):
    """마지막 상태만 담는다. 시계열이 아니다."""
    with db.session() as session:
        learner = get_or_create_learner(session, "haru")
        for hold in (3000, 2550, 2167):
            save_difficulty(
                session,
                learner.id,
                TRACK_DISCRIMINATION,
                length=3,
                hold_ms=hold,
                boundary="explicit",
            )
        session.commit()

    with db.session() as session:
        rows = session.scalars(select(LearnerDifficulty)).all()
        assert len(rows) == 1
        assert rows[0].hold_ms == 2167


def test_the_two_tracks_have_separate_rows(db):
    """판별 난이도와 단어 난이도는 별개다."""
    with db.session() as session:
        learner = get_or_create_learner(session, "haru")
        save_difficulty(
            session, learner.id, TRACK_DISCRIMINATION,
            length=7, hold_ms=800, boundary="direct",
        )
        save_difficulty(
            session, learner.id, TRACK_WORD,
            length=3, hold_ms=3000, boundary="explicit",
        )
        session.commit()

    with db.session() as session:
        disc = load_difficulty(session, learner.id, TRACK_DISCRIMINATION)
        word = load_difficulty(session, learner.id, TRACK_WORD)
        assert (disc.length, disc.hold_ms) == (7, 800)
        assert (word.length, word.hold_ms) == (3, 3000)


def test_learners_do_not_share_difficulty(db):
    with db.session() as session:
        haru = get_or_create_learner(session, "haru")
        mina = get_or_create_learner(session, "mina")
        save_difficulty(
            session, haru.id, TRACK_DISCRIMINATION,
            length=7, hold_ms=800, boundary="direct",
        )
        session.commit()

    with db.session() as session:
        assert load_difficulty(session, mina.id, TRACK_DISCRIMINATION) is None


def test_one_row_per_learner_and_track_is_enforced(db):
    """스키마 수준에서 막는다. 코드가 실수해도 두 행이 생기지 않는다."""
    with db.session() as session:
        learner = get_or_create_learner(session, "haru")
        session.add_all([
            LearnerDifficulty(
                learner_id=learner.id, track=TRACK_DISCRIMINATION,
                length=3, hold_ms=3000, boundary="explicit", updated_at=utcnow(),
            ),
            LearnerDifficulty(
                learner_id=learner.id, track=TRACK_DISCRIMINATION,
                length=5, hold_ms=2000, boundary="direct", updated_at=utcnow(),
            ),
        ])
        with pytest.raises(IntegrityError):
            session.commit()


def test_difficulty_has_no_transition_column(db):
    """transition_ms 는 난이도 축이 아니다 (PRD 9.1).

    이월하면 다음 세션이 오답 재생 상태(1800ms)로 시작한다.
    """
    columns = {
        c["name"] for c in inspect(db.engine).get_columns("learner_difficulty")
    }
    assert columns == {
        "id", "learner_id", "track", "length", "hold_ms", "boundary", "updated_at"
    }
