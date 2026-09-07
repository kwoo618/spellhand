"""훈련 세션 루프와 적응 (PRD 9.4, 기획서 5.2).

흐름: 세션 시작 → 문항 생성 → 로봇 구동 → 응답 수집 → 판정 → 기록 → 적응 → 다음

적응은 **규칙 기반**이다. LLM 호출은 없다. 다만 나중에 LLM 이 이 자리를 대신할
수 있도록 `Adaptation` 을 갈아끼울 수 있게 분리해 두었다 — 세션 루프는 어떤
규칙으로 난이도가 바뀌는지 모른다.

글자 → Pose 변환과 중립 삽입은 여기서 한다 (PRD 6.1). 이 아래로는 글자가
내려가지 않는다 (R2).
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Tuple

from server import judge as judge_module
from server.config import Config, WordSet
from server.db import (
    BOUNDARIES,
    BOUNDARY_DIRECT,
    BOUNDARY_EXPLICIT,
    RESULT_CORRECT,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    Confusion,
    Database,
    Item,
    TrainingSession,
    get_or_create_learner,
    utcnow,
)
from server.device import DeviceAdapter
from server.items import (
    RECENT_ITEM_WINDOW,
    Difficulty,
    ItemSpec,
    generate_discrimination_items,
    generate_word_items,
    heavy_letter_caps,
)
from server.safety import PURPOSE_DEMO, PURPOSE_ITEM, SafetyGate, SessionLock
from server.stats import (
    PairCount,
    TrackSummary,
    confusion_counts,
    depth_unlocked,
    recent_letters,
    track_summary,
    weakest_pairs,
)

__all__ = [
    "HOLD_DECAY_ON_CORRECT",
    "DIFFICULTY_LADDER",
    "ItemPrompt",
    "DemoShown",
    "ItemOutcome",
    "SessionPlan",
    "SessionSummary",
    "Adaptation",
    "RuleAdaptation",
    "SessionRunner",
]

logger = logging.getLogger(__name__)

# PRD 9.4. 정답 시 유지 시간을 줄이는 비율. 3000 -> 2550 -> 2167 -> 2000(바닥).
# poses.json 에 출처가 없어 여기 둔다 (DECISIONS.md §19).
HOLD_DECAY_ON_CORRECT = 0.85

# ①축이 바닥을 친 뒤 넘어갈 ②·③축 순서 (PRD 9.4: length 3→5 → direct → 5→7).
DIFFICULTY_LADDER: Tuple[Tuple[int, str], ...] = (
    (3, BOUNDARY_EXPLICIT),
    (5, BOUNDARY_EXPLICIT),
    (5, BOUNDARY_DIRECT),
    (7, BOUNDARY_DIRECT),
)


# --------------------------------------------------------------------------- #
# 주고받는 값
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ItemPrompt:
    """**클라이언트로 나가는** 문항 표현.

    정답은 여기 없다. 이 객체는 그대로 직렬화되어 브라우저로 가므로, 정답이
    필드로 들어 있으면 화면에 띄우지 않아도 응답 본문에서 읽힌다. 정답은 서버가
    `_AnswerKey` 로 따로 들고 있다 (DECISIONS.md §22).

    새 필드를 추가할 때는 그 값이 정답을 되돌릴 수 있는지 먼저 확인할 것.
    `length` 는 이미 나가는 정보다 (PRD 14.1 화면이 길이를 표시한다).
    """

    seq: int
    track: str
    length: int
    boundary: str
    hold_ms: int
    transition_ms: int
    attempt: int


@dataclass(frozen=True)
class _AnswerKey:
    """서버만 보는 정답. 클라이언트 표현과 타입이 아예 다르다.

    `ItemPrompt` 와 섞이지 않도록 별도 타입으로 둔다 — dict 나 문자열로 들고
    다니면 언젠가 응답 payload 에 얹힌다.
    """

    seq: int
    letters: str


class AnswerProvider(Protocol):
    """세션 루프가 응답을 받아 오는 방법.

    `expected` 는 **인자로만** 전달된다. `prompt` 의 필드가 아니므로 클라이언트
    표현을 직렬화할 때 딸려 나갈 수 없다. 실제 웹 어댑터는 이 인자를 무시하고
    브라우저에서 받은 문자열을 돌려준다.
    """

    def __call__(self, prompt: ItemPrompt, expected: str) -> str: ...


@dataclass(frozen=True)
class DemoShown:
    """오답 2회 후 나란히 보여 준 두 글자와 힌트."""

    expected: str
    given: str
    hint: Optional[str]


@dataclass(frozen=True)
class ItemOutcome:
    seq: int
    track: str
    letters: str
    attempt: int
    answer: str
    result: str
    confusions: Tuple[judge_module.Confusion, ...]
    demo: Optional[DemoShown] = None


@dataclass(frozen=True)
class SessionPlan:
    """세션 구성. 기본은 판별 8 + 통글자 4 (PRD 9.5)."""

    discrimination_items: int = 8
    word_items: int = 4

    def as_json(self) -> str:
        return json.dumps(
            {
                "discrimination_items": self.discrimination_items,
                "word_items": self.word_items,
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class SessionSummary:
    """세션 요약. **트랙별로만** 나온다.

    통합 정확도 같은 것은 없다. 두 트랙을 합친 숫자는 의미가 없기 때문이다
    (기획서 3.3).
    """

    session_id: int
    learner_id: int
    by_track: Mapping[str, TrackSummary]
    confusions_by_track: Mapping[str, Tuple[PairCount, ...]]
    outcomes: Tuple[ItemOutcome, ...]


# --------------------------------------------------------------------------- #
# 적응 규칙
# --------------------------------------------------------------------------- #

class Adaptation(Protocol):
    """난이도를 다음 문항으로 옮기는 규칙.

    LLM 이 나중에 이 자리를 대신할 수 있다. 세션 루프는 구현을 모른다.
    """

    def on_correct(self, difficulty: Difficulty, config: Config) -> Difficulty: ...

    def on_wrong_first(
        self, difficulty: Difficulty, config: Config
    ) -> Difficulty: ...

    def on_wrong_second(
        self, difficulty: Difficulty, config: Config
    ) -> Difficulty: ...


class RuleAdaptation:
    """PRD 9.4 의 규칙을 그대로 옮긴 것."""

    def on_correct(self, difficulty: Difficulty, config: Config) -> Difficulty:
        timing = config.timing
        decayed = int(difficulty.hold_ms * HOLD_DECAY_ON_CORRECT)
        hold = min(max(decayed, timing.hold_min_ms), timing.hold_max_ms)

        if hold < difficulty.hold_ms:
            # ①축이 아직 남아 있다.
            return replace(difficulty, hold_ms=hold)

        rung = _next_rung(difficulty)
        if not rung:
            # 사다리 꼭대기. 더 올릴 축이 없다.
            return replace(difficulty, hold_ms=hold)

        # ①축 바닥에서 ②·③축으로 올라간다. 이때 **유지 시간을 되돌린다.**
        # 새 단에서 ①축을 다시 걸어 내려갈 수 있어야 세 축이 독립적으로
        # 움직인다 (DECISIONS.md §23).
        return replace(difficulty, hold_ms=timing.hold_default_ms, **rung)

    def on_wrong_first(self, difficulty: Difficulty, config: Config) -> Difficulty:
        timing = config.timing
        return replace(
            difficulty,
            hold_ms=timing.hold_max_ms,
            transition_ms=timing.transition_slow_ms,
            boundary=BOUNDARY_EXPLICIT,
        )

    def on_wrong_second(self, difficulty: Difficulty, config: Config) -> Difficulty:
        timing = config.timing
        return replace(
            difficulty,
            length=max(DIFFICULTY_LADDER[0][0], difficulty.length - 1),
            hold_ms=timing.hold_default_ms,
            transition_ms=timing.transition_ms,
        )


def _rung_rank(length: int, boundary: str) -> Tuple[int, int]:
    """사다리 위 위치. 길이가 먼저고, 같은 길이면 direct 가 더 어렵다."""
    return (length, BOUNDARIES.index(boundary))


def _next_rung(difficulty: Difficulty) -> Dict[str, object]:
    """지금보다 한 단 어려운 칸. 꼭대기면 빈 dict.

    사다리에 정확히 없는 조합에서도 답을 낸다. `on_wrong_second` 가 길이를
    1 줄이면 (4, explicit) 처럼 사다리에 없는 자리에 서게 되는데, 그때 "다음
    칸 없음"으로 처리하면 후퇴한 학습자가 다시는 올라가지 못한다.
    """
    current = _rung_rank(difficulty.length, difficulty.boundary)
    for length, boundary in DIFFICULTY_LADDER:
        if _rung_rank(length, boundary) > current:
            return {"length": length, "boundary": boundary}
    return {}


# --------------------------------------------------------------------------- #
# 세션 루프
# --------------------------------------------------------------------------- #

class SessionRunner:
    """한 세션을 끝까지 돌린다."""

    def __init__(
        self,
        config: Config,
        words: WordSet,
        database: Database,
        device: DeviceAdapter,
        gate: SafetyGate,
        lock: SessionLock,
        *,
        adaptation: Optional[Adaptation] = None,
        clock: Callable[[], datetime] = utcnow,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._config = config
        self._words = words
        self._db = database
        self._device = device
        self._gate = gate
        self._lock = lock
        self._adaptation = adaptation or RuleAdaptation()
        self._clock = clock
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------ #

    def run(
        self,
        learner_name: str,
        answer_provider: AnswerProvider,
        *,
        plan: Optional[SessionPlan] = None,
        source: str = "rule",
    ) -> SessionSummary:
        plan = plan or SessionPlan()
        session_id, learner_id = self._open_session(learner_name, plan, source)
        self._lock.acquire(session_id)

        outcomes: List[ItemOutcome] = []
        try:
            difficulty = Difficulty(
                length=DIFFICULTY_LADDER[0][0],
                hold_ms=self._config.timing.hold_default_ms,
                transition_ms=self._config.timing.transition_ms,
                boundary=DIFFICULTY_LADDER[0][1],
            )
            seq = 0
            # heavy_letters 비율은 세션 전체 문항 수 기준이다. 문항을 하나씩
            # 만들더라도 예산은 여기서 한 번 잡고 소진해 나간다.
            heavy_caps = heavy_letter_caps(
                self._config, plan.discrimination_items
            )
            heavy_used: Dict[str, int] = {letter: 0 for letter in heavy_caps}
            for track, count in (
                (TRACK_DISCRIMINATION, plan.discrimination_items),
                (TRACK_WORD, plan.word_items),
            ):
                for _ in range(count):
                    seq += 1
                    self._lock.heartbeat(session_id)
                    difficulty, item_outcomes = self._run_one_item(
                        session_id=session_id,
                        learner_id=learner_id,
                        seq=seq,
                        track=track,
                        difficulty=difficulty,
                        answer_provider=answer_provider,
                        heavy_caps=heavy_caps,
                        heavy_used=heavy_used,
                    )
                    outcomes.extend(item_outcomes)
            self._close_session(session_id, status="done")
        except Exception:
            self._close_session(session_id, status="aborted")
            raise
        finally:
            # 세션이 어떻게 끝나든 손은 쉬는 자세로 돌아간다 (R3).
            self._device.safe()
            self._lock.release(session_id)

        return self._summarize(session_id, learner_id, tuple(outcomes))

    # ------------------------------------------------------------------ #

    def _run_one_item(
        self,
        *,
        session_id: int,
        learner_id: int,
        seq: int,
        track: str,
        difficulty: Difficulty,
        answer_provider: AnswerProvider,
        heavy_caps: Mapping[str, int],
        heavy_used: Dict[str, int],
    ) -> Tuple[Difficulty, Tuple[ItemOutcome, ...]]:
        """문항 하나를 최대 2회 시도까지 진행하고, 다음 난이도를 돌려준다."""
        spec = self._generate(
            session_id, learner_id, track, difficulty, heavy_caps, heavy_used
        )
        for letter in heavy_used:
            if letter in spec.letters:
                heavy_used[letter] += 1
        outcomes: List[ItemOutcome] = []

        for attempt in (1, 2):
            unlocked = self._depth_unlocked(learner_id)
            presented_at, ready_at = self._present(
                session_id, spec, depth_unlocked=unlocked
            )
            prompt = ItemPrompt(
                seq=seq,
                track=spec.track,
                length=len(spec.letters),
                boundary=spec.boundary,
                hold_ms=spec.hold_ms,
                transition_ms=spec.transition_ms,
                attempt=attempt,
            )
            # 정답은 서버가 들고 있고 응답기에는 별도 인자로만 건넨다.
            # 클라이언트로 나가는 것은 prompt 뿐이다.
            key = _AnswerKey(seq=seq, letters=spec.letters)
            answer = answer_provider(prompt, key.letters)
            answered_at = self._clock()
            judgement = judge_module.judge(key.letters, answer)

            self._record(
                session_id=session_id,
                seq=seq,
                spec=spec,
                attempt=attempt,
                answer=answer,
                judgement=judgement,
                presented_at=presented_at,
                ready_at=ready_at,
                answered_at=answered_at,
            )

            if judgement.result == RESULT_CORRECT:
                outcomes.append(
                    _outcome(seq, spec, attempt, answer, judgement, demo=None)
                )
                return self._adaptation.on_correct(difficulty, self._config), tuple(
                    outcomes
                )

            if attempt == 1:
                # 같은 문항을 느린 속도·또박또박으로 다시 낸다. 정답은 알려주지
                # 않는다 (기획서 5.2).
                outcomes.append(
                    _outcome(seq, spec, attempt, answer, judgement, demo=None)
                )
                retried = self._adaptation.on_wrong_first(difficulty, self._config)
                spec = ItemSpec(
                    track=spec.track,
                    letters=spec.letters,
                    hold_ms=retried.hold_ms,
                    transition_ms=retried.transition_ms,
                    boundary=BOUNDARY_EXPLICIT,
                )
                difficulty = retried
                continue

            # 2회째 오답. 혼동한 두 글자를 나란히 시범한 뒤 후퇴한다.
            demo = self._demo_pair(session_id, spec.track, judgement)
            outcomes.append(
                _outcome(seq, spec, attempt, answer, judgement, demo=demo)
            )
            return self._adaptation.on_wrong_second(difficulty, self._config), tuple(
                outcomes
            )

        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ #

    def _generate(
        self,
        session_id: int,
        learner_id: int,
        track: str,
        difficulty: Difficulty,
        heavy_caps: Mapping[str, int],
        heavy_used: Mapping[str, int],
    ) -> ItemSpec:
        with self._db.session() as db:
            recent = recent_letters(
                db, track, window=RECENT_ITEM_WINDOW, learner_id=learner_id
            )
            if track == TRACK_DISCRIMINATION:
                unlocked = depth_unlocked(
                    db, self._config, track, learner_id=learner_id
                )
                pairs = weakest_pairs(
                    db,
                    self._config,
                    track,
                    top=2,
                    exclude_depth=not unlocked,
                    learner_id=learner_id,
                )
                return generate_discrimination_items(
                    self._config,
                    count=1,
                    difficulty=difficulty,
                    recent=recent,
                    target_pairs=pairs,
                    depth_unlocked=unlocked,
                    rng=self._rng,
                    heavy_caps=heavy_caps,
                    heavy_used=heavy_used,
                )[0]

            return generate_word_items(
                self._config,
                self._words,
                count=1,
                difficulty=difficulty,
                recent=recent,
                rng=self._rng,
            )[0]

    def _depth_unlocked(self, learner_id: int) -> bool:
        with self._db.session() as db:
            return depth_unlocked(
                db, self._config, TRACK_DISCRIMINATION, learner_id=learner_id
            )

    def _present(
        self, session_id: int, spec: ItemSpec, *, depth_unlocked: bool
    ) -> Tuple[datetime, datetime]:
        """문항 하나를 손으로 표현한다. 구동 요청 1건이다.

        중립 삽입은 여기서 한다. 어댑터 아래로는 글자가 내려가지 않는다 (R2).
        """
        poses = self._gate.authorize_sequence(
            session_id,
            spec.letters,
            spec.track,
            depth_unlocked=depth_unlocked,
            purpose=PURPOSE_ITEM,
        )
        presented_at = self._clock()
        for index, pose in enumerate(poses):
            # 글자마다 heartbeat 를 찍는다. 문항 단위로는 부족하다 — 7글자를
            # 느린 재생으로 내면 한 문항이 락 만료 시간을 넘길 수 있다
            # (DECISIONS.md §25).
            self._lock.heartbeat(session_id)
            if index and spec.boundary == BOUNDARY_EXPLICIT:
                # 또박또박 모드: 글자 사이에 중립 자세로 경계를 만든다.
                self._device.neutral()
            self._device.move_to(pose, spec.transition_ms, spec.hold_ms)

        # 관찰 시간의 기준점은 마지막 글자의 **도달** 시점이다. 동기 어댑터는
        # 유지까지 끝난 뒤에 돌아오므로 유지 시간만큼 되돌려 잡는다
        # (DECISIONS.md §24).
        ready_at = self._clock() - timedelta(milliseconds=spec.hold_ms)
        return presented_at, ready_at

    def _demo_pair(
        self, session_id: int, track: str, judgement: judge_module.Judgement
    ) -> Optional[DemoShown]:
        """혼동 첫 위치의 두 글자를 나란히 보여 준다 (PRD 9.4).

        `order` 결과에는 혼동 기록이 없으므로 시범 대상이 없다. 그때는 시범
        없이 후퇴만 한다.
        """
        if not judgement.confusions:
            return None

        first = judgement.confusions[0]
        letters = first.expected + first.given
        poses = self._gate.authorize_sequence(
            session_id,
            letters,
            track,
            depth_unlocked=True,  # 시범은 커리큘럼 검사 면제 (PRD 10장 ③)
            purpose=PURPOSE_DEMO,
        )
        hold = self._config.timing.hold_max_ms
        for index, pose in enumerate(poses):
            self._lock.heartbeat(session_id)
            if index:
                self._device.neutral()
            self._device.move_to(pose, self._config.timing.transition_ms, hold)

        return DemoShown(
            expected=first.expected,
            given=first.given,
            hint=self._hint_for(first.expected, first.given),
        )

    def _hint_for(self, a: str, b: str) -> Optional[str]:
        """혼동쌍 힌트. 목록에 없는 쌍이면 힌트가 없다 — 시범 자체는 보여 준다."""
        key = frozenset((a, b))
        for pair in self._config.confusion_pairs:
            if frozenset(pair.pair) == key:
                return pair.hint
        return None

    # ------------------------------------------------------------------ #

    def _open_session(
        self, learner_name: str, plan: SessionPlan, source: str
    ) -> Tuple[int, int]:
        with self._db.session() as db:
            learner = get_or_create_learner(db, learner_name)
            row = TrainingSession(
                learner_id=learner.id,
                status="running",
                source=source,
                plan_json=plan.as_json(),
                started_at=self._clock(),
            )
            db.add(row)
            db.commit()
            return row.id, learner.id

    def _close_session(self, session_id: int, *, status: str) -> None:
        with self._db.session() as db:
            row = db.get(TrainingSession, session_id)
            if row is not None:
                row.status = status
                row.ended_at = self._clock()
                db.commit()

    def _record(
        self,
        *,
        session_id: int,
        seq: int,
        spec: ItemSpec,
        attempt: int,
        answer: str,
        judgement: judge_module.Judgement,
        presented_at: datetime,
        ready_at: datetime,
        answered_at: datetime,
    ) -> None:
        with self._db.session() as db:
            item = Item(
                session_id=session_id,
                seq=seq,
                track=spec.track,
                letters=spec.letters,
                hold_ms=spec.hold_ms,
                transition_ms=spec.transition_ms,
                boundary=spec.boundary,
                presented_at=presented_at,
                ready_at=ready_at,
                answered_at=answered_at,
                answer=answer,
                result=judgement.result,
                attempt=attempt,
            )
            db.add(item)
            db.flush()
            for confusion in judgement.confusions:
                db.add(
                    Confusion(
                        item_id=item.id,
                        # items.track 을 그대로 복제한다. 여기서 트랙이 어긋나면
                        # 집계가 섞인다.
                        track=spec.track,
                        position=confusion.position,
                        expected=confusion.expected,
                        given=confusion.given,
                    )
                )
            db.commit()

    def _summarize(
        self, session_id: int, learner_id: int, outcomes: Tuple[ItemOutcome, ...]
    ) -> SessionSummary:
        with self._db.session() as db:
            by_track = {
                track: track_summary(db, track, session_id=session_id)
                for track in (TRACK_DISCRIMINATION, TRACK_WORD)
            }
            confusions = {
                track: confusion_counts(
                    db, self._config, track, session_id=session_id
                )
                for track in (TRACK_DISCRIMINATION, TRACK_WORD)
            }
        return SessionSummary(
            session_id=session_id,
            learner_id=learner_id,
            by_track=by_track,
            confusions_by_track=confusions,
            outcomes=outcomes,
        )


def _outcome(
    seq: int,
    spec: ItemSpec,
    attempt: int,
    answer: str,
    judgement: judge_module.Judgement,
    *,
    demo: Optional[DemoShown],
) -> ItemOutcome:
    return ItemOutcome(
        seq=seq,
        track=spec.track,
        letters=spec.letters,
        attempt=attempt,
        answer=answer,
        result=judgement.result,
        confusions=judgement.confusions,
        demo=demo,
    )
