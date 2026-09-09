"""REST 라우트와 WebSocket 테스트 (PRD 11장).

디바이스 동작은 **인라인**으로 돌린다. 실제 서버는 백그라운드 스레드를 쓰지만
(`_thread_background`), 테스트는 `background` 를 주입해 순서를 확정한다.
시간도 주입한다 — 그러지 않으면 rate limit 이 실제 운용과 다르게 걸린다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from server.db import TRACK_DISCRIMINATION, TRACK_WORD, Database
from server.device import DeviceAdapter
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


def test_the_index_redirects_to_practice(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/practice.html"


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

def test_practice_page_is_served(client):
    response = client.get("/practice.html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_page_has_no_build_step_and_no_cdn(client):
    """빌드 도구도 프레임워크도 CDN 도 쓰지 않는다. 단일 파일이다."""
    body = client.get("/practice.html").text
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
    body = client.get("/practice.html").text
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
    body = client.get("/practice.html").text
    assert "by_track" in body
    assert "confusions_by_track" in body
    for forbidden in ("overall", "total_accuracy", "combined"):
        assert forbidden not in body.lower(), forbidden


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


def test_practice_does_not_link_to_mockhand(client):
    """제품 기능이 아니다. 학습자 뷰에 노출하지 않는다 (PRD 6.3)."""
    assert "mockhand" not in client.get("/practice.html").text
