"""안전 게이트와 세션 락 (PRD 10장, R4).

**모든 구동 요청은 예외 없이 이 경로를 통과한다.**

```
요청 → ① 세션 락 → ② 글자 존재 → ③ 커리큘럼 → ④ flex 조회·clamp
     → ⑤ 각도 변환·clamp → ⑥ rate limit → ⑦ 디바이스 idle → ⑧ 캘리브레이션 해시
```

⑧ 은 `tools/gen_calibration.py` 가 없어 아직 구현되지 않았다. 자리를 비워 두고
`serial` 모드가 생길 때 채운다. 빈 껍데기를 만들지 않는다.

거부는 전부 `SafetyError` 하위 예외다. 각 예외는 `code` 와 `http_status` 를
들고 있어 상위 계층이 문자열을 새로 지어낼 필요가 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence, Tuple

from server.calibration import pose_to_angles
from server.config import FLEX_MAX, FLEX_MIN, Config
from server.db import (
    DEVICE_LOCK_ID,
    TRACK_DISCRIMINATION,
    TRACKS,
    Database,
    DeviceLock,
    utcnow,
)
from server.device import DeviceAdapter, Pose
from server.stats import item_contains_depth_pair

__all__ = [
    "SafetyError",
    "LockNotHeldError",
    "LockUnavailableError",
    "UnsupportedLettersError",
    "CurriculumViolationError",
    "RateLimitError",
    "SessionQuotaExceededError",
    "DeviceNotIdleError",
    "SessionLock",
    "SafetyGate",
    "PURPOSE_ITEM",
    "PURPOSE_DEMO",
    "MAX_DRIVE_REQUESTS_PER_SESSION",
    "LOCK_HEARTBEAT_SECONDS",
    "LOCK_EXPIRY_SECONDS",
]

logger = logging.getLogger(__name__)

# PRD 10장 "추가 제한". 설정 파일에 출처가 없어 여기 둔다 (DECISIONS.md §19).
MAX_DRIVE_REQUESTS_PER_SESSION = 200
LOCK_HEARTBEAT_SECONDS = 15
LOCK_EXPIRY_SECONDS = 60

# 구동 요청의 목적. 시범(demo)은 커리큘럼 검사에서 면제된다 (PRD 10장 ③) —
# 시범은 채점 문항이 아니라 "이 둘이 이렇게 다르다"를 보여 주는 것이므로,
# 아직 해금되지 않은 쌍이라도 보여 줄 수 있어야 한다.
PURPOSE_ITEM = "item"
PURPOSE_DEMO = "demo"


# --------------------------------------------------------------------------- #
# 구조화된 거부
# --------------------------------------------------------------------------- #

class SafetyError(Exception):
    """안전 게이트가 막은 요청. 모든 거부는 이 하위 타입이다."""

    code = "SAFETY_ERROR"
    http_status = 400


class LockNotHeldError(SafetyError):
    """요청한 세션이 디바이스 락 보유자가 아니다 (게이트 ①)."""

    code = "LOCK_NOT_HELD"
    http_status = 409


class LockUnavailableError(SafetyError):
    """다른 세션이 이미 락을 쥐고 있다.

    PRD 10장 표에는 코드가 없다. 게이트 ① 과는 다른 상황(획득 시도 실패)이라
    별도 코드를 두었다 (DECISIONS.md §21).
    """

    code = "LOCK_UNAVAILABLE"
    http_status = 409


class UnsupportedLettersError(SafetyError):
    """이 손이 만들 수 없는 글자가 들어 있다 (게이트 ②).

    LLM 이 잘못된 글자를 내놓는 경로를 여기서 끊는다.
    """

    code = "UNSUPPORTED_LETTERS"
    http_status = 400

    def __init__(self, letters: Sequence[str], alternatives: Sequence[str]) -> None:
        self.letters = tuple(letters)
        self.alternatives = tuple(alternatives)
        super().__init__(
            f"letters {list(self.letters)} are not in poses; "
            f"this hand can express {list(self.alternatives)}"
        )


class CurriculumViolationError(SafetyError):
    """커리큘럼이 아직 허용하지 않는 문항이다 (게이트 ③)."""

    code = "CURRICULUM_VIOLATION"
    http_status = 400


class RateLimitError(SafetyError):
    """직전 구동 요청으로부터 최소 간격이 지나지 않았다 (게이트 ⑥)."""

    code = "RATE_LIMIT"
    http_status = 429


class SessionQuotaExceededError(SafetyError):
    """세션당 구동 요청 상한을 넘었다.

    PRD 10장 "추가 제한"에는 코드가 없어 별도로 두었다 (DECISIONS.md §21).
    rate limit 과 원인이 다르므로 같은 코드로 묶지 않는다.
    """

    code = "SESSION_QUOTA"
    http_status = 429


class DeviceNotIdleError(SafetyError):
    """디바이스가 아직 움직이고 있다 (게이트 ⑦).

    `server.device.DeviceBusyError` 와 이름이 겹치지 않게 두었다. 저쪽은
    어댑터가 스스로 거절한 것이고, 이쪽은 게이트가 보내기 전에 막은 것이다.
    """

    code = "DEVICE_BUSY"
    http_status = 409


# --------------------------------------------------------------------------- #
# 세션 락
# --------------------------------------------------------------------------- #

class SessionLock:
    """디바이스 락. `device_lock` 테이블의 단일 행이 실체다.

    한 번에 한 세션만 손을 움직일 수 있다. 락을 쥔 세션이 죽으면 heartbeat 가
    멈추고, 만료 시간이 지나면 다음 세션이 가져갈 수 있다.
    """

    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], datetime] = utcnow,
        expiry_seconds: int = LOCK_EXPIRY_SECONDS,
    ) -> None:
        self._db = db
        self._clock = clock
        self._expiry = timedelta(seconds=expiry_seconds)

    def _row(self, session) -> DeviceLock:
        row = session.get(DeviceLock, DEVICE_LOCK_ID)
        if row is None:
            row = DeviceLock(id=DEVICE_LOCK_ID)
            session.add(row)
            session.flush()
        return row

    def _is_expired(self, row: DeviceLock, now: datetime) -> bool:
        if row.session_id is None:
            return True
        last = row.heartbeat_at or row.acquired_at
        if last is None:
            return True
        return now - last > self._expiry

    def holder(self) -> Optional[int]:
        """지금 락을 쥐고 있는 세션. 만료되었으면 None."""
        now = self._clock()
        with self._db.session() as session:
            row = self._row(session)
            if self._is_expired(row, now):
                return None
            return row.session_id

    def acquire(self, session_id: int) -> None:
        """락을 잡는다. 다른 세션이 살아 있으면 거부한다."""
        now = self._clock()
        with self._db.session() as session:
            row = self._row(session)
            if not self._is_expired(row, now) and row.session_id != session_id:
                raise LockUnavailableError(
                    f"device is held by session {row.session_id} "
                    f"(last heartbeat {row.heartbeat_at}); "
                    f"session {session_id} cannot acquire it"
                )
            row.session_id = session_id
            row.acquired_at = now
            row.heartbeat_at = now
            session.commit()

    def heartbeat(self, session_id: int) -> None:
        """락이 살아 있음을 알린다. 보유자가 아니면 거부한다."""
        now = self._clock()
        with self._db.session() as session:
            row = self._row(session)
            if row.session_id != session_id or self._is_expired(row, now):
                raise LockNotHeldError(
                    f"session {session_id} does not hold the device lock "
                    f"(holder={row.session_id})"
                )
            row.heartbeat_at = now
            session.commit()

    def release(self, session_id: int) -> None:
        """락을 놓는다. 보유자가 아니면 아무것도 하지 않는다."""
        with self._db.session() as session:
            row = self._row(session)
            if row.session_id != session_id:
                return
            row.session_id = None
            row.acquired_at = None
            row.heartbeat_at = None
            session.commit()

    def clear_expired(self) -> Optional[int]:
        """고아 락 정리. 서버 기동 시 부른다 (PRD 10장).

        정리한 세션 id 를 돌려준다. 정리할 것이 없으면 None.
        """
        now = self._clock()
        with self._db.session() as session:
            row = self._row(session)
            if row.session_id is None or not self._is_expired(row, now):
                return None
            orphan = row.session_id
            row.session_id = None
            row.acquired_at = None
            row.heartbeat_at = None
            session.commit()
            logger.warning(
                "cleared an orphaned device lock held by session %s "
                "(last heartbeat %s)",
                orphan,
                row.heartbeat_at,
            )
            return orphan


# --------------------------------------------------------------------------- #
# 안전 게이트
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Authorized:
    poses: Tuple[Pose, ...]


class SafetyGate:
    """구동 요청 하나를 검사하고 통과시킨다.

    **rate limit 은 구동 요청 단위다.** 글자 단위로 걸면 시퀀스가 스스로
    차단된다 — 시퀀스 안의 글자 간격은 전이 시간이 물리적으로 보장한다
    (PRD 10장).
    """

    def __init__(
        self,
        config: Config,
        device: DeviceAdapter,
        lock: SessionLock,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_requests_per_session: int = MAX_DRIVE_REQUESTS_PER_SESSION,
    ) -> None:
        self._config = config
        self._device = device
        self._lock = lock
        self._clock = clock
        self._max_requests = max_requests_per_session
        self._request_counts: dict = {}
        self._last_request_at: Optional[datetime] = None

    @property
    def request_count(self) -> int:
        return sum(self._request_counts.values())

    def requests_for(self, session_id: int) -> int:
        return self._request_counts.get(session_id, 0)

    def authorize_sequence(
        self,
        session_id: int,
        letters: str,
        track: str,
        *,
        depth_unlocked: bool,
        purpose: str = PURPOSE_ITEM,
    ) -> Tuple[Pose, ...]:
        """구동 요청 하나를 검사하고 글자별 Pose 를 돌려준다.

        글자 → Pose 변환까지 여기서 하는 이유는, 게이트를 통과한 것과 실제로
        보내는 것이 같은 값임을 보장하기 위해서다. 중립 삽입은 `session.py` 가
        한다 (PRD 6.1).

        통과하면 이 요청을 구동 1건으로 계산한다.
        """
        text = letters.upper()

        # ① 세션 락
        holder = self._lock.holder()
        if holder != session_id:
            raise LockNotHeldError(
                f"session {session_id} does not hold the device lock "
                f"(holder={holder}); no motion is allowed without it"
            )

        # ② 글자 존재
        unknown = sorted({c for c in text if c not in self._config.poses})
        if unknown:
            raise UnsupportedLettersError(unknown, sorted(self._config.poses))

        # ③ 커리큘럼
        if track not in TRACKS:
            raise CurriculumViolationError(
                f"track is {track!r}, expected one of {list(TRACKS)}"
            )
        if (
            purpose == PURPOSE_ITEM
            and track == TRACK_DISCRIMINATION
            and not depth_unlocked
            and item_contains_depth_pair(self._config, text)
        ):
            raise CurriculumViolationError(
                f"{text!r} contains a depth pair but depth pairs are not "
                f"unlocked yet for this learner"
            )

        # ④ flex 조회·clamp / ⑤ 각도 변환·clamp
        poses = tuple(self._pose_for(letter) for letter in text)

        # ⑥ rate limit — 요청 단위
        now = self._clock()
        minimum = timedelta(milliseconds=self._config.timing.transition_ms)
        if self._last_request_at is not None and now - self._last_request_at < minimum:
            raise RateLimitError(
                f"only {(now - self._last_request_at).total_seconds() * 1000:.0f}ms "
                f"since the previous drive request, need at least "
                f"{self._config.timing.transition_ms}ms"
            )

        # 세션당 구동 요청 상한
        used = self._request_counts.get(session_id, 0)
        if used >= self._max_requests:
            raise SessionQuotaExceededError(
                f"session {session_id} has already made {used} drive requests, "
                f"the limit is {self._max_requests}"
            )

        # ⑦ 디바이스 idle
        if self._device.is_busy:
            raise DeviceNotIdleError(
                "the device is still moving; wait for the current motion to finish"
            )

        # ⑧ 캘리브레이션 해시 — tools/gen_calibration.py 이후. serial 모드가
        #    생길 때 여기에 들어간다.

        self._last_request_at = now
        self._request_counts[session_id] = used + 1
        return poses

    def _pose_for(self, letter: str) -> Pose:
        """게이트 ④⑤. flex 를 자르고, 각도로 바꿔 보아 범위를 확인한다."""
        raw = self._config.poses[letter]
        clamped = {}
        blocked = []
        for finger, value in raw.items():
            bounded = min(max(float(value), FLEX_MIN), FLEX_MAX)
            if bounded != value:
                blocked.append((finger, value, bounded))
            clamped[finger] = bounded

        if blocked:
            # 설정 검증을 통과한 자세라면 여기 올 수 없다. 왔다면 검증을 우회한
            # 경로가 있다는 뜻이므로 조용히 넘기지 않는다.
            logger.warning(
                "blocked: clamped out-of-range flex for letter %r: %s",
                letter,
                ", ".join(
                    f"{finger} {value} -> {bounded}"
                    for finger, value, bounded in blocked
                ),
            )

        pose = Pose.from_config(self._config, clamped)
        # 각도 변환은 캘리브레이션 계층이 다시 한 번 자르고 경고한다. 결과를
        # 쓰지는 않지만(퍼밀로 보내므로) 범위를 벗어나는 자세를 여기서 걸러낸다.
        pose_to_angles(self._config, pose.flex)
        return pose
