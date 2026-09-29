"""데이터 모델과 DB 세션 관리 (PRD 8장).

스키마는 PRD 8장의 DDL 을 그대로 옮긴 것이다. 컬럼을 임의로 늘리지 않는다 —
`confusions` 에 "혼동쌍 목록에 있는 쌍인가"를 저장하지 않는 것도 그래서다.
그 판정은 `confusion_pairs` 설정에서 오고 설정은 바뀔 수 있으므로, 저장하지 않고
집계 시점에 `stats.py` 가 계산한다 (DECISIONS.md §18).

`items.track` 과 `confusions.track` 이 같은 값을 중복해서 들고 있는 것은 PRD 8장이
의도한 것이다 — 트랙별 집계에서 JOIN 을 피해 두 트랙이 섞일 경로를 하나 줄인다.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

__all__ = [
    "Base",
    "Learner",
    "TrainingSession",
    "Item",
    "Confusion",
    "DeviceLock",
    "LearnerDifficulty",
    "Database",
    "utcnow",
    "TRACK_DISCRIMINATION",
    "TRACK_WORD",
    "TRACKS",
    "RESULT_CORRECT",
    "RESULT_WRONG",
    "RESULT_LENGTH",
    "RESULT_ORDER",
    "RESULTS",
    "BOUNDARY_EXPLICIT",
    "BOUNDARY_DIRECT",
    "BOUNDARIES",
    "DEVICE_LOCK_ID",
    "load_difficulty",
    "save_difficulty",
    "count_answered_sessions",
]

# 트랙. 이 둘의 통계는 어떤 경로로도 섞이지 않는다 (PRD 9.2).
TRACK_DISCRIMINATION = "discrimination"
TRACK_WORD = "word"
TRACKS = (TRACK_DISCRIMINATION, TRACK_WORD)

# 판정 결과 (PRD 9.3).
RESULT_CORRECT = "correct"
RESULT_WRONG = "wrong"
RESULT_LENGTH = "length"
RESULT_ORDER = "order"
RESULTS = (RESULT_CORRECT, RESULT_WRONG, RESULT_LENGTH, RESULT_ORDER)

# 글자 경계 (난이도 ③축).
BOUNDARY_EXPLICIT = "explicit"
BOUNDARY_DIRECT = "direct"
BOUNDARIES = (BOUNDARY_EXPLICIT, BOUNDARY_DIRECT)

# device_lock 은 항상 한 줄이다. 두 세션이 동시에 손을 잡는 상태를 스키마
# 수준에서 불가능하게 만든다.
DEVICE_LOCK_ID = 1

DEFAULT_DATABASE_URL = "sqlite+pysqlite:///station.db"


def utcnow() -> datetime:
    """타임존 없는 UTC. SQLite 에 넣고 빼도 의미가 흔들리지 않는다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Learner(Base):
    __tablename__ = "learners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )


