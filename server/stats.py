"""트랙별 통계 (PRD 9.2, 9.5).

**모든 집계 함수는 `track` 을 필수 인자로 받는다.** 기본값도 없고, 트랙을
합치는 함수도 없다. 단어 문항의 정답은 철자 지식으로 보정되므로 판별 정확도와
같은 표에 들어가면 지표가 오염된다 (기획서 3.3).

"전체 정확도" 같은 통합 지표는 만들지 않는다. 요청받지 않았고, 만드는 순간
두 트랙을 섞는 경로가 생긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from server.judge import normalize_answer

__all__ = [
    "UnknownTrackError",
    "TrackSummary",
    "PairCount",
    "MatrixCell",
    "ConfusionMatrix",
    "HoldBucket",
    "ItemRecord",
    "SessionAccuracy",
    "DepthUnlockProgress",
    "track_summary",
    "accuracy_by_hold_ms",
    "recent_items",
    "confusion_counts",
    "confusion_matrix",
    "weakest_pairs",
    "default_pairs",
    "session_accuracy",
    "depth_unlock_progress",
    "depth_unlocked",
    "recent_letters",
    "item_contains_depth_pair",
    "depth_pair_keys",
]


class UnknownTrackError(ValueError):
    """허용되지 않은 트랙 값. 라우트가 400 과 JSON 으로 돌려준다 (DECISIONS.md §61).

    `ValueError` 를 잇는다 — 집계 함수를 직접 부르는 쪽의 뜻은 그대로다. 예전에는
    맨 `ValueError` 라 라우트 밖으로 나가 **500 + 비 JSON** 이 됐고, 화면의
    `JSON.parse` 가 거기서 터졌다 (§50.3 과 같은 경로).

    `all` · `both` 도 여기로 온다. **거부하는 값이지 지원하는 값이 아니다** —
    두 트랙을 합친 집계는 없다.
    """

    code = "UNKNOWN_TRACK"
    http_status = 400

    def __init__(self, track: object) -> None:
        super().__init__(
            f"track is {track!r}, expected one of {list(TRACKS)}; "
            f"aggregates are never computed across tracks"
        )
        self.track = track
        self.allowed = TRACKS


def _require_track(track: str) -> str:
    if track not in TRACKS:
        raise UnknownTrackError(track)
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
class HoldBucket:
    """한 유지 시간에서의 성적 (PRD 12.1 `accuracy_by_hold_ms`).

    ①축(유지 시간)을 더 내려도 되는지 판단하는 근거다. 버킷을 묶지 않고 실제로
    출제된 `hold_ms` 값 그대로 센다 — 사다리가 설정값에서 나오므로(§7) 구간을
    여기서 새로 정의하면 설정과 어긋난다.
    """

    hold_ms: int
    answered: int
    correct: int

    @property
    def accuracy(self) -> Optional[float]:
        if not self.answered:
            return None
        return self.correct / self.answered


@dataclass(frozen=True)
class ItemRecord:
    """출제·채점된 문항 하나 (PRD 12.1 "최근 20문항").

    `letters` 가 들어 있다. 이것은 **진단·에이전트용** 표현이고 학습자 화면으로
    가는 `ItemPrompt` 가 아니다 — 정답 분리는 학습자에게 적용되는 규칙이다
    (DECISIONS.md §22). 혼동 행렬이 이미 글자를 내보내고 있는 것과 같은 범위다.
    """

    seq: int
    track: str
    letters: str
    hold_ms: int
    boundary: str
    attempt: int
    answer: Optional[str]
    result: Optional[str]
    response_ms: Optional[float]


@dataclass(frozen=True)
class PairCount:
    """혼동 쌍 하나와 그 발생 횟수.

    `listed` 는 이 쌍이 `poses.json` 의 `confusion_pairs` 에 있는가다. 목록에 없는
    쌍도 기록은 하되 약점 선정에는 쓰지 않는다 (PRD 9.3).
    """

    pair: Tuple[str, str]
    count: int
    listed: bool


@dataclass(frozen=True)
class MatrixCell:
    """혼동 행렬의 한 칸. **방향이 있다** — `expected` 는 로봇손이 보여 준 글자,
    `given` 은 학습자가 고른 답이다. 둘이 같으면 대각선(맞게 읽음)이다.

    `listed` 는 `PairCount.listed` 와 같은 뜻이다 — 이 두 글자가 `poses.json` 의
    `confusion_pairs` 에 있는가. 대각선 칸은 쌍이 아니므로 늘 `False` 다.
    """

    expected: str
    given: str
    count: int
    listed: bool

    @property
    def diagonal(self) -> bool:
        return self.expected == self.given


@dataclass(frozen=True)
class ConfusionMatrix:
    """한 트랙의 `expected × given` 행렬 (DECISIONS.md §60).

    `PairCount` 는 방향을 합치고 정답을 세지 않는다 — 약점 쌍을 고르는 데는
    그것으로 충분하지만, "V 를 W 로 읽는가, W 를 V 로 읽는가" 와 "V 를 몇 번 맞게
    읽었나" 를 보여 줄 수 없다. 이것은 그 둘을 담는다.

    **위치 대응이 확실한 답만 칸에 들어간다** — `correct` 와 `wrong`. `length` 는
    위치가 밀렸고 `order` 는 글자는 다 읽었지만 위치가 뒤섞였다. 둘 다 혼동을
    기록하지 않는 결과이므로(`judge`) 대각선에도 넣지 않는다. 대신 몇 문항이
    빠졌는지 `skipped` 로 낸다 — 숨기지 않는다.

    `letters` 는 세로·가로에 같은 순서로 놓는 축이다. `poses.json` 의 글자에,
    기록에만 있는 글자(타자로 친 답)가 있으면 뒤에 붙인다. `cells` 는 0 이 아닌
    칸만 담는다.
    """

    track: str
    letters: Tuple[str, ...]
    cells: Tuple[MatrixCell, ...]
    items: int
    skipped: Mapping[str, int]


@dataclass(frozen=True)
class SessionAccuracy:
    """세션 하나에서의 한 트랙 성적. 추이를 그리기 위한 것이다.

    **세션 단위이지 시계열 테이블이 아니다.** `items` 를 세션으로 묶어 세는
    것뿐이고 따로 쌓아 두는 것이 없다 (DECISIONS.md §40 의 "지금은 시계열이
    아니다" 와 같은 범위다).

    한 세션이 두 트랙을 모두 담지만 이 값은 **한 트랙만** 센다. 세션이 같아도
    판별과 단어는 다른 줄이다.

    `ordinal` 은 **이 트랙에서** 답한 세션 중 몇 번째인가다 (1부터, DECISIONS.md
    §60). `session_id` 는 공부 세션·참여하지 않은 세션도 번호를 먹어 첫 훈련이
    "세션 2" 가 된다. `limit` 으로 자르기 **전에** 센다 — 자른 뒤에 세면 21번째
    세션이 "1번째" 가 된다.
    """

    session_id: int
    ordinal: int
    started_at: Optional[datetime]
    answered: int
    correct: int

    @property
    def accuracy(self) -> Optional[float]:
        if not self.answered:
            return None
        return self.correct / self.answered


@dataclass(frozen=True)
class DepthUnlockProgress:
    """깊이쌍 해금까지 얼마나 왔는가 (`curriculum.depth_pairs_unlock`).

    `depth_unlocked()` 가 내는 예/아니오와 **같은 계산에서 나온다.** 화면에
    진척도를 보이려고 판정을 한 벌 더 만들면, 조건을 고칠 때 둘이 어긋나
    "다 됐다고 나오는데 안 열린다" 가 된다.

    `counted` 는 창에 들어간 문항 수다. 기록이 창보다 적으면 그만큼만 들어간다
    — 그때 `accuracy` 는 "지금까지의 정확도"이고 해금 판정은 아직 아니다.
    """

    track: str
    window_items: int
    min_accuracy: float
    answered: int
    counted: int
    correct: int
    unlocked: bool

    @property
    def accuracy(self) -> Optional[float]:
        if not self.counted:
            return None
        return self.correct / self.counted

    @property
    def items_remaining(self) -> int:
        """해금 판정을 받기까지 더 풀어야 하는 문항 수."""
        return max(0, self.window_items - self.counted)


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


def accuracy_by_hold_ms(
    db: DbSession,
    track: str,
    *,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> Tuple[HoldBucket, ...]:
    """유지 시간별 정확도, 유지 시간이 긴 것부터 (PRD 12.1).

    `track` 은 필수다. 두 트랙을 합친 버킷은 만들지 않는다 — 단어 트랙의 정답은
    철자 지식으로 보정되므로 같은 버킷에 넣으면 ①축 판단이 오염된다.
    """
    items = _answered_items(
        db, track, session_id=session_id, learner_id=learner_id
    )

    tally: Dict[int, List[Item]] = {}
    for item in items:
        tally.setdefault(item.hold_ms, []).append(item)

    return tuple(
        HoldBucket(
            hold_ms=hold_ms,
            answered=len(bucket),
            correct=sum(1 for i in bucket if i.result == RESULT_CORRECT),
        )
        for hold_ms, bucket in sorted(tally.items(), reverse=True)
    )


def recent_items(
    db: DbSession,
    track: str,
    *,
    limit: int,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> Tuple[ItemRecord, ...]:
    """최근 채점된 문항들, 최신순 (PRD 12.1).

    `track` 은 필수다. 두 트랙을 한 목록에 섞지 않는다.
    """
    items = _answered_items(
        db, track, session_id=session_id, learner_id=learner_id, limit=limit
    )
    return tuple(
        ItemRecord(
            seq=item.seq,
            track=item.track,
            letters=item.letters,
            hold_ms=item.hold_ms,
            boundary=item.boundary,
            attempt=item.attempt,
            answer=item.answer,
            result=item.result,
            response_ms=(
                (item.answered_at - item.ready_at).total_seconds() * 1000
                if item.answered_at is not None and item.ready_at is not None
                else None
            ),
        )
        for item in items
    )


def _confusion_rows(
    db: DbSession,
    track: str,
    *,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> List[Confusion]:
    """한 트랙의 혼동 기록. `confusion_counts` 와 `confusion_matrix` 가 **같은
    범위 조건**을 쓰도록 한 곳에 둔다."""
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
    return list(db.scalars(stmt))


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
    listed = {frozenset(p.pair) for p in config.confusion_pairs}
    tally: Dict[FrozenSet[str], int] = {}
    for confusion in _confusion_rows(
        db, track, session_id=session_id, learner_id=learner_id
    ):
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


def confusion_matrix(
    db: DbSession,
    config: Config,
    track: str,
    *,
    session_id: Optional[int] = None,
    learner_id: Optional[int] = None,
) -> ConfusionMatrix:
    """한 트랙의 `expected × given` 행렬 (DECISIONS.md §60).

    대각선 밖 칸은 `confusions` 기록 그대로다 — `confusion_counts` 가 세는 것과
    같은 행을 방향을 살려 센다. 대각선은 `items` 에서 센다: `correct` 문항은 모든
    위치, `wrong` 문항은 혼동이 기록되지 않은 위치다. 두 조회 모두 **자기 테이블의
    `track`** 으로 거른다. 두 트랙을 합친 행렬은 만들 수 없다.
    """
    _require_track(track)
    by_item: Dict[int, Dict[int, Confusion]] = {}
    for confusion in _confusion_rows(
        db, track, session_id=session_id, learner_id=learner_id
    ):
        by_item.setdefault(confusion.item_id, {})[confusion.position] = confusion

    tally: Dict[Tuple[str, str], int] = {}
    skipped = {RESULT_LENGTH: 0, RESULT_ORDER: 0}
    counted = 0
    for item in _answered_items(
        db, track, session_id=session_id, learner_id=learner_id
    ):
        if item.result in skipped:
            skipped[item.result] += 1
            continue
        if item.result not in (RESULT_CORRECT, RESULT_WRONG):
            continue
        counted += 1
        confused = by_item.get(item.id, {})
        for position, letter in enumerate(normalize_answer(item.letters)):
            confusion = confused.get(position)
            key = (
                (confusion.expected, confusion.given)
                if confusion is not None
                else (letter, letter)
            )
            tally[key] = tally.get(key, 0) + 1

    listed = {frozenset(p.pair) for p in config.confusion_pairs}
    seen = {letter for key in tally for letter in key}
    axis = tuple(sorted(config.poses)) + tuple(sorted(seen - set(config.poses)))
    order = {letter: index for index, letter in enumerate(axis)}
    cells = sorted(
        (
            MatrixCell(
                expected=expected,
                given=given,
                count=count,
                listed=expected != given
                and frozenset((expected, given)) in listed,
            )
            for (expected, given), count in tally.items()
        ),
        key=lambda c: (order[c.expected], order[c.given]),
    )
    return ConfusionMatrix(
        track=track,
        letters=axis,
        cells=tuple(cells),
        items=counted,
        skipped=skipped,
    )


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

    # 기록이 부족하면 설정 순서대로 채운다. 훈련 첫 세션에도 목표 쌍이 있어야
    # 한다.
    return default_pairs(
        config, top=top, exclude_depth=exclude_depth, ranked=ranked
    )


def default_pairs(
    config: Config,
    *,
    top: int,
    exclude_depth: bool = True,
    ranked: Sequence[ConfusionPair] = (),
) -> Tuple[ConfusionPair, ...]:
    """`ranked` 를 앞에 두고 모자라는 만큼 **설정 순서**로 채운다.

    `weakest_pairs()` 의 폴백이 이것이다. 따로 꺼낸 이유는 **아직 없는 학습자**
    다 — DB 에 행이 없으니 `learner_id` 가 없고, `weakest_pairs()` 에
    `learner_id=None` 을 넘기면 모든 학습자의 혼동을 센다. 그런 호출자는
    기록을 읽지 않고 이것만 부른다 (DECISIONS.md §54). 기록이 없는 학습자에게
    `weakest_pairs()` 가 낼 값과 **같은 코드**에서 나온다.

    DB 를 보지 않는다. 트랙도 받지 않는다 — 설정의 혼동쌍 목록은 트랙과 무관하다.
    """
    chosen: List[ConfusionPair] = list(ranked)
    if len(chosen) < top:
        for pair in config.confusion_pairs:
            if exclude_depth and pair.depth:
                continue
            if pair not in chosen:
                chosen.append(pair)
            if len(chosen) >= top:
                break
    return tuple(chosen[:top])


def session_accuracy(
    db: DbSession,
    track: str,
    *,
    learner_id: int,
    limit: Optional[int] = None,
) -> Tuple[SessionAccuracy, ...]:
    """세션별 성적, **오래된 것부터**. 추이를 읽기 위한 것이다.

    다른 집계는 최신순이지만 이것만 반대다 — 왼쪽에서 오른쪽으로 읽는 추이에서
    최신순은 거꾸로 간다. `limit` 은 **최근** 몇 세션인지이고, 자른 뒤에 다시
    오래된 순으로 세운다.

    `track` 은 필수다. 한 세션이 두 트랙을 모두 담지만 줄은 트랙마다 따로다.
    """
    _require_track(track)

    tally: Dict[int, List[Item]] = {}
    for item in _answered_items(db, track, learner_id=learner_id):
        tally.setdefault(item.session_id, []).append(item)

    ordered = sorted(tally)
    # 차례는 자르기 전에 정한다. 이 트랙에서 답한 세션만 번호를 먹는다.
    ordinal = {session_id: n for n, session_id in enumerate(ordered, start=1)}
    if limit is not None:
        ordered = ordered[-limit:]

    started = {
        row.id: row.started_at
        for row in db.scalars(
            select(TrainingSession).where(TrainingSession.id.in_(ordered))
        )
    } if ordered else {}

    return tuple(
        SessionAccuracy(
            session_id=session_id,
            ordinal=ordinal[session_id],
            started_at=started.get(session_id),
            answered=len(tally[session_id]),
            correct=sum(
                1 for i in tally[session_id] if i.result == RESULT_CORRECT
            ),
        )
        for session_id in ordered
    )


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


def depth_unlock_progress(
    db: DbSession, config: Config, track: str, *, learner_id: Optional[int]
) -> DepthUnlockProgress:
    """해금 판정과 **거기까지의 진척도**를 한 번에 낸다.

    학습자의 최근 `window_items` 문항(깊이쌍 포함 문항은 제외) 정확도가
    `min_accuracy` 이상이면 해금이다. 임계값은 전부 설정에서 읽는다.

    기록이 `window_items` 에 못 미치면 **잠금**이다. 근거가 부족한 상태에서
    가장 어려운 쌍을 내보내지 않는다. 그래도 지금까지의 정확도는 함께 낸다 —
    학습자 화면이 "몇 문항 남았고 지금 몇 퍼센트인가" 를 보여야 한다.

    `track` 이 필수인 이유: 두 트랙의 정확도를 섞으면 단어 트랙의 성적이 판별
    트랙의 해금 결정을 좌우한다. 그것은 트랙 분리 원칙 위반이다.

    `learner_id` 가 `None` 이면 아직 기록이 없는 학습자다. **아무것도 세지
    않는다** — `_answered_items()` 에 `None` 을 넘기면 범위를 좁히지 않아
    모든 학습자의 문항을 센다. 남의 기록으로 해금되는 일을 여기서 막는다.
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
        for item in (
            _answered_items(db, track, learner_id=learner_id)
            if learner_id is not None
            else []
        )
        if not item_contains_depth_pair(config, item.letters)
    ]
    recent = candidates[:window]
    correct = sum(1 for i in recent if i.result == RESULT_CORRECT)
    # 창이 다 차야 판정한다. 부분 정확도로 열지 않는다.
    unlocked = len(candidates) >= window and bool(
        recent and correct / len(recent) >= threshold
    )

    return DepthUnlockProgress(
        track=track,
        window_items=window,
        min_accuracy=threshold,
        answered=len(candidates),
        counted=len(recent),
        correct=correct,
        unlocked=unlocked,
    )


def depth_unlocked(
    db: DbSession, config: Config, track: str, *, learner_id: int
) -> bool:
    """깊이쌍을 출제해도 되는가 (`curriculum.depth_pairs_unlock`).

    **판정은 `depth_unlock_progress()` 한 곳에서만 한다.** 화면이 진척도를
    보이려고 같은 조건을 한 벌 더 쓰면, 조건을 고칠 때 둘이 어긋난다.
    """
    return depth_unlock_progress(
        db, config, track, learner_id=learner_id
    ).unlocked


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
