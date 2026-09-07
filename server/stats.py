"""트랙별 통계 (PRD 9.2, 9.5).

**모든 집계 함수는 `track` 을 필수 인자로 받는다.** 기본값도 없고, 트랙을
합치는 함수도 없다. 단어 문항의 정답은 철자 지식으로 보정되므로 판별 정확도와
같은 표에 들어가면 지표가 오염된다 (기획서 3.3).

"전체 정확도" 같은 통합 지표는 만들지 않는다. 요청받지 않았고, 만드는 순간
두 트랙을 섞는 경로가 생긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from server.config import Config, ConfusionPair
from server.db import (
    RESULT_CORRECT,
    RESULT_LENGTH,
    RESULT_ORDER,
    RESULT_WRONG,
    TRACKS,
    Confusion,
    Item,
    TrainingSession,
)

__all__ = [
    "TrackSummary",
    "PairCount",
    "track_summary",
    "confusion_counts",
    "weakest_pairs",
    "depth_unlocked",
    "recent_letters",
    "item_contains_depth_pair",
    "depth_pair_keys",
]


def _require_track(track: str) -> str:
    if track not in TRACKS:
        raise ValueError(
            f"track is {track!r}, expected one of {list(TRACKS)}; "
            f"aggregates are never computed across tracks"
        )
    return track


@dataclass(frozen=True)
class TrackSummary:
    """한 트랙의 성적. 다른 트랙의 숫자가 섞여 들어올 자리가 없다."""

    track: str
    answered: int
    correct: int
    wrong: int
    length_errors: int
    order_errors: int
    avg_response_ms: Optional[float]

    @property
    def accuracy(self) -> Optional[float]:
        if not self.answered:
            return None
        return self.correct / self.answered

    @property
    def order_error_rate(self) -> Optional[float]:
        if not self.answered:
            return None
        return self.order_errors / self.answered


@dataclass(frozen=True)
class PairCount:
    """혼동 쌍 하나와 그 발생 횟수.

    `listed` 는 이 쌍이 `poses.json` 의 `confusion_pairs` 에 있는가다. 목록에 없는
    쌍도 기록은 하되 약점 선정에는 쓰지 않는다 (PRD 9.3).
    """

    pair: Tuple[str, str]
    count: int
    listed: bool


# --------------------------------------------------------------------------- #
# 조회 범위
# --------------------------------------------------------------------------- #

def _answered_items(
    db: DbSession,
    track: str,
    *,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Item]:
    """답변이 기록된 문항을 최신순으로 가져온다.

    `track` 은 필수다. `session_id` 나 `learner_id` 로 범위를 좁힐 수 있다.
    """
    _require_track(track)
    stmt = select(Item).where(Item.track == track, Item.result.is_not(None))
    if session_id is not None:
        stmt = stmt.where(Item.session_id == session_id)
    if learner_id is not None:
        stmt = stmt.join(
            TrainingSession, TrainingSession.id == Item.session_id
        ).where(TrainingSession.learner_id == learner_id)
    stmt = stmt.order_by(Item.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------- #
# 집계
# --------------------------------------------------------------------------- #

def track_summary(
    db: DbSession,
    track: str,
    *,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> TrackSummary:
    """한 트랙의 정확도·순서 오류율·평균 응답 시간."""
    items = _answered_items(
        db, track, session_id=session_id, learner_id=learner_id
    )

    response_times = [
        (item.answered_at - item.ready_at).total_seconds() * 1000
        for item in items
        if item.answered_at is not None and item.ready_at is not None
    ]

    return TrackSummary(
        track=track,
        answered=len(items),
        correct=sum(1 for i in items if i.result == RESULT_CORRECT),
        wrong=sum(1 for i in items if i.result == RESULT_WRONG),
        length_errors=sum(1 for i in items if i.result == RESULT_LENGTH),
        order_errors=sum(1 for i in items if i.result == RESULT_ORDER),
        avg_response_ms=(
            sum(response_times) / len(response_times) if response_times else None
        ),
    )


def confusion_counts(
    db: DbSession,
    config: Config,
    track: str,
    *,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> Tuple[PairCount, ...]:
    """혼동 쌍별 발생 횟수, 많은 순.

    `confusions.track` 으로 직접 거른다. `items` 와 JOIN 하지 않는 것이 PRD 8장의
    의도다 — 조인 조건을 잘못 쓰면 두 트랙이 섞이기 때문이다.
    """
    _require_track(track)
    stmt = select(Confusion).where(Confusion.track == track)
    if session_id is not None or learner_id is not None:
        stmt = stmt.join(Item, Item.id == Confusion.item_id)
        if session_id is not None:
            stmt = stmt.where(Item.session_id == session_id)
        if learner_id is not None:
            stmt = stmt.join(
                TrainingSession, TrainingSession.id == Item.session_id
            ).where(TrainingSession.learner_id == learner_id)

    listed = {frozenset(p.pair) for p in config.confusion_pairs}
    tally: Dict[FrozenSet[str], int] = {}
    for confusion in db.scalars(stmt):
        key = frozenset((confusion.expected, confusion.given))
        tally[key] = tally.get(key, 0) + 1

    counts = [
        PairCount(
            pair=tuple(sorted(key)),  # type: ignore[arg-type]
            count=count,
            listed=key in listed,
        )
        for key, count in tally.items()
    ]
    counts.sort(key=lambda pc: (-pc.count, pc.pair))
    return tuple(counts)


def weakest_pairs(
    db: DbSession,
    config: Config,
    track: str,
    *,
    top: int = 2,
    exclude_depth: bool = True,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> Tuple[ConfusionPair, ...]:
    """가장 많이 틀린 혼동 쌍 (PRD 9.5).

    `confusion_pairs` 에 있는 쌍만 후보다. 목록에 없는 쌍은 기록에는 남아도
    훈련 대상이 아니다 — 사람이 검토하지 않은 쌍이기 때문이다 (PRD 9.3).
    """
    _require_track(track)
    by_key = {frozenset(p.pair): p for p in config.confusion_pairs}

    ranked: List[ConfusionPair] = []
    for count in confusion_counts(
        db, config, track, session_id=session_id, learner_id=learner_id
    ):
        pair = by_key.get(frozenset(count.pair))
        if pair is None or (exclude_depth and pair.depth):
            continue
        ranked.append(pair)

    if len(ranked) < top:
        # 기록이 부족하면 설정 순서대로 채운다. 훈련 첫 세션에도 목표 쌍이 있어야
        # 한다.
        for pair in config.confusion_pairs:
            if exclude_depth and pair.depth:
                continue
            if pair not in ranked:
                ranked.append(pair)
            if len(ranked) >= top:
                break

    return tuple(ranked[:top])


# --------------------------------------------------------------------------- #
# 깊이쌍 게이팅
# --------------------------------------------------------------------------- #

def depth_pair_keys(config: Config) -> Tuple[FrozenSet[str], ...]:
    """굽힘 깊이로만 갈리는 쌍들 (E/I, O/S, E/O)."""
    return tuple(frozenset(p.pair) for p in config.depth_pairs())


def item_contains_depth_pair(config: Config, letters: str) -> bool:
    """이 글자열이 깊이쌍을 품고 있는가.

    쌍의 **두 글자가 모두** 들어 있어야 깊이쌍 문항이다. E 하나만 있는 것은
    깊이 판별을 요구하지 않는다.
    """
    present = set(letters.upper())
    return any(key <= present for key in depth_pair_keys(config))


def depth_unlocked(
    db: DbSession, config: Config, track: str, *, learner_id: int
) -> bool:
    """깊이쌍을 출제해도 되는가 (`curriculum.depth_pairs_unlock`).

    학습자의 최근 `window_items` 문항(깊이쌍 포함 문항은 제외) 정확도가
    `min_accuracy` 이상이면 해금이다. 임계값은 전부 설정에서 읽는다.

    기록이 `window_items` 에 못 미치면 **잠금**이다. 근거가 부족한 상태에서
    가장 어려운 쌍을 내보내지 않는다.

    `track` 이 필수인 이유: 두 트랙의 정확도를 섞으면 단어 트랙의 성적이 판별
    트랙의 해금 결정을 좌우한다. 그것은 트랙 분리 원칙 위반이다.
    """
    _require_track(track)
    unlock = config.curriculum.get("depth_pairs_unlock")
    if not isinstance(unlock, Mapping):
        raise ValueError(
            "$.curriculum.depth_pairs_unlock: missing, expected an object with "
            "window_items and min_accuracy"
        )
    window = unlock["window_items"]
    threshold = unlock["min_accuracy"]

    candidates = [
        item
        for item in _answered_items(db, track, learner_id=learner_id)
        if not item_contains_depth_pair(config, item.letters)
    ]
    if len(candidates) < window:
        return False

    recent = candidates[:window]
    accuracy = sum(1 for i in recent if i.result == RESULT_CORRECT) / len(recent)
    return accuracy >= threshold


# --------------------------------------------------------------------------- #
# 중복 회피
# --------------------------------------------------------------------------- #

def recent_letters(
    db: DbSession,
    track: str,
    *,
    window: int,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> Tuple[str, ...]:
    """최근 출제된 글자열들. 같은 문항을 다시 내지 않기 위한 것이다.

    답변 여부와 무관하게 **출제된** 것을 센다. 재생 중이거나 답을 못 한 문항도
    방금 본 문항이다.
    """
    _require_track(track)
    stmt = select(Item.letters).where(Item.track == track)
    if session_id is not None:
        stmt = stmt.where(Item.session_id == session_id)
    if learner_id is not None:
        stmt = stmt.join(
            TrainingSession, TrainingSession.id == Item.session_id
        ).where(TrainingSession.learner_id == learner_id)
    stmt = stmt.order_by(Item.id.desc()).limit(window)
    return tuple(db.scalars(stmt))
