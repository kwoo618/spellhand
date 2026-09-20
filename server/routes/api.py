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

from server.agent import PlanNotFoundError, PlanStateError
from server.calibration import calibration_hash
from server.db import (
    BOUNDARIES,
    BOUNDARY_EXPLICIT,
    TRACK_DISCRIMINATION,
    TRACK_WORD,
    TRACKS,
    Learner,
)
from server.device import DEVICE_MODE_SERIAL
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
    SessionFinishedError,
    SessionPlan,
    SessionState,
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

# MCP 로그 패널이 받아들이는 이벤트 종류.
#
# 앞의 네 가지는 PRD 11장 `/ws/mcp-log` 에 적힌 것이다. `plan_decided` 는 더한
# 것이다 — HITL 의 **결정**이 로그에 남지 않으면, 패널에 `plan_ready` 가 뜬 뒤
# 정적이 흐르고 갑자기 구동 호출이 통과하는 것처럼 보인다. 시연 ⑥ 의 "라이브
# 정지" 는 "멈췄다 → 사람이 승인했다 → 그래서 움직인다" 가 한 화면에서 이어져야
# 성립한다 (DECISIONS.md §48).
MCP_LOG_TYPES = (
    "tool_call",
    "tool_result",
    "plan_ready",
    "plan_decided",
    "blocked",
)

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


@router.get("/sessions/open")
def open_session(request: Request) -> Dict[str, Any]:
    """지금 참여할 수 있는 세션. 없으면 `session_id` 가 `null` 이다.

    **승인(`/agent/plans/{id}/approve`)이 만든 세션을 학습자가 찾는 창구다.**
    승인은 세션을 만들고 락까지 잡는다 (PRD 13.3). 그 뒤 학습자 화면이
    `POST /sessions` 로 **새** 세션을 내려 하면 `LOCK_UNAVAILABLE` 로 막히고,
    막힌 것을 풀려고 `/device/reset` 을 부르면 방금 승인한 세션이 죽는다.
    승인된 계획이 그렇게 버려졌다 (DECISIONS.md §50).

    락을 쥔 세션을 그대로 돌려준다 — 이 서버가 컨트롤러를 들고 있고 아직
    끝나지 않은 것만이다. 새로 만드는 것이 아니므로 락도 건드리지 않는다.

    문항이 이미 걸려 있으면 `prompt` 도 함께 낸다. 화면을 새로 고쳐 다시
    들어오는 경로가 그것이다 — 한 번 더 물어보게 하지 않는다.

    **`planned` 가 구동 경로를 가른다.** 승인된 계획에서 온 세션은 문항이
    계획에서 나와야 하므로 `/planned` 로 집행한다. `/next` 로 내면 규칙이
    문항을 새로 뽑아 승인된 글자열이 버려진다 — 그것은 HITL 을 무효로 만드는
    것이고, 고치려던 결함의 다른 얼굴이다 (DECISIONS.md §50).
    """
    station = _station(request)
    holder = station.lock.holder()
    controller = (
        station.controllers.get(holder) if holder is not None else None
    )
    if controller is None or controller.finished:
        # 락이 비었거나, 쥔 세션이 이 서버의 것이 아니거나, 이미 끝났다.
        # 셋 다 "참여할 세션이 없다" 다 — 화면은 이름 입력으로 간다.
        return {"session_id": None}

    payload: Dict[str, Any] = {
        "session_id": controller.session_id,
        "learner_id": controller.learner_id,
        "learner_name": _learner_name(station, controller.learner_id),
        "total_items": controller.total_items,
        "index": controller.index,
        "state": controller.state,
        # 계획에서 온 세션인가. 화면은 이 칸으로 구동 경로를 고른다.
        "planned": False,
    }
    try:
        stored = station.plans.for_session(controller.session_id)
    except PlanNotFoundError:
        pass
    else:
        payload["planned"] = True
        payload["plan_id"] = stored.plan_id
        payload["remaining_items"] = len(stored.queue)
    if controller.state in (SessionState.AWAITING_PRESENT, SessionState.AWAITING_ANSWER):
        # 문항을 읽다 실패해도 **이 창구는 죽지 않는다.** 여기서 예외가 나면
        # 참여 조회 자체가 500 이 되고, 그러면 아무도 세션에 들어갈 수 없다 —
        # 락을 쥔 세션이 남아 있는데 화면은 이름 입력만 내놓는 최악의 조합이다.
        # 실제로 그렇게 됐다 (DECISIONS.md §50.4). 문항만 빼고 나머지는 낸다.
        try:
            payload["prompt"] = _prompt_payload(
                controller.current_prompt(), controller
            )
        except Exception:  # noqa: BLE001 - 조회가 구동 상태 때문에 죽지 않게
            logger.exception(
                "could not read the current prompt of session %s",
                controller.session_id,
            )
    return payload


