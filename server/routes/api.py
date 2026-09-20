"""REST 라우트 (PRD 11장).

라우트는 **얇다.** 판정도 적응도 안전 검사도 하지 않고 `SessionController` 와
`SafetyGate` 를 부를 뿐이다. 거부는 `safety.py` / `session.py` 가 만든 구조화된
예외를 그대로 HTTP 로 옮긴다 — 여기서 에러 문자열을 새로 짓지 않는다.

인증이 없다. 단일 학습자 로컬 실행 전제다 (DECISIONS.md §27).
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Body, Query, Request
from sqlalchemy import select

from server.db import (
    BOUNDARIES,
    BOUNDARY_EXPLICIT,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    TRACKS,
    Learner,
)
from server.items import (
    RECENT_ITEM_WINDOW,
    discrimination_violations,
    word_violations,
)
from server.routes.events import MCP_LOG_CHANNEL
from server.safety import LockNotHeldError, UnsupportedLettersError
from server.session import (
    DIFFICULTY_LADDER,
    AnswerOutcome,
    ItemPrompt,
    SessionController,
    SessionPlan,
    SessionStateError,
)
from server.stats import (
    accuracy_by_hold_ms,
    confusion_counts,
    depth_unlocked,
    recent_items,
    recent_letters,
    track_summary,
)

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# MCP 로그 패널이 받아들이는 이벤트 종류 (PRD 11장 `/ws/mcp-log`).
MCP_LOG_TYPES = ("tool_call", "tool_result", "plan_ready", "blocked")

# 후보 문항을 몇 번 뽑아 보고 멈출 것인가. items.py 의 `_MAX_ATTEMPTS` 와 같은
# 역할이지만 저쪽은 문항 하나 기준이고 이쪽은 목록 하나 기준이라 재사용하지
# 않는다 (DECISIONS.md §19).
_BANK_MAX_ATTEMPTS = 2000


def _station(request: Request):
    return request.app.state.station


def _controller(request: Request, session_id: int) -> SessionController:
    station = _station(request)
    controller = station.controllers.get(session_id)
    if controller is None:
        raise SessionStateError(
            f"session {session_id} is not running on this server"
        )
    return controller


def _prompt_payload(prompt: ItemPrompt, controller: SessionController) -> Dict[str, Any]:
    """문항을 클라이언트 표현으로 바꾼다.

    `dataclasses.asdict(prompt)` 를 그대로 쓴다. `ItemPrompt` 에는 정답이 없고
    (DECISIONS.md §22), 여기서 정답을 덧붙이지 않는다.
    """
    payload = dataclasses.asdict(prompt)
    payload["index"] = prompt.seq
    payload["total"] = controller.total_items
    return payload


def _outcome_payload(outcome: AnswerOutcome) -> Dict[str, Any]:
    """채점 결과를 클라이언트 표현으로 바꾼다.

    재생이 남아 있으면 `expected` 도 `confusions` 도 비어 있다. 오답 1회째에
    정답을 알려주면 재생이 의미를 잃는다 (기획서 5.2).
    """
    return {
        "seq": outcome.seq,
        "result": outcome.result,
        "retry": outcome.retry,
        "finished": outcome.finished,
        "expected": outcome.expected,
        "confusions": [
            {"position": c.position, "expected": c.expected, "given": c.given}
            for c in outcome.confusions
        ],
        "demo": (
            None
            if outcome.demo is None
            else {
                "letters": [outcome.demo.expected, outcome.demo.given],
                "hint": outcome.demo.hint,
            }
        ),
    }


def _summary_payload(summary) -> Dict[str, Any]:
    """세션 요약. **트랙별로만** 나온다. 통합 지표는 만들지 않는다."""
    return {
        "session_id": summary.session_id,
        "learner_id": summary.learner_id,
        "by_track": {
            track: {
                "track": s.track,
                "answered": s.answered,
                "correct": s.correct,
                "wrong": s.wrong,
                "length_errors": s.length_errors,
                "order_errors": s.order_errors,
                "accuracy": s.accuracy,
                "order_error_rate": s.order_error_rate,
                "avg_response_ms": s.avg_response_ms,
            }
            for track, s in summary.by_track.items()
        },
        "confusions_by_track": {
            track: [
                {"pair": list(c.pair), "count": c.count, "listed": c.listed}
                for c in counts
            ]
            for track, counts in summary.confusions_by_track.items()
        },
    }


# --------------------------------------------------------------------------- #
# 학습자
# --------------------------------------------------------------------------- #

@router.get("/learners")
def list_learners(request: Request) -> Dict[str, Any]:
    station = _station(request)
    with station.database.session() as db:
        names = list(db.scalars(select(Learner.name).order_by(Learner.name)))
    return {"learners": names}


# --------------------------------------------------------------------------- #
# 세션
# --------------------------------------------------------------------------- #

@router.post("/sessions", status_code=201)
def start_session(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """세션을 만들고 디바이스 락을 잡는다.

    다른 세션이 손을 쥐고 있으면 `LOCK_UNAVAILABLE` 로 거부된다.
    """
    station = _station(request)
    learner_name = str(body.get("learner_name") or "").strip()
    if not learner_name:
        raise SessionStateError("learner_name is required")

    plan = SessionPlan(
        discrimination_items=int(
            body.get("discrimination_items", SessionPlan().discrimination_items)
        ),
        word_items=int(body.get("word_items", SessionPlan().word_items)),
    )
    controller = station.new_controller()
    session_id = controller.start(
        learner_name, plan=plan, source=str(body.get("source", "rule"))
    )
    station.controllers[session_id] = controller
    return {
        "session_id": session_id,
        "learner_id": controller.learner_id,
        "total_items": controller.total_items,
        "state": controller.state,
    }


@router.get("/sessions/{session_id}/next")
def next_item(request: Request, session_id: int) -> Dict[str, Any]:
    controller = _controller(request, session_id)
    prompt = controller.next_item()
    return _prompt_payload(prompt, controller)


@router.post("/sessions/{session_id}/present", status_code=202)
def present(request: Request, session_id: int) -> Dict[str, Any]:
    """현재 문항을 손으로 표현한다.

    디바이스 동작은 블로킹이므로 백그라운드에서 돌린다. 진행 상황은 WS 로
    나간다 — 이 응답은 "시작했다"까지다. 같은 문항에 다시 불러도 된다
    (화면의 "다시 보기").
    """
    station = _station(request)
    controller = _controller(request, session_id)
    controller.current_prompt()  # 표현할 문항이 있는지 먼저 확인한다.
    station.run_in_background(session_id, controller.present)
    return {"status": "presenting", "index": controller.index}


@router.post("/sessions/{session_id}/answer")
def submit_answer(
    request: Request, session_id: int, body: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    controller = _controller(request, session_id)
    outcome = controller.submit(str(body.get("answer", "")))
    return _outcome_payload(outcome)


@router.post("/sessions/{session_id}/demo", status_code=202)
def replay_demo(request: Request, session_id: int) -> Dict[str, Any]:
    """직전에 보여 준 시범을 다시 보여 준다.

    시범이 나온 적 없는 문항이면 거부한다 — 시범은 정답 공개이기 때문이다.
    """
    station = _station(request)
    controller = _controller(request, session_id)
    if not controller.has_demo:
        raise SessionStateError(
            "no demo has been shown for the current item; a demo is only "
            "available after a second wrong answer"
        )
    station.run_in_background(session_id, controller.replay_demo)
    return {"status": "demonstrating"}


@router.post("/sessions/{session_id}/sequence")
def present_sequence(
    request: Request, session_id: int, body: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """**호출자가 고른** 글자열을 표현한다 (에이전트·MCP 경로, PRD 12.1).

    `/present` 와 두 가지가 다르다.

    1. 글자열을 호출자가 준다. 안전 게이트가 그것을 검사하므로 거부 사유
       (`UNSUPPORTED_LETTERS`, `CURRICULUM_VIOLATION`)가 **이 응답으로** 돌아온다.
    2. **동기다.** 손이 끝날 때까지 돌려주지 않는다. 브라우저는 202 를 받고 WS 를
       보면 되지만(§27), 도구 호출자에게는 WS 가 없다 (DECISIONS.md §46).

    `transition_ms` 는 받지 않는다 — 난이도 축이 아니고 호출자가 정할 값도 아니다.
    """
    station = _station(request)
    _require_lock(station, session_id)
    controller = _controller(request, session_id)

    letters = str(body.get("letters") or "").strip()
    track = str(body.get("track") or TRACK_DISCRIMINATION)
    boundary = str(body.get("boundary") or BOUNDARY_EXPLICIT)
    hold_ms = _clamp_hold_ms(station, body.get("hold_ms"))

    prompt = controller.present_letters(
        letters, track=track, hold_ms=hold_ms, boundary=boundary
    )
    payload = _prompt_payload(prompt, controller)
    payload["status"] = "presented"
    return payload


@router.post("/sessions/{session_id}/demo-letter")
def demo_letter(
    request: Request, session_id: int, body: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """글자 **하나**를 시범으로 보여 준다 (PRD 12.1 `demo_letter`).

    채점하지 않고 기록하지 않으며 활성 문항도 건드리지 않는다. 트랙은
    `discrimination` 으로 고정한다 — 게이트의 트랙 유효성 검사에만 쓰이고
    기록이 남지 않으므로 트랙 통계에 섞일 자리가 없다.

    `/sessions/{id}/demo` 와 다르다. 저쪽은 **직전에 나온** 시범의 재생이고
    (오답 2회 뒤에만 허용, DECISIONS.md §26), 이쪽은 호출자가 고른 글자다.
    """
    station = _station(request)
    _require_lock(station, session_id)
    controller = _controller(request, session_id)

    letter = str(body.get("letter") or "").strip()
    hold_ms = _clamp_hold_ms(station, body.get("hold_ms"))

    prompt = controller.present_letters(
        letter,
        track=TRACK_DISCRIMINATION,
        hold_ms=hold_ms,
        boundary=BOUNDARY_EXPLICIT,
        scored=False,
    )
    return {
        "status": "demonstrated",
        "letter": letter.upper(),
        "hold_ms": prompt.hold_ms,
        "transition_ms": prompt.transition_ms,
    }


def _require_lock(station, session_id: int) -> None:
    """구동 요청의 원인을 락 쪽으로 먼저 돌린다 (게이트 ①, PRD 15.2).

    이 검사 없이는 세션이 아예 없는 호출이 `SESSION_STATE` 로 돌아온다 —
    컨트롤러 조회가 먼저이기 때문이다. 그런데 PRD 15.2 의 라이브 차단 사례는
    "`start_session` 없이 `present_sequence`" 를 **`LOCK_NOT_HELD`** 로
    규정하고 있고, 그게 원인을 더 정확히 말한다: 세션이 없으면 락도 없다.

    **게이트를 대신하지 않는다.** 아래에서 `SafetyGate` 가 ① 을 다시 본다.
    앞서 거절하는 검사는 통과 범위를 넓히지 않는다.
    """
    holder = station.lock.holder()
    if holder != session_id:
        raise LockNotHeldError(
            f"session {session_id} does not hold the device lock "
            f"(holder={holder}); no motion is allowed without it"
        )


def _clamp_hold_ms(station, value: Any) -> int:
    """유지 시간을 설정 범위로 자른다 (PRD 13.3).

    호출자가 LLM 이므로 범위를 벗어난 값이 온다. 스키마에 범위를 적을 수 없는
    것은 상·하한이 설정값이라서다 — 그래서 여기서 자르고, 자른 사실을 경고로
    남기고(§12), 실제로 쓰인 값을 응답에 담아 돌려준다.
    """
    timing = station.config.timing
    if value is None:
        return timing.hold_default_ms
    requested = int(value)
    bounded = min(max(requested, timing.hold_min_ms), timing.hold_max_ms)
    if bounded != requested:
        logger.warning(
            "blocked: clamped hold_ms %s -> %s (range %s..%s)",
            requested,
            bounded,
            timing.hold_min_ms,
            timing.hold_max_ms,
        )
    return bounded


@router.post("/sessions/{session_id}/heartbeat")
def heartbeat(request: Request, session_id: int) -> Dict[str, Any]:
    controller = _controller(request, session_id)
    controller.heartbeat()
    return {"status": "alive", "state": controller.state}


@router.post("/sessions/{session_id}/end")
def end_session(request: Request, session_id: int) -> Dict[str, Any]:
    station = _station(request)
    controller = _controller(request, session_id)
    summary = controller.end()
    station.controllers.pop(session_id, None)
    return _summary_payload(summary)


# --------------------------------------------------------------------------- #
# 통계
# --------------------------------------------------------------------------- #

@router.get("/stats/confusion")
def confusion_matrix(
    request: Request,
    track: str = Query(..., description="discrimination | word. 필수다."),
    learner_id: Optional[int] = Query(None),
) -> Dict[str, Any]:
    """혼동 행렬. `track` 은 **필수**다 (PRD 11장).

    기본값을 두지 않는 것은 실수로 두 트랙을 합쳐 보는 일을 막기 위해서다.
    """
    station = _station(request)
    with station.database.session() as db:
        counts = confusion_counts(
            db, station.config, track, learner_id=learner_id
        )
    return {
        "track": track,
        "pairs": [
            {"pair": list(c.pair), "count": c.count, "listed": c.listed}
            for c in counts
        ],
    }


@router.get("/stats/learners/{learner_id}")
def learner_stats(request: Request, learner_id: int) -> Dict[str, Any]:
    """학습자 상세. 트랙별로 분리해서 낸다."""
    station = _station(request)
    with station.database.session() as db:
        by_track = {}
        for track in TRACKS:
            summary = track_summary(db, track, learner_id=learner_id)
            by_track[track] = {
                "answered": summary.answered,
                "correct": summary.correct,
                "wrong": summary.wrong,
                "length_errors": summary.length_errors,
                "order_errors": summary.order_errors,
                "accuracy": summary.accuracy,
                "order_error_rate": summary.order_error_rate,
                "avg_response_ms": summary.avg_response_ms,
            }
    return {"learner_id": learner_id, "by_track": by_track}


# --------------------------------------------------------------------------- #
# 디바이스
# --------------------------------------------------------------------------- #

@router.get("/device/status")
def device_status(request: Request) -> Dict[str, Any]:
    station = _station(request)
    return {
        "mode": station.device_mode,
        "busy": station.device.is_busy,
        "lock_holder": station.lock.holder(),
        # 캘리브레이션 해시 대조는 tools/gen_calibration.py 이후다 (게이트 ⑧).
        "calibration_hash_ok": None,
    }


@router.post("/device/stop")
def device_stop(request: Request) -> Dict[str, Any]:
    """비상 정지. 손을 즉시 안전 자세로 만든다.

    **세션은 그대로 살아 있다.** 진행 중이던 문항만 취소되고 락도 유지된다 —
    학습자는 같은 문항을 다시 보고 이어 가면 된다. 세션까지 끝내는 것은
    `/api/device/reset` 이다 (DECISIONS.md §45).
    """
    station = _station(request)
    station.device.stop()
    return {"status": "stopped"}


@router.post("/device/reset")
def device_reset(request: Request) -> Dict[str, Any]:
    """물린 락을 서버 재시작 없이 푼다 (PRD 11장).

    **정지(`/api/device/stop`)와 결과가 다르다.** 정지는 손만 멈추고 세션을
    살려 두지만, 이쪽은 세션을 끝낸다.

    활성 세션을 `aborted` 로 닫고, 락을 놓고, 손을 안전 자세로 되돌린다.
    답한 문항까지의 기록과 난이도는 남는다 — 중단 세션과 같은 취급이다
    (DECISIONS.md §39).

    **§42 수정의 대체가 아니라 보조다.** 원인을 고치는 것이 먼저고, 이것은
    시연 중에 서버를 껐다 켤 수 없어서 두는 마지막 수단이다. 진행 중인
    세션이 날아가므로 화면에서 확인 절차를 거친다.
    """
    station = _station(request)
    ended = station.reset_device()
    return {
        # 무엇을 끝냈는지가 화면 문구의 근거다. 비어 있으면 끝낸 것이 없다 —
        # "잠긴 세션이 없습니다" 와 "세션 N 을 종료했습니다" 를 여기서 가른다.
        "status": "reset",
        "ended_sessions": list(ended),
        # 해제 **뒤의** 보유자다. 정상이면 항상 null 이다.
        "lock_holder": station.lock.holder(),
    }


# --------------------------------------------------------------------------- #
# 능력 선언 · 문항 후보 · 학습자 상세 (PRD 12.1 의 1층 도구가 쓴다)
# --------------------------------------------------------------------------- #

def _learner_id_by_name(station, name: str) -> Optional[int]:
    """이름으로 학습자를 찾는다. **없으면 만들지 않는다.**

    조회가 학습자를 만들면 오타 한 번이 학습자 목록에 남는다.
    """
    with station.database.session() as db:
        return db.scalar(select(Learner.id).where(Learner.name == name))


def _requested_letters(station, letters: Optional[Sequence[str]]) -> List[str]:
    """요청된 글자 집합. 비어 있으면 이 손이 만들 수 있는 글자 전부.

    지원하지 않는 글자가 있으면 안전 게이트와 **같은 예외**로 거부한다 — 거부
    코드와 대안 목록을 여기서 새로 짓지 않는다.
    """
    supported = sorted(station.config.poses)
    if not letters:
        return supported

    # 반복 질의(letters=B, letters=D)와 쉼표 구분(letters=B,D)을 모두 받는다.
    requested: List[str] = []
    for chunk in letters:
        for letter in str(chunk).replace(",", " ").split():
            for character in letter.upper():
                if character not in requested:
                    requested.append(character)

    unknown = sorted(set(requested) - set(supported))
    if unknown:
        raise UnsupportedLettersError(unknown, supported)
    return sorted(requested)


@router.get("/capabilities")
def capabilities(
    request: Request,
    learner_name: Optional[str] = Query(
        None,
        description=(
            "깊이쌍 해금 여부는 학습자별이다. 이름을 주면 그 학습자의 실제 값을, "
            "주지 않으면 이력이 없는 학습자의 값(항상 잠금)을 낸다."
        ),
    ),
) -> Dict[str, Any]:
    """런타임 능력 선언 (PRD 12.1 `get_capabilities`).

    **글자 목록을 프롬프트에 하드코딩하지 않기 위한 것이다.** 전부 `poses.json`
    과 `words.json` 에서 읽는다. 여기에 값을 적어 두면 §1 의 단일 출처가 깨진다.
    """
    station = _station(request)
    config = station.config
    timing = config.timing

    unlocked = False
    if learner_name:
        learner_id = _learner_id_by_name(station, learner_name)
        if learner_id is not None:
            with station.database.session() as db:
                unlocked = depth_unlocked(
                    db, config, TRACK_DISCRIMINATION, learner_id=learner_id
                )

    return {
        "device_id": config.device_id,
        "dof": dict(config.dof),
        "letters": sorted(config.poses),
        "tracks": list(TRACKS),
        "confusion_pairs": [
            {
                "pair": list(pair.pair),
                "axis": pair.axis,
                "depth": pair.depth,
                "hint": pair.hint,
            }
            for pair in config.confusion_pairs
        ],
        # §20: 깊이쌍 게이팅은 판별 트랙에만 걸린다.
        "depth_pairs_unlocked": unlocked,
        "difficulty_axes": {
            "hold_ms": {
                "min": timing.hold_min_ms,
                "max": timing.hold_max_ms,
                "default": timing.hold_default_ms,
            },
            # 사다리가 길이의 단일 출처다 (session.DIFFICULTY_LADDER).
            "length": {
                "min": min(length for length, _ in DIFFICULTY_LADDER),
                "max": max(length for length, _ in DIFFICULTY_LADDER),
            },
            "boundary": list(BOUNDARIES),
        },
        "transition_ms": timing.transition_ms,
        "word_count": len(station.words.words),
    }


@router.get("/item-bank")
def item_bank(
    request: Request,
    track: str = Query(..., description="discrimination | word. 필수다."),
    length: int = Query(...),
    count: int = Query(...),
    letters: Optional[List[str]] = Query(None),
    exclude_recent: bool = Query(True),
    learner_name: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """제약을 만족하는 문항 후보 (PRD 12.1 `get_item_bank`).

    제약 검사는 `items.py` 의 `*_violations()` 를 그대로 쓴다. 생성기를 쓰지 않는
    것은 요청된 글자 집합으로 알파벳을 좁혀야 하는데 생성기가 그 인자를 받지
    않기 때문이다. `violations()` 는 "누가 만든 문항이든 검사한다"는 용도로
    분리되어 있으므로(items.py 머리말) 이쪽이 의도된 경로다.

    `learner_name` 이 없으면 깊이쌍은 **잠금**으로 보고, 최근 출제는 학습자를
    가리지 않고 본다. 근거가 없는 상태에서 가장 어려운 쌍을 후보에 넣지 않는다
    (DECISIONS.md §46).
    """
    station = _station(request)
    if track not in TRACKS:
        raise SessionStateError(
            f"track is {track!r}, expected one of {list(TRACKS)}"
        )
    if count < 0:
        raise SessionStateError(f"count is {count}, expected zero or more")
    if length < 1:
        raise SessionStateError(f"length is {length}, expected at least 1")

    alphabet = _requested_letters(station, letters)
    learner_id = (
        _learner_id_by_name(station, learner_name) if learner_name else None
    )

    with station.database.session() as db:
        recent = (
            recent_letters(
                db, track, window=RECENT_ITEM_WINDOW, learner_id=learner_id
            )
            if exclude_recent
            else ()
        )
        unlocked = (
            depth_unlocked(
                db, station.config, TRACK_DISCRIMINATION, learner_id=learner_id
            )
            if learner_id is not None
            else False
        )

    if track == TRACK_WORD:
        allowed = set(alphabet)
        candidates = [
            word.letters
            for word in station.words.of_length(length)
            if set(word.letters) <= allowed
            and not word_violations(
                station.config,
                station.words,
                word.letters,
                BOUNDARY_EXPLICIT,
                recent=recent,
            )
        ][:count]
    else:
        candidates = _sample_bank(
            station,
            alphabet=alphabet,
            length=length,
            count=count,
            recent=recent,
            depth_is_unlocked=unlocked,
        )

    return {
        "track": track,
        "length": length,
        "letters": alphabet,
        "depth_pairs_unlocked": unlocked,
        "excluded_recent": list(recent),
        "candidates": candidates,
    }


def _sample_bank(
    station,
    *,
    alphabet: Sequence[str],
    length: int,
    count: int,
    recent: Sequence[str],
    depth_is_unlocked: bool,
) -> List[str]:
    """제약을 통과하는 판별 문항 후보를 모은다. 못 채우면 채운 만큼만 낸다.

    빈 목록도 정상적인 답이다 — 예컨대 깊이쌍이 잠긴 채 글자를 E·I 로 묶어
    2글자를 요청하면 통과하는 조합이 없다. 그 사실을 후보 0개로 알린다.
    """
    seen = [text.upper() for text in recent]
    found: List[str] = []
    for _ in range(_BANK_MAX_ATTEMPTS):
        if len(found) >= count:
            break
        text = "".join(station.rng.choice(list(alphabet)) for _ in range(length))
        if text in found:
            continue
        if discrimination_violations(
            station.config, text, recent=seen, depth_unlocked=depth_is_unlocked
        ):
            continue
        found.append(text)
    return found


@router.get("/learners/{learner_name}/stats")
def learner_stats_by_name(request: Request, learner_name: str) -> Dict[str, Any]:
    """학습자 상세 (PRD 12.1 `get_learner_stats`).

    **트랙별로만** 낸다. 모든 수치가 트랙 아래에 들어 있어 합친 숫자를 담을
    자리가 구조에 없다. 기록이 없는 이름도 정상이다 — 0 으로 채워 낸다.
    """
    station = _station(request)
    learner_id = _learner_id_by_name(station, learner_name)

    by_track: Dict[str, Any] = {}
    unlocked = False
    with station.database.session() as db:
        for track in TRACKS:
            summary = track_summary(db, track, learner_id=learner_id)
            by_track[track] = {
                "answered": summary.answered,
                "correct": summary.correct,
                "wrong": summary.wrong,
                "length_errors": summary.length_errors,
                "order_errors": summary.order_errors,
                "accuracy": summary.accuracy,
                "order_error_rate": summary.order_error_rate,
                "avg_response_ms": summary.avg_response_ms,
                "confusions": [
                    {"pair": list(c.pair), "count": c.count, "listed": c.listed}
                    for c in confusion_counts(
                        db, station.config, track, learner_id=learner_id
                    )
                ],
                "accuracy_by_hold_ms": [
                    {
                        "hold_ms": bucket.hold_ms,
                        "answered": bucket.answered,
                        "correct": bucket.correct,
                        "accuracy": bucket.accuracy,
                    }
                    for bucket in accuracy_by_hold_ms(
                        db, track, learner_id=learner_id
                    )
                ],
                "recent_items": [
                    dataclasses.asdict(record)
                    for record in recent_items(
                        db,
                        track,
                        limit=RECENT_ITEM_WINDOW,
                        learner_id=learner_id,
                    )
                ],
            }
        if learner_id is not None:
            unlocked = depth_unlocked(
                db, station.config, TRACK_DISCRIMINATION, learner_id=learner_id
            )

    return {
        "learner_name": learner_name,
        "learner_id": learner_id,
        # §20: 판별 트랙에만 걸리는 게이트다.
        "depth_pairs_unlocked": unlocked,
        "by_track": by_track,
    }


# --------------------------------------------------------------------------- #
# MCP 호출 로그
# --------------------------------------------------------------------------- #

@router.post("/mcp-log", status_code=202)
def publish_mcp_log(
    request: Request, body: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """MCP 서버가 자기 호출 기록을 흘려보내는 입구 (PRD 11장 `/ws/mcp-log`).

    MCP 서버는 Claude Desktop 이 띄우는 별도 프로세스라 이 앱의 이벤트 허브에
    직접 넣을 수 없다. 그래서 로그만 여기로 받아 `/ws/mcp-log` 로 부채질한다
    (DECISIONS.md §46).

    **명령 경로가 아니다.** 여기로 들어온 것은 화면에 표시될 뿐이고 손을
    움직이거나 세션을 바꾸지 않는다. 그래서 로그 패널은 감사 기록이 아니다 —
    같은 기계의 무엇이든 여기에 쓸 수 있다 (로컬 무인증 전제, §27).
    """
    station = _station(request)
    type_ = str(body.get("type") or "")
    if type_ not in MCP_LOG_TYPES:
        raise SessionStateError(
            f"type is {type_!r}, expected one of {list(MCP_LOG_TYPES)}"
        )
    event = {key: value for key, value in body.items() if key != "type"}
    event["type"] = type_
    station.hub.publish(MCP_LOG_CHANNEL, event)
    return {"status": "published", "type": type_}