class TrainingSession(Base):
    """PRD 8장의 `sessions` 테이블.

    SQLAlchemy 의 `Session` 과 이름이 겹쳐 클래스명만 다르게 두었다.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 공부 세션(`source="study"`)만 NULL 이다. 공부는 이름을 받지 않는다 —
    # 기록이 없는데 이름을 받으면 학습자 표에 훈련한 적 없는 사람이 생긴다
    # (DECISIONS.md §63). 훈련 세션은 여전히 학습자를 가진다. 예전 DB 는
    # `NOT NULL` 이다 — `sessions_allow_no_learner()` 와 `tools/migrate_db.py`.
    learner_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("learners.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    plan_json: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    track: Mapped[str] = mapped_column(Text, nullable=False)
    letters: Mapped[str] = mapped_column(Text, nullable=False)
    hold_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    transition_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    boundary: Mapped[str] = mapped_column(Text, nullable=False)
    presented_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # 마지막 글자가 목표 자세에 도달한 시각. 응답 시간의 기준점이다 (PRD 8장).
    ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    answer: Mapped[Optional[str]] = mapped_column(Text)
    result: Mapped[Optional[str]] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Confusion(Base):
    __tablename__ = "confusions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("items.id"), nullable=False
    )
    # items.track 의 복제. 집계 시 JOIN 을 피하기 위한 것이며 PRD 8장이 그렇게
    # 정의했다. 기록할 때 항상 item 의 값을 그대로 넣는다.
    track: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    expected: Mapped[str] = mapped_column(Text, nullable=False)
    given: Mapped[str] = mapped_column(Text, nullable=False)


class LearnerDifficulty(Base):
    """학습자가 **트랙별로** 마지막에 도달한 난이도.

    세션이 끝날 때마다 갱신되고 다음 세션이 여기서 이어 시작한다. 이것이 없으면
    10세션을 해도 매번 3글자·또박또박·최대 유지에서 다시 시작한다 — 반복으로
    판별력을 기른다는 훈련 모델의 경로가 막힌다 (DECISIONS.md §40).

    **마지막 상태만 담는다.** 학습자·트랙당 한 행이고 시계열이 아니다. 추이가
    필요해지면 그때 이력 테이블을 따로 만든다.

    `transition_ms` 는 여기 없다. 난이도 축이 아니라 오답 재생에서만 늘리는
    값이라(PRD 9.1), 이월하면 다음 세션이 느린 재생 상태로 시작한다.
    """

    __tablename__ = "learner_difficulty"
    __table_args__ = (
        # 학습자·트랙당 한 행. 두 트랙의 난이도가 섞일 자리를 스키마에서 없앤다.
        UniqueConstraint("learner_id", "track", name="learner_difficulty_one_per_track"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    learner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("learners.id"), nullable=False
    )
    track: Mapped[str] = mapped_column(Text, nullable=False)
    length: Mapped[int] = mapped_column(Integer, nullable=False)
    hold_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    boundary: Mapped[str] = mapped_column(Text, nullable=False)
    # 감쇠(오래 쉰 학습자의 난이도를 낮추는 것)는 만들지 않았다. 나중에 판단할
    # 수 있도록 시각만 남긴다 (DECISIONS.md §40).
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DeviceLock(Base):
    __tablename__ = "device_lock"
    __table_args__ = (CheckConstraint("id = 1", name="device_lock_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("sessions.id")
    )
    acquired_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Database:
    """엔진 하나와 그 위의 세션 팩토리."""

    def __init__(self, url: str = DEFAULT_DATABASE_URL) -> None:
        self.url = url
        if ":memory:" in url:
            # 인메모리 DB 는 연결마다 별개다. 테스트에서 같은 DB 를 보려면
            # 연결 하나를 공유해야 한다.
            self._engine = create_engine(
                url, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        else:
            self._engine = create_engine(url)
        self._sessionmaker = sessionmaker(bind=self._engine, expire_on_commit=False)

    @classmethod
    def in_memory(cls) -> "Database":
        db = cls("sqlite+pysqlite:///:memory:")
        db.create_all()
        return db

    @property
    def engine(self):
        return self._engine

    def create_all(self) -> None:
        Base.metadata.create_all(self._engine)
        self._ensure_lock_row()

    def drop_all(self) -> None:
        Base.metadata.drop_all(self._engine)

    def sessions_allow_no_learner(self) -> bool:
        """`sessions.learner_id` 가 NULL 을 받는가. 예전 DB 는 받지 않는다.

        `create_all()` 은 있는 표를 고치지 않으므로, §63 이전에 만든 DB 는
        `tools/migrate_db.py` 를 돌리기 전까지 `NOT NULL` 그대로다. 그래도
        서버는 뜬다 — 막히는 것은 공부 시작 하나뿐이다.
        """
        with self._engine.connect() as connection:
            columns = connection.exec_driver_sql("PRAGMA table_info(sessions)").fetchall()
        for _cid, name, _type, notnull, _default, _pk in columns:
            if name == "learner_id":
                return not notnull
        return False

    def _ensure_lock_row(self) -> None:
        """device_lock 의 단일 행을 만들어 둔다. 없으면 락을 잡을 수 없다."""
        with self.session() as session:
            existing = session.get(DeviceLock, DEVICE_LOCK_ID)
            if existing is None:
                session.add(DeviceLock(id=DEVICE_LOCK_ID))
                session.commit()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """DB 세션 하나. 예외가 나면 롤백한다.

        커밋은 호출한 쪽이 한다 — 어디까지가 한 단위인지는 호출자가 안다.
        """
        session = self._sessionmaker()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def get_or_create_learner(session: Session, name: str) -> Learner:
    """이름으로 학습자를 찾고 없으면 만든다."""
    learner = session.scalar(select(Learner).where(Learner.name == name))
    if learner is None:
        learner = Learner(name=name, created_at=utcnow())
        session.add(learner)
        session.flush()
    return learner


def load_difficulty(
    session: Session, learner_id: int, track: str
) -> Optional[LearnerDifficulty]:
    """학습자의 해당 트랙 마지막 난이도. 이력이 없으면 None."""
    return session.scalar(
        select(LearnerDifficulty).where(
            LearnerDifficulty.learner_id == learner_id,
            LearnerDifficulty.track == track,
        )
    )


def save_difficulty(
    session: Session,
    learner_id: int,
    track: str,
    *,
    length: int,
    hold_ms: int,
    boundary: str,
    now: Optional[datetime] = None,
) -> LearnerDifficulty:
    """트랙별 난이도를 덮어쓴다. 학습자·트랙당 한 행이다."""
    row = load_difficulty(session, learner_id, track)
    if row is None:
        row = LearnerDifficulty(learner_id=learner_id, track=track)
        session.add(row)
    row.length = length
    row.hold_ms = hold_ms
    row.boundary = boundary
    row.updated_at = now or utcnow()
    session.flush()
    return row


def count_answered_sessions(session: Session, learner_id: int) -> int:
    """학습자가 **한 문항이라도 답한** 세션 수.

    `sessions` 행을 그대로 세지 않는다. 승인만 되고 아무도 참여하지 않은 세션,
    락을 못 잡고 곧바로 닫힌 세션(DECISIONS.md §49)도 행은 남는다. 그것을
    "누적 세션" 으로 세면 학습자가 하지 않은 일이 기록에 얹힌다.

    트랙을 가리지 않는다. 세는 것은 세션이지 성적이 아니다 — 정확도 같은
    지표는 여전히 트랙별로만 나온다 (`server/stats.py`).
    """
    return session.scalar(
        select(func.count(func.distinct(Item.session_id)))
        .join(TrainingSession, TrainingSession.id == Item.session_id)
        .where(
            TrainingSession.learner_id == learner_id,
            Item.result.is_not(None),
        )
    ) or 0