def _learner_name(station, learner_id: int) -> Optional[str]:
    with station.database.session() as db:
        learner = db.get(Learner, learner_id)
        return learner.name if learner is not None else None


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
    # 끝난 세션으로는 계획을 더 집행할 수 없다.
    station.plans.forget_session(session_id)
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
    """상태, 모드, 캘리브레이션 해시 일치 여부 (PRD 11장)."""
    station = _station(request)
    expected = calibration_hash(station.config)
    ok, state = _calibration_state(station, expected)
    return {
        "mode": station.device_mode,
        "busy": station.device.is_busy,
        "lock_holder": station.lock.holder(),
        # 서버가 기대하는 해시. `firmware/hand_controller/calibration.h` 의
        # `CAL_HASH` 와 눈으로 맞춰 볼 수 있는 값이다 (DECISIONS.md §35).
        "calibration_hash": expected,
        # **`true` / `false` / `null` 만 담는다.** "n/a" 같은 문자열을 넣지 않는
        # 이유는, `_ok` 라는 이름 때문에 호출자가 `if not ok:` 로 읽을 것이고
        # 그러면 "해당 없음" 과 "불일치" 가 같은 분기로 들어가기 때문이다 —
        # 이 필드가 막으려는 사고가 바로 그것이다. 사유는 아래 칸에 둔다.
        "calibration_hash_ok": ok,
        # ok | mismatch | not_applicable | unknown
        "calibration_hash_state": state,
    }


def _calibration_state(station, expected: str) -> "tuple[Optional[bool], str]":
    """해시 대조 상태.

    대조는 `SerialDevice.connect()` 의 핸드셰이크에서 일어난다. 불일치면 연결이
    거부되어 서버가 기동하지 못하므로, **살아 있는 serial 서버에서 `mismatch` 는
    나오지 않는다** (DECISIONS.md §35, §48). 그래도 비교를 그대로 두는 것은,
    나중에 연결 시점 거부를 무르더라도 이 필드가 계속 사실을 말하게 하기 위한
    것이다.
    """
    if station.device_mode != DEVICE_MODE_SERIAL:
        # Mock 에는 펌웨어가 없다. 대조할 상대가 없는 것이지 틀린 것이 아니다.
        return None, "not_applicable"
    verified = getattr(station.device, "verified_calibration_hash", None)
    if verified is None:
        # 아직 연결되지 않았거나 끊겼다. 지금 손에 올라간 펌웨어를 알지 못한다.
        return None, "unknown"
    return verified == expected, "ok" if verified == expected else "mismatch"


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
    # `type` 이 먼저 나오게 둔다. 로그 패널이 줄마다 종류부터 읽는다.
    event: Dict[str, Any] = {"type": type_}
    event.update(
        {key: value for key, value in body.items() if key != "type"}
    )
    station.hub.publish(MCP_LOG_CHANNEL, event)
    return {"status": "published", "type": type_}


# --------------------------------------------------------------------------- #
# 에이전트 — 계획 생성과 HITL 승인 (PRD 11장, 13.3)
# --------------------------------------------------------------------------- #

