"""REST 라우트와 WebSocket 테스트 (PRD 11장).

디바이스 동작은 **인라인**으로 돌린다. 실제 서버는 백그라운드 스레드를 쓰지만
(`_thread_background`), 테스트는 `background` 를 주입해 순서를 확정한다.
시간도 주입한다 — 그러지 않으면 rate limit 이 실제 운용과 다르게 걸린다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from server.db import TRACK_DISCRIMINATION, TRACK_WORD, Database
from server.device import (
    DeviceAdapter,
    DeviceCancelledError,
    DeviceProtocolError,
)
from server.device.mock import MockDevice
from server.routes import build_station, create_app
from tests.conftest import REAL_POSES, REAL_WORDS

SMALL_PLAN = {"discrimination_items": 2, "word_items": 1}


@pytest.fixture
def station(config, clock):
    """인라인 실행 + 주입 시계로 조립한 서버.

    실제 서버는 구동을 백그라운드 스레드로 돌리지만, 테스트는 `background` 를
    인라인으로 주입해 순서를 확정한다. 시계도 가짜 sleep 과 물려 두어야
    rate limit 이 실제 운용과 같은 조건에서 걸린다.
    """
    return build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
    )


@pytest.fixture
def client(station):
    with TestClient(create_app(station)) as test_client:
        yield test_client


def start(client, **overrides):
    body = {"learner_name": "haru", **SMALL_PLAN, **overrides}
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def answer_correctly(client, station, session_id):
    """서버가 들고 있는 정답으로 답한다. 클라이언트 표현에는 정답이 없다."""
    controller = station.controllers[session_id]
    return client.post(
        f"/api/sessions/{session_id}/answer",
        json={"answer": controller._active_answer()},
    )


# --------------------------------------------------------------------------- #
# 정상 흐름
# --------------------------------------------------------------------------- #

def test_learners_start_empty(client):
    assert client.get("/api/learners").json() == {"learners": []}


def test_a_session_can_be_started(client):
    body = start(client)
    assert body["session_id"] > 0
    assert body["total_items"] == 3
    assert body["state"] == "awaiting_item"
    assert client.get("/api/learners").json()["learners"] == ["haru"]


def test_the_full_loop_runs_to_a_summary(client, station):
    session = start(client)
    sid = session["session_id"]

    for _ in range(session["total_items"]):
        prompt = client.get(f"/api/sessions/{sid}/next")
        assert prompt.status_code == 200, prompt.text
        assert client.post(f"/api/sessions/{sid}/present").status_code == 202
        outcome = answer_correctly(client, station, sid)
        assert outcome.status_code == 200, outcome.text
        assert outcome.json()["result"] == "correct"

    summary = client.post(f"/api/sessions/{sid}/end")
    assert summary.status_code == 200
    body = summary.json()
    assert body["by_track"][TRACK_DISCRIMINATION]["answered"] == 2
    assert body["by_track"][TRACK_WORD]["answered"] == 1
    assert body["by_track"][TRACK_DISCRIMINATION]["accuracy"] == 1.0


def test_the_summary_has_no_combined_metric(client, station):
    session = start(client)
    sid = session["session_id"]
    for _ in range(session["total_items"]):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        answer_correctly(client, station, sid)

    body = client.post(f"/api/sessions/{sid}/end").json()
    assert set(body["by_track"]) == {TRACK_DISCRIMINATION, TRACK_WORD}
    for forbidden in ("accuracy", "overall", "total_accuracy"):
        assert forbidden not in body


def test_heartbeat_keeps_the_lock(client):
    sid = start(client)["session_id"]
    response = client.post(f"/api/sessions/{sid}/heartbeat")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_device_status_reports_the_lock_holder(client):
    assert client.get("/api/device/status").json()["lock_holder"] is None
    sid = start(client)["session_id"]
    status = client.get("/api/device/status").json()
    assert status["mode"] == "mock"
    assert status["lock_holder"] == sid
    assert status["busy"] is False


def test_device_status_says_the_hash_check_does_not_apply_to_mock(client, station):
    """Mock 에는 펌웨어가 없다. 대조할 상대가 없는 것이지 틀린 것이 아니다.

    `calibration_hash_ok` 에 "n/a" 같은 문자열을 넣지 않는다 — 이름이 `_ok` 이므로
    호출자가 `if not ok:` 로 읽을 것이고, 그러면 "해당 없음" 과 "불일치" 가 같은
    분기로 들어간다. 이 필드가 막으려는 사고가 바로 그것이다 (DECISIONS §48).
    """
    from server.calibration import calibration_hash

    status = client.get("/api/device/status").json()
    assert status["calibration_hash"] == calibration_hash(station.config)
    assert status["calibration_hash_ok"] is None
    assert status["calibration_hash_state"] == "not_applicable"


def test_device_status_reports_a_verified_hash_in_serial_mode(client, station):
    """serial 모드에서 연결이 살아 있으면 대조를 통과한 상태다.

    `mismatch` 는 살아 있는 서버에서 나오지 않는다 — `connect()` 가 연결을
    거부하므로 서버가 기동하지 못한다 (DECISIONS §35, §48).
    """
    from server.calibration import calibration_hash
    from server.device import DEVICE_MODE_SERIAL

    expected = calibration_hash(station.config)

    class Verified:
        """해시 대조를 통과한 시리얼 어댑터. 상태 조회에 필요한 만큼만 흉내낸다."""

        verified_calibration_hash = expected
        is_busy = False

    station.device_mode = DEVICE_MODE_SERIAL
    station.device = Verified()  # type: ignore[assignment]
    status = client.get("/api/device/status").json()
    assert status["calibration_hash_ok"] is True
    assert status["calibration_hash_state"] == "ok"

    # 끊긴 뒤에는 지금 손에 올라간 펌웨어를 알지 못한다.
    Verified.verified_calibration_hash = None  # type: ignore[assignment]
    status = client.get("/api/device/status").json()
    assert status["calibration_hash_ok"] is None
    assert status["calibration_hash_state"] == "unknown"


def test_device_stop_returns_to_safe(client, station):
    start(client)
    assert client.post("/api/device/stop").json()["status"] == "stopped"
    assert dict(station.device.current_flex()) == dict(station.config.safe)


def test_stats_endpoints_are_track_scoped(client, station):
    session = start(client)
    sid = session["session_id"]
    for _ in range(session["total_items"]):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        answer_correctly(client, station, sid)
    client.post(f"/api/sessions/{sid}/end")

    confusion = client.get(
        "/api/stats/confusion", params={"track": TRACK_DISCRIMINATION}
    )
    assert confusion.status_code == 200
    assert confusion.json()["track"] == TRACK_DISCRIMINATION

    learner = client.get(f"/api/stats/learners/{session['learner_id']}")
    assert set(learner.json()["by_track"]) == {TRACK_DISCRIMINATION, TRACK_WORD}


def test_confusion_stats_require_a_track(client):
    """track 에 기본값을 두지 않는다. 실수로 두 트랙을 합쳐 보는 일을 막는다."""
    assert client.get("/api/stats/confusion").status_code == 422


def test_the_confusion_endpoint_is_a_directed_matrix_with_the_diagonal(client, station):
    """`expected × given` 칸이 온다. 맞게 읽은 위치가 대각선 칸으로 들어 있고,
    틀린 칸은 방향을 가진다 — 보여 준 글자가 `expected` 다 (§60)."""
    sid = start(client)["session_id"]
    _answer_wrong_twice(client, station, sid)
    body = client.get(
        "/api/stats/confusion", params={"track": TRACK_DISCRIMINATION}
    ).json()

    assert body["track"] == TRACK_DISCRIMINATION
    assert body["letters"] == sorted(station.config.poses)
    assert "pairs" not in body
    cells = body["cells"]
    assert {"expected", "given", "count", "diagonal", "listed"} <= set(cells[0])
    diagonal = [c for c in cells if c["diagonal"]]
    wrong = [c for c in cells if not c["diagonal"]]
    assert diagonal and wrong
    assert all(c["expected"] == c["given"] and not c["listed"] for c in diagonal)
    assert all(c["expected"] != c["given"] for c in wrong)
    # 두 번 틀렸다. 한 번에 한 칸씩, 첫 글자에서.
    assert sum(c["count"] for c in wrong) == 2
    assert body["items"] == 2
    # 한 번도 틀리지 않은 등재 쌍도 격자에 표시할 수 있게 설정의 혼동쌍이 온다.
    assert body["listed_pairs"] == [
        list(p.pair) for p in station.config.confusion_pairs
    ]
    listed = {frozenset(p) for p in body["listed_pairs"]}
    for cell in wrong:
        assert cell["listed"] == (frozenset((cell["expected"], cell["given"])) in listed)


def test_the_confusion_endpoint_keeps_the_tracks_apart(client, station):
    """판별 기록은 단어 행렬에 없다. 트랙마다 따로 묻고 따로 온다."""
    sid = start(client)["session_id"]
    _answer_wrong_twice(client, station, sid)
    disc = client.get(
        "/api/stats/confusion", params={"track": TRACK_DISCRIMINATION}
    ).json()
    word = client.get("/api/stats/confusion", params={"track": TRACK_WORD}).json()

    assert disc["cells"] and disc["items"] == 2
    assert word["cells"] == [] and word["items"] == 0
    assert word["track"] == TRACK_WORD


def test_the_index_redirects_to_the_station(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/station.html"


def test_the_old_page_addresses_still_land_on_the_station(client):
    """학습자 뷰와 진단 뷰는 한 화면이 됐다. 예전 주소는 살아 있어야 한다 —
    PRD 14장과 시연 대본이 그 주소를 적고 있다 (DECISIONS §52).

    `/teacher.html` 은 교사 모드로 연다. 학습자 모드로 떨어뜨리면 교사가
    발표 중에 모드를 찾아 헤맨다."""
    practice = client.get("/practice.html", follow_redirects=False)
    assert practice.status_code in (302, 307)
    assert practice.headers["location"] == "/station.html"

    teacher = client.get("/teacher.html", follow_redirects=False)
    assert teacher.status_code in (302, 307)
    assert teacher.headers["location"] == "/station.html?mode=teacher"


# --------------------------------------------------------------------------- #
# 정답 유출 회귀 — 완화하지 않는다
# --------------------------------------------------------------------------- #

def test_no_response_body_reveals_the_answer_before_it_is_earned(client, station):
    """제출 전까지 어떤 응답 본문에도 정답이 나오지 않는다."""
    session = start(client)
    sid = session["session_id"]
    controller = station.controllers[sid]

    for _ in range(session["total_items"]):
        prompt = client.get(f"/api/sessions/{sid}/next")
        expected = controller._active_answer()

        # /next 와 /present 응답 어디에도 정답이 없다.
        assert expected not in prompt.text, prompt.text
        present = client.post(f"/api/sessions/{sid}/present")
        assert expected not in present.text, present.text
        assert expected not in client.get("/api/device/status").text

        answer_correctly(client, station, sid)


def test_a_wrong_first_attempt_does_not_reveal_the_answer(client, station):
    """오답 1회째에는 정답도 혼동 내역도 내보내지 않는다 (기획서 5.2).

    재생을 시키면서 정답을 알려주면 재생이 의미를 잃는다.
    """
    session = start(client)
    sid = session["session_id"]
    controller = station.controllers[sid]

    client.get(f"/api/sessions/{sid}/next")
    client.post(f"/api/sessions/{sid}/present")
    expected = controller._active_answer()
    wrong = ("B" if expected[0] != "B" else "W") + expected[1:]

    response = client.post(
        f"/api/sessions/{sid}/answer", json={"answer": wrong}
    )
    body = response.json()

    assert body["retry"] is True
    assert body["expected"] is None
    assert body["confusions"] == []
    assert body["demo"] is None
    assert expected not in response.text


def test_the_answer_is_revealed_once_the_item_is_over(client, station):
    """제출이 끝난 문항의 정답은 공개된다 (기획서 5.2)."""
    session = start(client)
    sid = session["session_id"]
    controller = station.controllers[sid]

    client.get(f"/api/sessions/{sid}/next")
    client.post(f"/api/sessions/{sid}/present")
    expected = controller._active_answer()
    body = answer_correctly(client, station, sid).json()

    assert body["result"] == "correct"
    assert body["retry"] is False
    assert body["expected"] == expected


# --------------------------------------------------------------------------- #
# 게이트 우회 불가 — R4
# --------------------------------------------------------------------------- #

def test_present_without_a_session_is_rejected(client):
    """서버에 없는 세션으로 구동을 요청하면 거부된다."""
    response = client.post("/api/sessions/999/present")
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"


def test_present_before_an_item_exists_is_rejected(client):
    sid = start(client)["session_id"]
    response = client.post(f"/api/sessions/{sid}/present")
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"


def test_answer_before_present_is_rejected(client):
    sid = start(client)["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    response = client.post(f"/api/sessions/{sid}/answer", json={"answer": "VWB"})
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"


def test_a_route_cannot_drive_the_device_without_the_lock(client, station):
    """라우트가 게이트를 우회하지 않는다.

    락을 강제로 빼앗은 뒤 구동을 요청하면, 게이트가 LOCK_NOT_HELD 로 막는다.
    라우트는 디바이스를 직접 만지지 않으므로 우회할 길이 없다.
    """
    session = start(client)
    sid = session["session_id"]
    client.get(f"/api/sessions/{sid}/next")

    # 다른 주인이 락을 가져간 상황을 만든다.
    station.lock.release(sid)
    station.lock.acquire(9999)

    events = []
    station.hub.publish = lambda session_id, event: events.append(event)

    client.post(f"/api/sessions/{sid}/present")

    assert events, "거부가 이벤트로 나가야 한다"
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "LOCK_NOT_HELD"


def test_a_second_session_cannot_start_while_the_lock_is_held(client, station):
    start(client)
    response = client.post(
        "/api/sessions", json={"learner_name": "mina", **SMALL_PLAN}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LOCK_UNAVAILABLE"


def test_rejections_carry_the_structured_code(client, station):
    """라우트가 에러 문자열을 새로 짓지 않는다. 계층이 만든 코드를 그대로 싣는다."""
    from server.safety import UnsupportedLettersError

    session = start(client)
    sid = session["session_id"]
    controller = station.controllers[sid]
    client.get(f"/api/sessions/{sid}/next")

    def boom() -> None:
        raise UnsupportedLettersError(["C"], sorted(station.config.poses))

    events = []
    station.hub.publish = lambda session_id, event: events.append(event)
    station.run_in_background(sid, boom)

    assert events[-1]["code"] == "UNSUPPORTED_LETTERS"


def test_demo_replay_is_refused_before_a_demo_exists(client, station):
    """시범은 정답 공개다. 아직 맞혀야 하는 문항에는 내주지 않는다."""
    session = start(client)
    sid = session["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    client.post(f"/api/sessions/{sid}/present")

    response = client.post(f"/api/sessions/{sid}/demo")
    assert response.status_code == 409
    assert response.json()["code"] == "SESSION_STATE"


# --------------------------------------------------------------------------- #
# 구동 중 실패와 락 반납 (DECISIONS.md §42)
# --------------------------------------------------------------------------- #

class BreakingDevice(MockDevice):
    """지정한 예외를 `move_to` 에서 던지는 Mock.

    실기에서 실제로 난 실패를 재현한다 — 앞선 `Z` 의 응답이 버퍼에 남아
    다음 `P` 의 ACK 자리에서 읽히는 상황이다.
    """

    def __init__(self, config, error, **kwargs):
        super().__init__(config, **kwargs)
        self._error = error
        self.moves = 0

    def move_to(self, pose, transition_ms, hold_ms):
        self.moves += 1
        raise self._error


def breaking_station(config, clock, error):
    return build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=BreakingDevice(config, error, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
    )


def test_a_device_failure_during_a_move_releases_the_lock(config, clock):
    """R4: 구동 중 예외가 나도 락은 반납된다.

    실기에서 이것이 풀리지 않아 다음 세션이 `LOCK_UNAVAILABLE` 로 시작조차
    하지 못했다 (DECISIONS.md §42). 가짜 시계라 만료로 풀릴 길은 없다 —
    명시적으로 놓아야만 통과한다.
    """
    station = breaking_station(
        config, clock, DeviceProtocolError("expected ACK, got 'RDY -1'")
    )
    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]
        assert station.lock.holder() == sid

        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")

        assert station.lock.holder() is None, "구동 실패가 락을 물고 있다"
        assert sid not in station.controllers

        # 그리고 다음 세션이 곧바로 시작된다.
        assert client.post(
            "/api/sessions", json={"learner_name": "nari", **SMALL_PLAN}
        ).status_code == 201


def test_a_device_failure_is_reported_before_the_session_is_closed(config, clock):
    """조용히 접지 않는다. 무엇이 실패했는지가 먼저 나간다."""
    station = breaking_station(config, clock, DeviceProtocolError("boom"))
    events = []
    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]
        client.get(f"/api/sessions/{sid}/next")
        station.hub.publish = lambda session_id, event: events.append(event)
        client.post(f"/api/sessions/{sid}/present")

    failures = [e for e in events if e["type"] == "error"]
    assert [e["code"] for e in failures] == ["DEVICE_FAILED"]
    assert "boom" in failures[0]["detail"]
    # 실패가 먼저 나가고 그다음에 세션이 닫힌다.
    assert events.index(failures[0]) < len(events) - 1
    assert events[-1]["type"] == "ended"


def test_a_cancelled_move_keeps_the_session_alive(config, clock):
    """정지는 통신 오류가 아니다. 락도 세션도 그대로 두고, 다음 문항으로 간다."""
    station = breaking_station(
        config, clock, DeviceCancelledError("cancelled by a stop before RDY")
    )
    events = []
    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]
        client.get(f"/api/sessions/{sid}/next")
        station.hub.publish = lambda session_id, event: events.append(event)
        client.post(f"/api/sessions/{sid}/present")

        assert station.lock.holder() == sid, "사람이 멈춘 것으로 락을 놓지 않는다"
        assert sid in station.controllers
        assert [e["code"] for e in events if e["type"] == "error"] == ["STOPPED"]

        # 세션은 살아 있다 — 같은 문항을 다시 보여 달라고 할 수 있다.
        assert client.post(f"/api/sessions/{sid}/present").status_code == 202


def test_a_failure_while_closing_still_releases_the_lock(config, clock):
    """정리 중에 손이 또 실패해도 락은 놓는다.

    `end()` 는 안전 자세로 보내려 한다. 그것마저 실패하는 것이 실기의
    현실적인 경우다 — 포트가 이미 죽었기 때문이다.
    """
    station = breaking_station(config, clock, DeviceProtocolError("boom"))

    def always_fails() -> None:
        raise DeviceProtocolError("the port is gone")

    station.device.safe = always_fails  # type: ignore[method-assign]

    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")

        assert station.lock.holder() is None
        assert sid not in station.controllers


# --------------------------------------------------------------------------- #
# 강제 해제
# --------------------------------------------------------------------------- #

def test_reset_ends_the_running_session_and_releases_the_lock(client, station):
    session = start(client)
    sid = session["session_id"]
    client.get(f"/api/sessions/{sid}/next")

    response = client.post("/api/device/reset")
    assert response.status_code == 200
    body = response.json()
    assert body["ended_sessions"] == [sid]
    assert body["lock_holder"] is None

    assert station.lock.holder() is None
    assert sid not in station.controllers


def test_reset_records_the_session_as_aborted(client, station):
    """답한 문항까지의 기록은 남는다. 중단 세션과 같은 취급이다 (§39)."""
    from server.db import TrainingSession

    session = start(client)
    sid = session["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    client.post(f"/api/sessions/{sid}/present")
    answer_correctly(client, station, sid)

    client.post("/api/device/reset")

    with station.database.session() as db:
        row = db.get(TrainingSession, sid)
        assert row.status == "aborted"
        assert row.ended_at is not None

    stats = client.get(f"/api/stats/learners/{session['learner_id']}").json()
    assert stats["by_track"][TRACK_DISCRIMINATION]["answered"] == 1


def test_reset_releases_a_lock_whose_controller_is_gone(client, station):
    """서버는 살아 있는데 컨트롤러만 사라진 경우. 락만 놓는다."""
    session = start(client)
    sid = session["session_id"]
    station.controllers.pop(sid)

    body = client.post("/api/device/reset").json()
    assert body["ended_sessions"] == [sid]
    assert station.lock.holder() is None


def test_reset_is_harmless_when_nothing_is_held(client, station):
    body = client.post("/api/device/reset").json()
    assert body["ended_sessions"] == []
    assert body["lock_holder"] is None


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #

def test_websocket_receives_presentation_events(client, station):
    session = start(client)
    sid = session["session_id"]

    with client.websocket_connect(f"/ws/session/{sid}") as ws:
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")

        events = [ws.receive_json() for _ in range(3)]

    kinds = [e["type"] for e in events]
    assert kinds[0] == "presenting"
    assert "ready" in kinds
    assert events[0]["total"] == session["total_items"]


def test_websocket_never_carries_the_answer(client, station):
    """WS 로 정답이 나가지 않는다 — 재생이 남아 있는 동안에는 특히."""
    session = start(client)
    sid = session["session_id"]
    controller = station.controllers[sid]

    with client.websocket_connect(f"/ws/session/{sid}") as ws:
        client.get(f"/api/sessions/{sid}/next")
        expected = controller._active_answer()
        client.post(f"/api/sessions/{sid}/present")

        wrong = ("B" if expected[0] != "B" else "W") + expected[1:]
        client.post(f"/api/sessions/{sid}/answer", json={"answer": wrong})

        seen = []
        for _ in range(len(expected) + 3):
            seen.append(ws.receive_json())
            if seen[-1]["type"] == "judged":
                break

    judged = [e for e in seen if e["type"] == "judged"]
    assert judged and judged[0]["retry"] is True
    for event in seen:
        assert expected not in json.dumps(event, ensure_ascii=False), event


def test_websocket_ignores_anything_the_client_sends(client, station):
    """WS 는 단방향이다. 명령 경로는 REST 하나뿐이다."""
    session = start(client)
    sid = session["session_id"]

    with client.websocket_connect(f"/ws/session/{sid}") as ws:
        ws.send_json({"type": "present", "please": "drive the hand"})
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        first = ws.receive_json()

    # 보낸 것은 무시되고, 서버가 알린 상태만 돌아온다.
    assert first["type"] == "presenting"
    assert station.gate.requests_for(sid) == 1, "WS 로는 구동이 일어나지 않는다"


def test_a_late_subscriber_still_sees_the_current_state(client, station):
    session = start(client)
    sid = session["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    client.post(f"/api/sessions/{sid}/present")

    # 표현이 끝난 뒤에 접속해도 무슨 일이 있었는지 받는다.
    with client.websocket_connect(f"/ws/session/{sid}") as ws:
        replayed = [ws.receive_json() for _ in range(2)]

    assert replayed[0]["type"] == "presenting"


# --------------------------------------------------------------------------- #
# 정적 파일
# --------------------------------------------------------------------------- #

def test_the_station_page_is_served(client):
    response = client.get("/station.html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_page_has_no_build_step_and_no_cdn(client):
    """빌드 도구도 프레임워크도 CDN 도 쓰지 않는다. 단일 파일이다."""
    body = client.get("/station.html").text
    assert "<script src=" not in body
    assert "<link rel=\"stylesheet\"" not in body
    for forbidden in ("react", "vue", "jquery", "tailwind", "cdn."):
        assert forbidden not in body.lower(), forbidden


def test_the_page_never_reads_an_answer_off_the_prompt(client):
    """화면 코드가 문항 표현에서 정답을 찾지 않는다.

    서버가 제출 전에는 정답을 주지 않지만(§22), 화면 쪽에서도 그런 필드를
    기대하지 않아야 나중에 필드가 생겨도 새지 않는다.

    `outcome.expected` 와 시범 이벤트의 `letters` 는 예외다. 둘 다 문항이
    끝난 뒤에만 나오고, 그때는 정답 공개가 의도다 (기획서 5.2).
    """
    body = client.get("/station.html").text
    for forbidden in (
        "prompt.expected",
        "prompt.letters",
        ".expected_letters",
        ".answer_key",
        "state.prompt.expected",
    ):
        assert forbidden not in body, forbidden


def test_the_page_shows_tracks_separately(client):
    """요약은 트랙별로만 그린다. 통합 지표를 만들지 않는다."""
    body = client.get("/station.html").text
    assert "by_track" in body
    for forbidden in ("overall", "total_accuracy", "combined"):
        assert forbidden not in body.lower(), forbidden


def test_the_session_summary_does_not_draw_confusion_pairs(client):
    """학습자가 보는 요약에 혼동 쌍을 그리지 않는다 (DECISIONS §52).

    예전에는 요약에 "혼동 쌍 V/W 3회" 가 있었다. 그것을 보고 다음 세션에
    들어가면 V 가 나올 때마다 W 를 의심하며 본다 — 순수 판별이 아니다.
    `/end` 응답에는 아직 실려 오지만(교사·MCP 도 쓴다) 화면은 읽지 않는다.
    """
    body = client.get("/station.html").text
    assert "confusions_by_track" not in body


# --------------------------------------------------------------------------- #
# 실제 백그라운드 스레드 경로
# --------------------------------------------------------------------------- #

def test_presentation_really_runs_on_a_background_thread(config, clock):
    """다른 테스트는 인라인으로 돌린다. 프로덕션 경로는 스레드다 — 한 번은
    그쪽으로도 지나가 본다.

    `/present` 가 즉시 202 로 돌아오고, 표현 완료는 WS `done` 으로 온다.
    """
    from server.routes import _thread_background

    station = build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=MockDevice(config, sleep=clock.sleep),
        clock=clock,
        background=_thread_background,
    )

    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]

        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            client.get(f"/api/sessions/{sid}/next")
            accepted = client.post(f"/api/sessions/{sid}/present")
            assert accepted.status_code == 202

            kinds = []
            while "done" not in kinds:
                kinds.append(ws.receive_json()["type"])

        assert kinds[0] == "presenting"
        assert kinds[-1] == "done"
        assert station.controllers[sid].state == "awaiting_answer"


# --------------------------------------------------------------------------- #
# mockhand — Mock 디바이스 보기 (PRD 6.3)
# --------------------------------------------------------------------------- #

def test_the_flex_feed_opens_without_a_session(client, station):
    """세션 없이도 열려야 한다. 디바이스만 두드려 보는 개발 중에도 쓴다."""
    with client.websocket_connect("/ws/device") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["fingers"] == list(station.config.fingers)
        assert set(hello["poses"]) == set(station.config.poses)
        assert hello["neutral"] == dict(station.config.neutral)
        assert hello["safe"] == dict(station.config.safe)

        first = ws.receive_json()
        assert first["type"] == "flex"
        assert first["flex"] == dict(station.device.current_flex())

    # 세션은 하나도 만들어지지 않았다.
    assert station.controllers == {}


def test_the_flex_feed_follows_the_device(client, station, config):
    """손이 움직이면 값이 따라온다."""
    with client.websocket_connect("/ws/device") as ws:
        ws.receive_json()  # hello
        before = ws.receive_json()["flex"]

        from server.device import Pose

        station.device.move_to(
            Pose.from_config(config, config.poses["I"]), 900, 800
        )
        after = ws.receive_json()["flex"]

    assert before == dict(config.safe)
    assert after == dict(config.poses["I"])


def test_the_flex_feed_carries_no_letter(client, station, config):
    """서버는 자세 이름을 얹지 않는다. 어느 자세인지는 클라이언트가 되짚는다."""
    with client.websocket_connect("/ws/device") as ws:
        hello = ws.receive_json()
        sample = ws.receive_json()

    assert set(sample) == {"type", "flex", "busy"}
    assert "letter" not in sample and "letters" not in sample
    # 자세 표는 hello 로 한 번만 간다.
    assert "poses" in hello


class StubDevice(DeviceAdapter):
    """실기 자리를 채우는 최소 어댑터. `current_flex` 가 없다.

    실제 포트를 열지 않고 "Mock 이 아닌 디바이스" 상황만 만든다.
    """

    def connect(self): pass
    def disconnect(self): pass
    @property
    def is_busy(self): return False
    def move_to(self, pose, transition_ms, hold_ms): pass
    def neutral(self): pass
    def safe(self): pass
    def stop(self): pass


def test_the_flex_feed_is_mock_only(config, clock):
    """실기 모드에서는 열리지 않는다. 실기는 실물을 보면 된다."""
    station = build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=StubDevice(),
        device_mode="serial",
        clock=clock,
        background=lambda work: work(),
    )

    with TestClient(create_app(station)) as client:
        with client.websocket_connect("/ws/device") as ws:
            message = ws.receive_json()

    assert message["type"] == "unavailable"
    assert message["reason"] == "MOCK_ONLY"
    assert "serial" in message["detail"]


def test_mockhand_page_is_served(client):
    response = client.get("/mockhand.html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_mockhand_has_no_drive_controls(client):
    """읽기 전용이다. 여기서 손을 움직일 수단을 만들지 않는다 —
    구동 경로가 늘면 안전 게이트를 통과시켜야 할 곳이 늘어난다."""
    body = client.get("/mockhand.html").text
    for forbidden in ("/present", "/api/sessions", "/answer", "/device/stop", "<button"):
        assert forbidden not in body, forbidden
    assert 'method: "POST"' not in body and "fetch(" not in body


def test_mockhand_uses_no_framework(client):
    body = client.get("/mockhand.html").text
    assert "<script src=" not in body
    for forbidden in ("react", "vue", "d3", "three.js", "cdn."):
        assert forbidden not in body.lower(), forbidden


def test_mockhand_hides_the_pose_name_by_default(client):
    """발표 폴백으로 쓸 때 학습자가 보는 화면이다. 기본은 숨김 (DECISIONS §41)."""
    body = client.get("/mockhand.html").text
    assert "revealed: false" in body
    assert "applyReveal();   // 기본은 숨김" in body


def _learner_markup(body):
    """학습자 모드에서 보이는 마크업만 떼어낸다 — 사이드바의 학습자 묶음과
    학습자 모드 본문. 교사 묶음·교사 모드 본문·스크립트는 들어가지 않는다."""
    nav_start = body.index('<div class="navGroup" id="learnerNav">')
    nav_end = body.index('<div class="navGroup" id="teacherNav"', nav_start)
    mode_start = body.index('<div id="learnerMode">')
    mode_end = body.index('<div id="teacherMode"', mode_start)
    return body[nav_start:nav_end] + body[mode_start:mode_end]


def _teacher_nav(body):
    start = body.index('<div class="navGroup" id="teacherNav"')
    return body[start:body.index('<div class="modeSwitch">', start)]


def test_learner_mode_does_not_link_to_mockhand(client):
    """제품 기능이 아니다. 학습자 모드에 노출하지 않는다 (PRD 6.3).

    한 화면이 된 뒤로 파일 전체에서 이름이 없을 수는 없다 — 교사 모드가
    새 창으로 띄운다. 그래서 **학습자 모드의 마크업**을 본다 (DECISIONS §52).
    """
    body = client.get("/station.html").text
    assert "mockhand" not in _learner_markup(body)


def test_teacher_mode_opens_mockhand_in_its_own_window(client):
    """통합하지 않고 새 창으로 띄운다. 손을 보면서 화면을 조작하려면 창이
    따로 있어야 한다. 교사 묶음 안에만 있다."""
    body = client.get("/station.html").text
    assert 'id="mockhandBtn"' in _teacher_nav(body)
    assert "window.open(" in body
    assert '"/mockhand.html", "mockhand"' in body


def test_learner_mode_markup_carries_no_confusion_data(client):
    """학습자 모드에는 혼동 행렬·약점 쌍으로 가는 길도, 그리는 자리도 없다.

    자기 약점을 알고 문항을 풀면 판별력 측정이 오염된다 (DECISIONS §52).
    """
    learner = _learner_markup(client.get("/station.html").text)
    for forbidden in ("혼동", "약점", "confusion", "weak", "statsView", "planView"):
        assert forbidden not in learner, forbidden


def test_the_page_opens_in_learner_mode(client):
    """교사 모드는 기본으로 숨는다. 주소가 가리킬 때만 그리로 연다."""
    body = client.get("/station.html").text
    assert '<div id="teacherMode" hidden>' in body
    assert '<div class="navGroup" id="teacherNav" hidden>' in body
    assert '<div id="learnerMode">' in body


def test_the_teacher_door_is_locked_during_a_session(client):
    """세션을 쥐고 있는 동안은 교사 모드로 못 간다 (DECISIONS §52).

    버튼만 잠그면 버튼을 거치지 않는 길이 남는다. 모드를 바꾸는 함수가
    직접 막아야 한다.
    """
    body = client.get("/station.html").text
    assert "return state.sessionId !== null;" in body
    assert 'if (mode === "teacher" && teacherLocked()) { renderNav(); return; }' in body


def test_the_mcp_log_is_joined_only_from_teacher_mode(client):
    """학습자 모드만 쓰는 화면은 진단 채널에 붙지 않는다 (DECISIONS §50, §52).

    로그 소켓을 여는 호출은 `startTeacher()` 안에 하나뿐이다. 끊겼을 때
    다시 붙는 `setTimeout(openLogSocket, ...)` 은 이미 붙은 뒤의 일이다.
    """
    body = client.get("/station.html").text
    start = body.index("function startTeacher() {")
    end = body.index("function openMockhand()", start)
    assert "openLogSocket();" in body[start:end]
    assert body.count("openLogSocket();") == 1


def _block(body, name):
    """`const NAME = { ... }` 안쪽만 떼어낸다.

    이름이 겹치는 다른 표(KNUCKLE 등)를 잘못 집지 않도록 범위를 좁힌다.
    """
    import re

    # 중첩 중괄호가 없는 평평한 객체들이라 첫 `};` 까지가 그 블록이다.
    match = re.search(rf"const {name} = \{{(.*?)\}};", body, re.S)
    assert match, f"{name} 블록을 찾지 못했다"
    return match.group(1)


def _segment_lengths(body):
    import re

    block = _block(body, "SEGMENTS")
    return {
        finger: [int(n) for n in values.split(",")]
        for finger, values in re.findall(r"(\w+):\s*\[([\d,\s]+)\]", block)
    }


def test_mockhand_fingers_have_joints(client):
    """손가락 하나가 선 하나면 "기울어진 막대" 로 보인다. 마디로 나눈다."""
    import re

    body = client.get("/mockhand.html").text
    segments = _segment_lengths(body)

    assert set(segments) == {"index", "middle", "ring", "pinky"}
    for finger, lengths in segments.items():
        assert len(lengths) >= 2, f"{finger}: 마디가 하나뿐이다"

    joints = re.search(r"const JOINTS = \[([\d,\s]+)\]", body)
    assert joints
    assert len(joints.group(1).split(",")) == len(segments["index"]), (
        "마디 수와 관절 수가 맞아야 한다"
    )

    thumb = _block(body, "THUMB")
    assert re.search(r"segments:\s*\[[\d,\s]+\]", thumb), "엄지도 마디로 나눈다"


def test_mockhand_fingers_differ_in_size(client):
    """엄지가 굵고 소지가 가늘다. 중지가 길고 소지가 짧다."""
    import re

    body = client.get("/mockhand.html").text
    widths = dict(
        (k, int(v)) for k, v in re.findall(r"(\w+):\s*(\d+)", _block(body, "WIDTH"))
    )
    assert widths["thumb"] > widths["index"] > widths["pinky"]

    lengths = _segment_lengths(body)
    total = {finger: sum(values) for finger, values in lengths.items()}
    assert total["middle"] > total["index"] > total["pinky"]
    assert total["middle"] > total["ring"] > total["pinky"]


def test_mockhand_marks_neutral_and_resting_differently(client):
    """중립과 안전은 다른 것이므로 같은 표시를 쓰지 않는다 (R3)."""
    body = client.get("/mockhand.html").text
    assert "chip neutral" in body and "chip resting" in body
    # 칩 모양이 서로 다르다: 하나는 채우고 하나는 점선 테두리다.
    assert "border: 2px dashed" in body
    assert ".chip.neutral { background: var(--ink)" in body


def test_mockhand_shows_state_regardless_of_the_letter_toggle(client):
    """중립·안전은 정답이 아니라 경계·대기 신호다. 토글과 무관하게 표시한다."""
    body = client.get("/mockhand.html").text
    # 글자만 토글에 걸린다.
    assert "const isLetter =" in body
    assert "if (isLetter && !shown)" in body


def test_mockhand_has_a_preview_mode(client):
    """세션 없이 11자 + 중립 + 안전을 하나씩 확인할 수 있어야 한다."""
    body = client.get("/mockhand.html").text
    assert 'URLSearchParams(location.search).has("preview")' in body
    assert "ArrowRight" in body and "ArrowLeft" in body
# --------------------------------------------------------------------------- #
# 실패 이벤트는 세션이 살아 있는지를 말한다 (DECISIONS §50)
# 이 테스트는 완화하지 않는다 — 화면이 이어갈 길을 여기서 판단한다
# --------------------------------------------------------------------------- #

class CancelsOnceDevice(MockDevice):
    """첫 이동만 정지로 끊고, 그다음부터는 정상으로 움직인다.

    정지 직후 [다시 보기] 를 누르는 실기 경로를 그대로 만든다. 끊긴 이동은
    가짜 시계를 거의 밀지 않으므로, 곧바로 누른 다시 보기가 구동 요청 최소
    간격 안에 들어온다 — 실기에서 벌어진 그 순서다.
    """

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.cancelled = 0

    def move_to(self, pose, transition_ms, hold_ms):
        if self.cancelled == 0:
            self.cancelled += 1
            raise DeviceCancelledError("cancelled by a stop before RDY")
        return super().move_to(pose, transition_ms, hold_ms)


def test_a_replay_right_after_a_stop_is_rejected_but_recoverable(config, clock):
    """정지 직후의 [다시 보기] 는 최소 간격에 막힌다. **세션은 살아 있다.**

    실기에서 여기서 멈췄다. `POST /present` 는 202 를 돌려주는데 백그라운드가
    `RATE_LIMIT` 으로 거부되고, 화면은 그 거부에 이어갈 길을 내놓지 않아
    학습자에게 누를 것이 하나도 남지 않았다 — heartbeat 만 도는 상태다.
    화면이 [다시 보기] 를 남길 근거가 `session_alive` 다 (DECISIONS §50).
    """
    station = build_station(
        config_path=REAL_POSES,
        words_path=REAL_WORDS,
        database=Database.in_memory(),
        device=CancelsOnceDevice(config, sleep=clock.sleep),
        clock=clock,
        background=lambda work: work(),
    )
    events = []
    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]
        client.get(f"/api/sessions/{sid}/next")
        controller = station.controllers[sid]
        station.hub.publish = lambda session_id, event: events.append(event)

        # 이동 중에 정지가 걸렸다.
        client.post(f"/api/sessions/{sid}/present")
        # 곧바로 누른 다시 보기. 최소 간격 안이므로 거부된다.
        assert client.post(f"/api/sessions/{sid}/present").status_code == 202

        failures = [e for e in events if e["type"] == "error"]
        assert [e["code"] for e in failures] == ["STOPPED", "RATE_LIMIT"]
        # 두 갈래 모두 세션이 살아 있다고 말한다.
        assert [e["session_alive"] for e in failures] == [True, True]
        assert sid in station.controllers
        assert station.lock.holder() == sid
        # 문항은 아직 나가지 못했다.
        assert controller.state == "awaiting_present"

        # 간격을 지나 다시 누르면 같은 문항이 나간다. 여기가 막히면 세션이
        # 영영 멈춘 것이다.
        clock.advance(3.0)
        assert client.post(f"/api/sessions/{sid}/present").status_code == 202
        assert controller.state == "awaiting_answer"


def test_a_stop_says_the_session_is_still_alive(config, clock):
    station = breaking_station(
        config, clock, DeviceCancelledError("cancelled by a stop before RDY")
    )
    events = []
    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]
        client.get(f"/api/sessions/{sid}/next")
        station.hub.publish = lambda session_id, event: events.append(event)
        client.post(f"/api/sessions/{sid}/present")

    failures = [e for e in events if e["type"] == "error"]
    assert [e["code"] for e in failures] == ["STOPPED"]
    assert failures[0]["session_alive"] is True


def test_a_device_failure_says_the_session_is_gone(config, clock):
    """예상 못 한 실패는 세션을 닫는다. 화면은 이어갈 길을 내놓지 않아야 한다."""
    station = breaking_station(config, clock, DeviceProtocolError("boom"))
    events = []
    with TestClient(create_app(station)) as client:
        session = start(client)
        sid = session["session_id"]
        client.get(f"/api/sessions/{sid}/next")
        station.hub.publish = lambda session_id, event: events.append(event)
        client.post(f"/api/sessions/{sid}/present")

    failures = [e for e in events if e["type"] == "error"]
    assert [e["code"] for e in failures] == ["DEVICE_FAILED"]
    assert failures[0]["session_alive"] is False


# --------------------------------------------------------------------------- #
# 참여할 세션 조회 (DECISIONS §50)
# --------------------------------------------------------------------------- #

def test_nothing_is_open_before_a_session_starts(client):
    assert client.get("/api/sessions/open").json() == {
        "session_id": None,
        "lock_holder": None,
    }


def test_an_open_session_is_reported_without_touching_the_lock(client, station):
    session = start(client)
    sid = session["session_id"]

    body = client.get("/api/sessions/open").json()
    assert body["session_id"] == sid
    assert body["learner_name"] == "haru"
    assert body["total_items"] == session["total_items"]
    assert body["state"] == "awaiting_item"
    # 조회는 구동이 아니다. 락도 구동 예산도 건드리지 않는다.
    assert station.lock.holder() == sid
    assert station.gate.requests_for(sid) == 0


def test_a_finished_session_is_not_open(client, station):
    session = start(client)
    sid = session["session_id"]
    client.post(f"/api/sessions/{sid}/end")
    # 끝난 세션은 참여 대상이 아니고, 락도 반납되어 있다.
    assert client.get("/api/sessions/open").json() == {
        "session_id": None,
        "lock_holder": None,
    }


def test_a_session_this_server_does_not_know_is_not_open(client, station):
    """락만 남고 컨트롤러가 사라진 경우. 참여할 수 있다고 말하지 않는다."""
    session = start(client)
    sid = session["session_id"]
    station.controllers.pop(sid)
    # 참여는 못 한다. 락 상태는 따로 낸다 (아래 §51 테스트가 그것을 본다).
    assert client.get("/api/sessions/open").json()["session_id"] is None
# --------------------------------------------------------------------------- #
# 참여 조회는 락 상태도 낸다 (DECISIONS §51)
# --------------------------------------------------------------------------- #

def test_the_open_query_reports_the_lock_holder_separately(client, station):
    """"참여할 세션이 있다" 와 "손이 잡혀 있다" 는 다른 사실이다.

    화면은 `lock_holder` 로 강제 해제를 내놓을지 정한다. 잡힌 것이 없는데
    권하면 시킨 대로 눌러도 "잠긴 세션이 없습니다" 가 돌아온다.
    """
    # 아무것도 없을 때: 둘 다 비어 있다.
    body = client.get("/api/sessions/open").json()
    assert body == {"session_id": None, "lock_holder": None}

    session = start(client)
    sid = session["session_id"]
    body = client.get("/api/sessions/open").json()
    assert body["session_id"] == sid
    assert body["lock_holder"] == sid


def test_a_held_lock_is_reported_even_with_no_session_to_join(client, station):
    """이 서버가 모르는 세션이 락을 쥔 경우. 참여는 못 하지만 해제는 할 수 있다."""
    session = start(client)
    sid = session["session_id"]
    # 컨트롤러만 사라진 상태 — 락은 남아 있다.
    station.controllers.pop(sid)

    body = client.get("/api/sessions/open").json()
    assert body["session_id"] is None, "참여할 세션은 없다"
    assert body["lock_holder"] == sid, "그래도 손은 잡혀 있다"


def test_the_lock_is_free_after_a_reset(client, station):
    """해제 뒤에는 참여할 세션도, 잡힌 락도 없다."""
    start(client)
    client.post("/api/device/reset")
    assert client.get("/api/sessions/open").json() == {
        "session_id": None,
        "lock_holder": None,
    }


def test_a_request_to_a_reset_session_is_a_structured_409(client, station):
    """죽은 세션에 보낸 요청은 구조화된 409 다. 화면이 이것으로 세션을 버린다.

    실기 로그에서 같은 요청이 네 번 반복됐다 (§51). 몸통이 읽히지 않으면
    화면은 반복을 멈출 근거를 갖지 못한다.
    """
    session = start(client)
    sid = session["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    client.post("/api/device/reset")

    for path in (f"/api/sessions/{sid}/planned", f"/api/sessions/{sid}/heartbeat"):
        response = client.post(path)
        assert response.status_code == 409, (path, response.text)
        assert response.json()["code"], path


# --------------------------------------------------------------------------- #
# 학습자 진행 — 혼동 데이터를 싣지 않는다 (DECISIONS §52)
# --------------------------------------------------------------------------- #

def _answer_wrong_twice(client, station, sid):
    """같은 문항을 두 번 틀려 혼동 기록을 남긴다. 첫 글자만 다른 글자로 바꾼다."""
    for _ in range(2):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        key = station.controllers[sid]._active_answer()
        other = next(c for c in sorted(station.config.poses) if c != key[0])
        client.post(
            f"/api/sessions/{sid}/answer", json={"answer": other + key[1:]}
        )


def _all_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _all_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _all_keys(inner)


def _run_session_correctly(client, station):
    session = start(client)
    sid = session["session_id"]
    for _ in range(session["total_items"]):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        answer_correctly(client, station, sid)
    client.post(f"/api/sessions/{sid}/end")
    return session


def test_progress_carries_no_confusion_data(client, station):
    """혼동 기록이 **있는** 학습자로 확인한다. 기록이 없으면 칸이 빠져 있어도
    우연일 수 있다.

    필드를 걸러 내는 것이 아니라 처음부터 담지 않는다 — 어떤 깊이에도 혼동
    쌍·글자·최근 문항이 없어야 한다."""
    sid = start(client)["session_id"]
    _answer_wrong_twice(client, station, sid)
    # 응답이 행렬이 된 뒤로(§60) 혼동 기록은 대각선 밖 칸이다.
    assert any(
        not cell["diagonal"]
        for cell in client.get(
            "/api/stats/confusion", params={"track": TRACK_DISCRIMINATION}
        ).json()["cells"]
    ), "혼동 기록이 남지 않아 이 테스트가 아무것도 보지 않는다"

    body = client.get("/api/learners/haru/progress").json()
    keys = set(_all_keys(body))
    for forbidden in (
        "confusions", "confusions_by_track", "pairs", "pair", "weakest_pairs",
        "letters", "recent_items", "expected", "given", "answer",
        "accuracy_by_hold_ms",
    ):
        assert forbidden not in keys, forbidden


def test_progress_numbers_sessions_within_each_track(client, station):
    """공부 세션은 번호를 먹지 않는다. 첫 훈련은 세션 id 가 무엇이든 "1번째" 다 (§60)."""
    study = client.post("/api/study-sessions", json={"learner_name": "haru"}).json()
    client.post(f"/api/study-sessions/{study['session_id']}/end")
    session = _run_session_correctly(client, station)
    assert session["session_id"] > study["session_id"]

    body = client.get("/api/learners/haru/progress").json()
    for track in (TRACK_DISCRIMINATION, TRACK_WORD):
        points = body["by_track"][track]["sessions"]
        assert [p["ordinal"] for p in points] == [1], track
        assert points[0]["session_id"] == session["session_id"]


def test_progress_reports_each_track_separately(client, station):
    session = _run_session_correctly(client, station)

    body = client.get("/api/learners/haru/progress").json()
    assert body["sessions"] == 1
    assert set(body["by_track"]) == {TRACK_DISCRIMINATION, TRACK_WORD}
    disc = body["by_track"][TRACK_DISCRIMINATION]
    word = body["by_track"][TRACK_WORD]
    assert (disc["answered"], word["answered"]) == (2, 1)
    assert [p["answered"] for p in disc["sessions"]] == [2]
    assert [p["answered"] for p in word["sessions"]] == [1]
    assert disc["sessions"][0]["session_id"] == session["session_id"]
    # 두 트랙을 더한 숫자를 두지 않는다.
    for forbidden in ("answered", "accuracy", "total_items", "overall"):
        assert forbidden not in body, forbidden


def test_progress_difficulty_is_where_the_next_session_starts(client, station):
    """화면의 "지금 난이도" 는 세션이 복원하는 값과 **같은 함수**에서 나온다."""
    from server.session import starting_difficulty

    fresh = client.get("/api/learners/haru/progress").json()
    for track in (TRACK_DISCRIMINATION, TRACK_WORD):
        assert fresh["by_track"][track]["difficulty"]["carried_over"] is False

    session = _run_session_correctly(client, station)

    body = client.get("/api/learners/haru/progress").json()
    with station.database.session() as db:
        for track in (TRACK_DISCRIMINATION, TRACK_WORD):
            expected = starting_difficulty(
                db, station.config, session["learner_id"], track
            )
            shown = body["by_track"][track]["difficulty"]
            assert shown["carried_over"] is True
            assert (shown["length"], shown["hold_ms"], shown["boundary"]) == (
                expected.length, expected.hold_ms, expected.boundary
            )


def test_progress_reads_the_unlock_condition_from_config(client, station):
    """해금 조건은 `poses.json` 에서 온다. 화면은 그 수치를 모른다."""
    unlock = station.config.curriculum["depth_pairs_unlock"]
    depth = client.get("/api/learners/haru/progress").json()["depth_pairs"]
    assert depth["track"] == TRACK_DISCRIMINATION
    assert depth["window_items"] == unlock["window_items"]
    assert depth["min_accuracy"] == unlock["min_accuracy"]
    assert depth["unlocked"] is False
    assert depth["items_remaining"] == unlock["window_items"]


def test_progress_of_an_unknown_name_does_not_count_other_learners(client, station):
    """기록 없는 이름은 0 이다. **남의 기록이 세지면 안 된다.**

    집계 함수는 `learner_id=None` 을 "범위 없음" 으로 읽는다. 없는 이름을
    그대로 넘기면 모든 학습자의 문항이 그 이름으로 잡힌다."""
    _run_session_correctly(client, station)

    response = client.get("/api/learners/nobody/progress")
    assert response.status_code == 200
    body = response.json()
    assert body["learner_id"] is None
    assert body["sessions"] == 0
    for track in (TRACK_DISCRIMINATION, TRACK_WORD):
        assert body["by_track"][track]["answered"] == 0
        assert body["by_track"][track]["sessions"] == []
    assert body["depth_pairs"]["answered"] == 0
    # 조회가 학습자를 만들지 않는다.
    assert client.get("/api/learners").json()["learners"] == ["haru"]


# --------------------------------------------------------------------------- #
# 없는 이름의 학습자 상세 — 남의 기록을 세지 않는다 (DECISIONS §53)
# --------------------------------------------------------------------------- #

def test_stats_of_an_unknown_name_do_not_aggregate_everyone(client, station):
    """**다른 학습자의 기록이 있는 상태**에서 본다. 기록이 비어 있으면 필터가
    빠져도 0 이 나와 이 결함이 보이지 않는다 — 예전 테스트가 그랬다.

    혼동·유지 시간·최근 문항까지 전부 비어야 한다. 요약 숫자만 막고 목록을
    놓치면 남의 글자열이 이 이름으로 나간다."""
    sid = start(client)["session_id"]
    _answer_wrong_twice(client, station, sid)
    answer_correctly(client, station, sid)
    haru = client.get("/api/learners/haru/stats").json()
    assert haru["by_track"][TRACK_DISCRIMINATION]["answered"] > 0
    assert haru["by_track"][TRACK_DISCRIMINATION]["confusions"]

    response = client.get("/api/learners/nobody/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["learner_id"] is None
    assert body["depth_pairs_unlocked"] is False
    for track in (TRACK_DISCRIMINATION, TRACK_WORD):
        stats = body["by_track"][track]
        assert stats["answered"] == 0
        assert stats["accuracy"] is None
        assert stats["avg_response_ms"] is None
        assert stats["confusions"] == []
        assert stats["accuracy_by_hold_ms"] == []
        assert stats["recent_items"] == []
    # 조회가 학습자를 만들지 않는다.
    assert client.get("/api/learners").json()["learners"] == ["haru"]


def test_stats_of_a_known_name_are_still_scoped_to_that_learner(client, station):
    """고친 뒤에도 있는 이름은 그 학습자만 센다. 다른 학습자가 섞이지 않는다."""
    sid = start(client)["session_id"]
    answer_count = 0
    for _ in range(2):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        answer_correctly(client, station, sid)
        answer_count += 1
    client.post("/api/device/reset")

    other = start(client, learner_name="mina")["session_id"]
    client.get(f"/api/sessions/{other}/next")
    client.post(f"/api/sessions/{other}/present")
    answer_correctly(client, station, other)

    haru = client.get("/api/learners/haru/stats").json()
    mina = client.get("/api/learners/mina/stats").json()
    assert haru["by_track"][TRACK_DISCRIMINATION]["answered"] == answer_count
    assert mina["by_track"][TRACK_DISCRIMINATION]["answered"] == 1


def test_item_bank_for_an_unknown_name_ignores_other_learners_history(client, station):
    """이름을 줬는데 없는 이름이면 "그 학습자, 기록 없음" 이다 (DECISIONS §54).

    예전에는 이름을 안 준 경우와 같은 길로 떨어져 **모든 학습자의** 최근
    출제를 피했고, 그 목록을 `excluded_recent` 로 응답에 그대로 실어 보냈다 —
    남의 최근 문항이 새 학습자의 후보 선택을 움직이고, 호출자에게 보이기까지
    했다. 다른 학습자의 기록을 먼저 심고 본다."""
    sid = start(client)["session_id"]
    for _ in range(2):
        client.get(f"/api/sessions/{sid}/next")
        client.post(f"/api/sessions/{sid}/present")
        answer_correctly(client, station, sid)
    params = {"track": TRACK_DISCRIMINATION, "length": 3, "count": 5}
    haru = client.get("/api/item-bank", params={**params, "learner_name": "haru"})
    assert haru.json()["excluded_recent"], "심은 기록이 최근 출제로 안 잡혀 이 테스트가 아무것도 보지 않는다"

    body = client.get(
        "/api/item-bank", params={**params, "learner_name": "nobody"}
    ).json()
    assert body["excluded_recent"] == []
    assert body["depth_pairs_unlocked"] is False


def test_item_bank_without_a_name_still_avoids_everyones_recent_items(client, station):
    """이름을 **주지 않은** 경우는 §46 의 결정 그대로다 — 학습자를 모르면
    누구에게든 최근에 나간 것을 피한다. 이번 수정은 이 경로를 바꾸지 않는다."""
    sid = start(client)["session_id"]
    client.get(f"/api/sessions/{sid}/next")
    client.post(f"/api/sessions/{sid}/present")
    answer_correctly(client, station, sid)

    body = client.get(
        "/api/item-bank",
        params={"track": TRACK_DISCRIMINATION, "length": 3, "count": 5},
    ).json()
    assert body["excluded_recent"]
