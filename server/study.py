"""공부 모드 — 글자를 하나씩 눌러 손으로 본다 (DECISIONS.md §56).

**훈련이 아니다.** 채점도 적응도 없고, 그래서 **기록할 방법 자체가 없다.**
`SessionController` 를 재사용하지 않은 이유가 그것이다. 저쪽에는 `items` 에
쓰는 `_record()` 와 난이도를 저장하는 `_persist_difficulty()` 가 있고, 세션
하나를 "문항 0개" 로 열어 그 길을 막는 방식이면 막은 곳이 하나 풀리는 순간
공부가 통계로 샌다. 이 모듈은 `items` · `confusions` · `learner_difficulty`
를 import 하지 않는다 — 테스트가 그것을 고정한다.

**구동은 훈련과 같은 길을 지난다.** 손을 움직이므로 락이 필요하고, 모든 구동
요청은 안전 게이트를 지난다 (PRD 10장). 글자는 `authorize_steps()`, 중립·안전은
`authorize_reference()` 다. 경유(§55)도 세션과 같은 `move_step()` 이다.

락은 `sessions` 행을 요구한다 (`device_lock.session_id` 가 그 행을 가리킨다).
그래서 공부도 `sessions` 에 한 줄을 남기되 `source="study"` 로 가른다. 문항이
없는 행이므로 세션 수·정확도·추이 어디에도 잡히지 않는다 — 전부 `items` 에서
세기 때문이다 (`count_answered_sessions` 도 답한 세션만 센다).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from server.config import Config
from server.db import (
    TRACK_DISCRIMINATION,
    Database,
    TrainingSession,
    get_or_create_learner,
    utcnow,
)
from server.device import DeviceAdapter
from server.safety import PURPOSE_DEMO, DeviceNotIdleError, SafetyGate, SessionLock
from server.session import SessionStateError, move_step

__all__ = ["STUDY_SOURCE", "REFERENCE_POSES", "StudyController", "letters_payload"]

# `sessions.source` 의 값. PRD 8장의 agent|rule|manual 에 하나를 더했다 (§56).
STUDY_SOURCE = "study"

# 글자가 아닌데 공부 화면에서 볼 수 있는 자세. 이름은 `poses.json` 의 키다.
# 중립은 또박또박 문항에서 글자 사이에 지나가고, 안전은 손이 쉬는 자세다.
# 둘은 서로를 대신하지 않는다 (R3).
REFERENCE_POSES = ("neutral", "safe")


class StudyController:
    """공부 한 번. 락을 잡고, 누른 자세를 보여 주고, 락을 놓는다."""

    def __init__(
        self,
        config: Config,
        database: Database,
        device: DeviceAdapter,
        gate: SafetyGate,
        lock: SessionLock,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._config = config
        self._db = database
        self._device = device
        self._gate = gate
        self._lock = lock
        self._clock = clock
        self._session_id: Optional[int] = None
        self._learner_id: Optional[int] = None
        self._ended = False
        # 구동은 한 번에 하나. 겹치면 거부한다 — 세션의 `_drive_slot()` 과 같다.
        self._drive_lock = threading.Lock()

    @property
    def session_id(self) -> int:
        if self._session_id is None:
            raise SessionStateError("study has not been started")
        return self._session_id

    @property
    def learner_id(self) -> int:
        if self._learner_id is None:
            raise SessionStateError("study has not been started")
        return self._learner_id

    @property
    def ended(self) -> bool:
        return self._ended

    def start(self, learner_name: str) -> int:
        """`sessions` 행을 열고 락을 잡는다. 못 잡으면 행을 닫고 나간다 (§49)."""
        if self._session_id is not None:
            raise SessionStateError("this controller already ran a study")
        with self._db.session() as db:
            learner = get_or_create_learner(db, learner_name)
            row = TrainingSession(
                learner_id=learner.id,
                status="running",
                source=STUDY_SOURCE,
                plan_json=None,
                started_at=self._clock(),
            )
            db.add(row)
            db.commit()
            session_id, learner_id = row.id, learner.id
        try:
            self._lock.acquire(session_id)
        except Exception:
            self._close(session_id, status="aborted")
            self._ended = True
            raise
        self._session_id = session_id
        self._learner_id = learner_id
        return session_id

    @contextmanager
    def _drive_slot(self):
        if not self._drive_lock.acquire(blocking=False):
            raise DeviceNotIdleError(
                f"study {self.session_id} is already showing a pose; "
                f"wait for it to finish before pressing another"
            )
        try:
            yield
        finally:
            self._drive_lock.release()

    def _require_running(self) -> None:
        if self._ended:
            raise SessionStateError(f"study {self.session_id} has ended")

    def show_letter(self, letter: str) -> Tuple[str, int]:
        """글자 하나를 보여 준다. 구동 요청 1건이다. 동기다.

        트랙은 게이트의 유효성 검사에만 쓰인다 — 기록이 없으므로 통계에 섞일
        자리가 없다 (`/demo-letter` 와 같은 판단). 커리큘럼은 면제한다: 한
        글자는 깊이쌍이 될 수 없고, 공부는 채점 문항이 아니다.
        """
        with self._drive_slot():
            self._require_running()
            text = letter.strip().upper()
            if len(text) != 1:
                raise SessionStateError(
                    f"study shows exactly one letter, got {len(text)}"
                )
            steps = self._gate.authorize_steps(
                self.session_id,
                text,
                TRACK_DISCRIMINATION,
                depth_unlocked=True,
                purpose=PURPOSE_DEMO,
            )
            timing = self._config.timing
            for step in steps:
                self._lock.heartbeat(self.session_id)
                move_step(
                    self._device,
                    self._config,
                    step,
                    timing.transition_ms,
                    self.hold_ms,
                )
            return text, self.hold_ms

    def show_reference(self, name: str) -> Tuple[str, int]:
        """중립 또는 안전 자세를 보여 준다. 구동 요청 1건이다."""
        with self._drive_slot():
            self._require_running()
            if name not in REFERENCE_POSES:
                raise SessionStateError(
                    f"pose is {name!r}, expected one of {list(REFERENCE_POSES)}"
                )
            pose = self._gate.authorize_reference(self.session_id, name)
            self._lock.heartbeat(self.session_id)
            self._device.move_to(pose, self._config.timing.transition_ms, self.hold_ms)
            return name, self.hold_ms

    @property
    def hold_ms(self) -> int:
        """유지 시간. 설정의 하한이다.

        유지가 끝나도 손은 그 자세에 머문다 (펌웨어 IDLE, 유휴 복귀 전까지).
        그러므로 유지 시간은 "보이는 시간" 이 아니라 **다음 버튼이 잠기는
        시간**이다. 길게 두면 학습자가 비교하려고 누른 두 번째 글자를 기다리게
        된다.
        """
        return self._config.timing.hold_min_ms

    def heartbeat(self) -> None:
        self._require_running()
        self._lock.heartbeat(self.session_id)

    def end(self, *, status: str = "done") -> None:
        """행을 닫고, 손을 안전 자세로, 락을 놓는다. 두 번 불러도 안전하다."""
        session_id = self.session_id
        if self._ended:
            return
        self._close(session_id, status=status)
        self._ended = True
        try:
            if self._lock.holder() == session_id:
                # 락을 이미 잃었다면 손은 남의 것이다. 움직이지 않는다.
                self._device.safe()
        finally:
            self._lock.release(session_id)

    def _close(self, session_id: int, *, status: str) -> None:
        with self._db.session() as db:
            row = db.get(TrainingSession, session_id)
            if row is not None:
                row.status = status
                row.ended_at = self._clock()
                db.commit()


def letters_payload(config: Config) -> Dict[str, object]:
    """화면이 버튼을 그리는 목록. **전부 `poses.json` 에서 온다.**

    세션을 받지 않는다. 훈련의 버튼 입력도 이것을 쓰므로, 문항이 무엇이든
    같은 목록이 나가야 정답이 새지 않는다 (§56).

    혼동쌍·힌트는 싣지 않는다. 학습자 모드가 부르는 창구다 (§52, §56).
    """
    return {
        "letters": sorted(config.poses),
        "references": list(REFERENCE_POSES),
    }
