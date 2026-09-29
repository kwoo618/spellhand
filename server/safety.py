"""안전 게이트와 세션 락 (PRD 10장, R4).

**모든 구동 요청은 예외 없이 이 경로를 통과한다.**

```
요청 → ① 세션 락 → ② 글자 존재 → ③ 커리큘럼 → ④ flex 조회·clamp
     → ⑤ 각도 변환·clamp → ⑥ rate limit → ⑦ 디바이스 idle → 전송
```

**⑧ 캘리브레이션 해시는 이 경로에 없다.** PRD 10장이 적은 대로 **연결 시점**에
한 번 확인한다 — `SerialDevice.connect()` 의 핸드셰이크에서 대조하고 불일치면
**연결 자체를 거부**해 서버가 기동하지 못한다 (DECISIONS.md §35, §48.2).

요청 단위로 옮기지 않는 이유: 게이트는 첫 구동 요청에서야 돌아간다. 그전에
기동·유휴 복귀로 나가는 안전 자세·중립 자세는 게이트를 지나지 않으므로, 해시가
틀린 펌웨어에 이미 잘못된 각도가 나간 뒤에 처음 검사하게 된다. 연결 시점 거부는
그 창을 아예 없앤다.

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
    "PlanSessionRestrictedError",
    "SessionLock",
    "SafetyGate",
    "PoseStep",
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


class PlanSessionRestrictedError(SafetyError):
    """승인된 계획으로 만든 세션에 호출자가 고른 글자를 구동하려 했다.

    게이트 ①~⑦ 의 하나가 아니다. 그 앞, 라우트에서 건다 — 계획 세션인지는
    `PlanStore` 가 알고 게이트는 모른다. 계획 세션은 `/planned` 로만 움직인다.
    승인은 "이 계획대로만 움직인다" 여야 하기 때문이다 (DECISIONS.md §62).
    """

    code = "PLAN_SESSION_RESTRICTED"
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


@dataclass(frozen=True)
class PoseStep:
    """글자 하나를 표현하는 동작. 목표 자세와, 있으면 먼저 거치는 경유 자세.

    둘 다 게이트 ④⑤ 를 지난 Pose 다. 글자 이름은 담지 않는다 (R2) — 경유가
    어느 글자인지는 설정만 안다 (DECISIONS.md §55).
    """

    pose: Pose
    approach: Optional[Pose] = None


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
        """`authorize_steps()` 와 같은 검사. 글자별 목표 Pose 만 돌려준다."""
        steps = self.authorize_steps(
            session_id, letters, track, depth_unlocked=depth_unlocked, purpose=purpose
        )
        return tuple(step.pose for step in steps)

    def authorize_steps(
        self,
        session_id: int,
        letters: str,
        track: str,
        *,
        depth_unlocked: bool,
        purpose: str = PURPOSE_ITEM,
    ) -> Tuple[PoseStep, ...]:
        """구동 요청 하나를 검사하고 글자별 PoseStep 을 돌려준다.

        경유 자세도 목표 자세와 같은 ④⑤ 를 지난다. 경유는 같은 구동 요청
        안의 동작이므로 rate limit·요청 수에 따로 잡히지 않는다.

        글자 → Pose 변환까지 여기서 하는 이유는, 게이트를 통과한 것과 실제로
        보내는 것이 같은 값임을 보장하기 위해서다. 중립 삽입은 `session.py` 가
        한다 (PRD 6.1).

        통과하면 이 요청을 구동 1건으로 계산한다.
        """
        text = letters.upper()

        # ① 세션 락
        self._require_holder(session_id)

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

        # ④ flex 조회·clamp / ⑤ 각도 변환·clamp — 경유 자세 포함
        steps = tuple(self._step_for(letter) for letter in text)

        # ⑥ ⑦ 과 요청 수
        self._admit(session_id)
        return steps

    def authorize_reference(self, session_id: int, name: str) -> Pose:
        """중립·안전 자세 **하나**를 보여 주는 구동 요청 (공부 모드).

        글자가 아니므로 ②③ 이 없다. 나머지 — ① 락, ④⑤ clamp, ⑥ rate limit,
        요청 수, ⑦ idle — 는 글자와 같은 코드를 지난다. "모든 구동 요청은
        예외 없이 이 경로를 통과한다" (PRD 10장) 를 지키려는 것이다.

        `name` 이 무엇인지는 호출자가 가린다. 여기서는 설정에 있는 키만 받는다.
        """
        self._require_holder(session_id)
        raw = {"neutral": self._config.neutral, "safe": self._config.safe}.get(name)
        if raw is None:
            raise CurriculumViolationError(
                f"reference pose is {name!r}, expected 'neutral' or 'safe'"
            )
        pose = self._pose_from(name, raw)
        self._admit(session_id)
        return pose

    def _require_holder(self, session_id: int) -> None:
        """게이트 ①."""
        holder = self._lock.holder()
        if holder != session_id:
            raise LockNotHeldError(
                f"session {session_id} does not hold the device lock "
                f"(holder={holder}); no motion is allowed without it"
            )

    def _admit(self, session_id: int) -> None:
        """게이트 ⑥ · 요청 수 · ⑦. 통과하면 구동 1건으로 센다."""
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

        # ⑧ 캘리브레이션 해시는 이 경로에 없다. `SerialDevice.connect()` 가
        #    핸드셰이크에서 대조하고 불일치면 연결을 거부한다 — 여기까지 왔다면
        #    해시는 이미 맞다 (DECISIONS.md §35, §48). 자리표시자를 두지 않는다.

        self._last_request_at = now
        self._request_counts[session_id] = used + 1

    def _step_for(self, letter: str) -> PoseStep:
        approach = self._config.approach_of(letter)
        return PoseStep(
            pose=self._pose_for(letter),
            approach=None if approach is None else self._pose_for(approach),
        )

    def _pose_for(self, letter: str) -> Pose:
        """게이트 ④⑤. flex 를 자르고, 각도로 바꿔 보아 범위를 확인한다."""
        return self._pose_from(letter, self._config.poses[letter])

    def _pose_from(self, letter: str, raw) -> Pose:
        """`_pose_for()` 의 본문. `letter` 는 경고에 쓰는 이름일 뿐이다."""
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
