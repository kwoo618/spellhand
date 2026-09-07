"""스키마와 세션 관리 테스트 (PRD 8장)."""

from datetime import datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from server.db import (
    BOUNDARIES,
    DEVICE_LOCK_ID,
    RESULTS,
    TRACKS,
    Confusion,
    Database,
    DeviceLock,
    Item,
    Learner,
    TrainingSession,
    get_or_create_learner,
    utcnow,
)


@pytest.fixture
def db():
    return Database.in_memory()


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