@router.post("/agent/plan", status_code=201)
def create_plan(request: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """자연어 요청을 세션 계획으로 바꾼다. **실행하지 않는다.**

    검증 10항목을 통과해도 세션은 만들어지지 않는다 (PRD 13.3). 승인 대기
    상태가 시연 ⑥ 의 "라이브 정지" 항목이다. 세션은
    `/api/agent/plans/{id}/approve` 에서만 생긴다.

    LLM 을 부를 수 없거나 재생성 2회가 실패하면 규칙 폴백으로 내려간다
    (PRD 13.4). 그때도 응답 형식은 같고 `source` 가 `rule` 이 된다.
    """
    station = _station(request)
    learner_name = str(body.get("learner_name") or "").strip()
    if not learner_name:
        raise SessionStateError("learner_name is required")
    prompt = str(body.get("request") or "").strip()
    if not prompt:
        raise SessionStateError("request is required")

    default = SessionPlan()
    plan, report, attempts = station.agent.plan(
        learner_name,
        prompt,
        discrimination_items=int(
            body.get("discrimination_items", default.discrimination_items)
        ),
        word_items=int(body.get("word_items", default.word_items)),
    )
    stored = station.plans.add(learner_name, prompt, plan, report, attempts)
    # 계획이 준비되었고 **아직 실행되지 않았다**는 사실을 로그 패널로 보낸다
    # (PRD 11장 `plan_ready`, 시연 ⑥).
    _publish_plan(station, "plan_ready", stored)
    return stored.as_json()


def _publish_plan(station, type_: str, stored) -> None:
    """계획 사건을 MCP 로그 채널로 흘린다.

    `plan_id` 와 `source` 는 PRD 11장의 예시에 없지만 더했다. 화면에 뜬 계획과
    로그 줄을 잇는 것이 `plan_id` 이고, 규칙 폴백이 돌았는지가 계획 하나에 대해
    가장 먼저 알아야 할 사실이기 때문이다 (DECISIONS.md §48).
    """
    station.hub.publish(
        MCP_LOG_CHANNEL,
        {
            "type": type_,
            "plan_id": stored.plan_id,
            "status": stored.status,
            "source": stored.plan.source,
            "items": len(stored.plan.items),
            "estimated_seconds": stored.plan.estimated_seconds,
            "session_id": stored.session_id,
        },
    )


@router.post("/agent/plans/{plan_id}/approve", status_code=201)
def approve_plan(request: Request, plan_id: int) -> Dict[str, Any]:
    """사람이 승인한다. **여기서 처음으로 세션이 생긴다** (PRD 13.3).

    세션의 문항 칸은 승인된 계획의 문항 수와 같다 — 더 열면 계획 밖의 문항이
    들어갈 자리가 생긴다. 이미 승인·거부된 계획은 다시 결정할 수 없다.

    `source` 는 계획을 만든 쪽이다 (`llm` 또는 `rule`). 4주차 실데이터에서
    어느 계획자가 만든 세션인지 구분하기 위한 것이다.
    """
    station = _station(request)
    stored = station.plans.pending(plan_id)
    if not stored.plan.items:
        raise PlanStateError(
            f"plan {plan_id} has no item left after validation; "
            f"make a new plan instead of approving an empty one"
        )

    controller = station.new_controller()
    session_id = controller.start(
        stored.learner_name,
        plan=stored.plan.as_session_plan(),
        source=stored.plan.source,
    )
    station.controllers[session_id] = controller
    station.plans.approve(plan_id, session_id)
    _publish_plan(station, "plan_decided", stored)
    payload = stored.as_json()
    payload["total_items"] = controller.total_items
    payload["state"] = controller.state
    return payload


@router.post("/agent/plans/{plan_id}/reject")
def reject_plan(request: Request, plan_id: int) -> Dict[str, Any]:
    """사람이 거부한다. 세션은 만들어지지 않는다."""
    station = _station(request)
    stored = station.plans.reject(plan_id)
    # 거부는 이것 말고는 아무 흔적도 남기지 않는다. 로그에 남기지 않으면 계획이
    # 조용히 사라진 것과 구분되지 않는다.
    _publish_plan(station, "plan_decided", stored)
    return stored.as_json()


@router.post("/sessions/{session_id}/planned")
def present_planned(request: Request, session_id: int) -> Dict[str, Any]:
    """**승인된 계획의** 다음 문항을 표현한다 (HITL 이후의 유일한 구동 경로).

    글자열이 계획에서 나오므로 승인되지 않은 문항이 이 경로로 나갈 수 없다.
    `/sequence` 는 호출자가 글자를 준다 — 그쪽은 MCP·에이전트의 자유 구동이고
    이쪽은 승인의 집행이다 (DECISIONS.md §47).

    오답 1회 뒤 재생 대기 중이면 **같은 문항**을 다시 낸다. 큐에서 빼지 않으므로
    재생이 건너뛰어지지 않는다 (기획서 5.2).

    **동기다.** `/sequence` 와 같은 이유로 손이 끝날 때까지 돌려주지 않는다
    (DECISIONS.md §46.2). 거부되면 문항이 큐에 그대로 남아 다시 시도할 수 있다.
    """
    station = _station(request)
    _require_lock(station, session_id)
    controller = _controller(request, session_id)
    stored = station.plans.for_session(session_id)

    replay = controller.awaiting_replay
    if replay:
        item = stored.last
        if item is None:
            raise SessionStateError(
                f"session {session_id} is awaiting a replay but no planned item "
                f"has been presented yet"
            )
    else:
        if not stored.queue:
            raise SessionFinishedError(
                f"the approved plan for session {session_id} has no item left"
            )
        item = stored.queue[0]

    prompt = controller.present_letters(
        item.letters,
        track=item.track,
        hold_ms=_clamp_hold_ms(station, item.hold_ms),
        boundary=item.boundary,
    )
    # 손이 실제로 표현한 뒤에 큐에서 뺀다. 거부되면 같은 문항으로 다시 부를 수 있다.
    if not replay:
        stored.queue.pop(0)
    stored.last = item

    payload = _prompt_payload(prompt, controller)
    payload["status"] = "presented"
    payload["plan_id"] = stored.plan_id
    payload["remaining_items"] = len(stored.queue)
    return payload
