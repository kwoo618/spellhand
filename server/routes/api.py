"""REST 라우트 (PRD 11장).

라우트는 **얇다.** 판정도 적응도 안전 검사도 하지 않고 `SessionController` 와
`SafetyGate` 를 부를 뿐이다. 거부는 `safety.py` / `session.py` 가 만든 구조화된
예외를 그대로 HTTP 로 옮긴다 — 여기서 에러 문자열을 새로 짓지 않는다.

인증이 없다. 단일 학습자 로컬 실행 전제다 (DECISIONS.md §27).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Query, Request
from sqlalchemy import select

from server.db import TRACKS, Learner
from server.session import (
    AnswerOutcome,
    ItemPrompt,
    SessionController,
    SessionPlan,
    SessionStateError,
)
from server.stats import confusion_counts, track_summary

__all__ = ["router"]

router = APIRouter(prefix="/api")


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
    """비상 정지. 손을 즉시 안전 자세로 만든다."""
    station = _station(request)
    station.device.stop()
    return {"status": "stopped"}


@router.post("/device/reset")
def device_reset(request: Request) -> Dict[str, Any]:
    """물린 락을 서버 재시작 없이 푼다 (PRD 11장).

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
        "status": "reset",
        "ended_sessions": list(ended),
        "lock_holder": station.lock.holder(),
    }
