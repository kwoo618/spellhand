"""시리얼 어댑터 테스트 (PRD 6장, 7장).

**실제 포트를 열지 않는다.** 펌웨어 응답을 흉내내는 가짜 전송로를 끼워
어댑터 전체를 검증한다. 실기는 전기팀 입회가 필요하다.

시간은 주입한다. 가짜 전송로가 읽을 때마다 시계를 밀어, 타임아웃이 실제
운용과 같은 조건에서 걸리게 한다.
"""

import os

import pytest

from server.calibration import calibration_hash
from server.device import (
    CalibrationMismatchError,
    DeviceBusyError,
    DeviceCancelledError,
    DeviceConfigError,
    DeviceNotConnectedError,
    DeviceProtocolError,
    DeviceRangeError,
    DeviceSequenceError,
    DeviceTimeoutError,
    DeviceTimingError,
    Pose,
    create_device,
)
from server.device.mock import MockDevice
from server.device.serial import NO_SEQ, SerialDevice

LETTERS = ("B", "D", "E", "F", "I", "L", "O", "S", "V", "W", "Y")


class FakePort:
    """펌웨어를 흉내내는 전송로.

    `script` 는 "쓰기 한 번에 대해 돌려줄 줄들"의 목록이다. 비어 있으면
    아무것도 돌려주지 않아 타임아웃을 만든다.
    """

    def __init__(self, clock, *, hash_value, script=None, read_tick_s=0.05):
        self.clock = clock
        self.hash_value = hash_value
        self.script = list(script or [])
        self.read_tick_s = read_tick_s
        self.written = []
        self.pending = []
        self.closed = False

    # -- 전송로 계약 -------------------------------------------------- #

    def write(self, data):
        text = data.decode("ascii").strip()
        self.written.append(text)
        self._respond(text)
        return len(data)

    def readline(self):
        if self.pending:
            return (self.pending.pop(0) + "\n").encode("ascii")
        # 읽을 것이 없으면 시간만 흐른다. 어댑터의 데드라인이 이것으로 지난다.
        self.clock.advance(self.read_tick_s)
        return b""

    def close(self):
        self.closed = True

    # -- 펌웨어 흉내 --------------------------------------------------- #

    def _respond(self, command):
        head = command.split()[0] if command.split() else ""
        if head == "H":
            self.pending.append(f"HSH {self.hash_value}")
            return
        if self.script:
            self.pending.extend(self.script.pop(0))
            return
        # 대본이 없으면 정상 응답을 만들어 준다.
        seq = _seq_of(command)
        if head == "P":
            self.pending.extend([f"ACK {seq}", f"RDY {seq}", f"DON {seq}"])
        elif head in ("Z", "N"):
            self.pending.extend(
                [f"ACK {NO_SEQ}", f"RDY {NO_SEQ}", f"DON {NO_SEQ}"]
            )

    @property
    def drive_commands(self):
        """구동 명령만. 해시 조회는 구동이 아니다."""
        return [c for c in self.written if c.split()[:1] != ["H"]]


def _seq_of(command):
    parts = command.split()
    return parts[1] if len(parts) > 1 else str(NO_SEQ)


@pytest.fixture
def port(clock, config):
    return FakePort(clock, hash_value=calibration_hash(config))


def build(config, clock, port, **kwargs):
    return SerialDevice(
        config,
        port="COM-TEST",
        transport=port,
        clock=clock,
        sleep=clock.sleep,
        seq_source=lambda: 1000,
        **kwargs,
    )


@pytest.fixture
def device(config, clock, port):
    dev = build(config, clock, port)
    dev.connect()
    return dev


def pose_of(config, letter):
    return Pose.from_config(config, config.poses[letter])


# --------------------------------------------------------------------------- #
# 해시 핸드셰이크 (R4 — 이 테스트는 완화하지 않는다)
# --------------------------------------------------------------------------- #

def test_matching_hash_connects(config, clock, port):
    dev = build(config, clock, port)
    dev.connect()
    assert dev.is_connected
    assert port.written[0] == "H"


def test_mismatched_hash_refuses_to_connect(config, clock):
    """펌웨어 재업로드를 잊은 상태다. 연결 자체를 거부한다."""
    port = FakePort(clock, hash_value="deadbeef")
    dev = build(config, clock, port)

    with pytest.raises(CalibrationMismatchError) as exc:
        dev.connect()

    assert dev.is_connected is False
    assert "deadbeef" in str(exc.value)
    assert calibration_hash(config) in str(exc.value)
    assert "gen_calibration.py" in str(exc.value)


def test_mismatched_hash_sends_no_drive_command(config, clock):
    """거부된 뒤에는 어떤 구동 명령도 나가지 않는다."""
    port = FakePort(clock, hash_value="deadbeef")
    dev = build(config, clock, port)

    with pytest.raises(CalibrationMismatchError):
        dev.connect()

    assert port.drive_commands == []
    assert port.closed is True


def test_a_refused_device_cannot_be_driven(config, clock):
    port = FakePort(clock, hash_value="deadbeef")
    dev = build(config, clock, port)
    with pytest.raises(CalibrationMismatchError):
        dev.connect()

    with pytest.raises(DeviceNotConnectedError):
        dev.move_to(pose_of(config, "B"), 900, 2000)
    with pytest.raises(DeviceNotConnectedError):
        dev.neutral()
    with pytest.raises(DeviceNotConnectedError):
        dev.safe()

    assert port.drive_commands == []


def test_there_is_no_way_to_skip_the_handshake():
    """검증을 건너뛰는 옵션을 만들지 않는다."""
    import inspect

    signature = inspect.signature(SerialDevice.__init__)
    for forbidden in ("skip_hash", "check_hash", "verify", "force", "unsafe"):
        assert forbidden not in signature.parameters


def test_a_silent_board_times_out_on_the_handshake(config, clock):
    port = FakePort(clock, hash_value="x")
    port._respond = lambda command: None  # 아무 응답도 하지 않는 보드
    dev = build(config, clock, port)

    with pytest.raises(DeviceTimeoutError) as exc:
        dev.connect()
    assert "HSH" in str(exc.value)


# --------------------------------------------------------------------------- #
# 전송 형식
# --------------------------------------------------------------------------- #

def test_move_sends_the_exact_protocol_line(device, port, config):
    device.move_to(pose_of(config, "I"), 900, 2000)

    # I = thumb .70 index .83 middle .80 ring .76 pinky .00
    assert port.drive_commands == ["P 1001 700 830 800 760 0 900 2000"]


def test_permille_conversion_matches_the_config(device, port, config):
    device.move_to(pose_of(config, "B"), 900, 3000)
    sent = port.drive_commands[0].split()

    expected = [
        str(round(config.poses["B"][finger] * 1000)) for finger in config.fingers
    ]
    assert sent[2:7] == expected
    assert sent[2] == "700"  # flex 0.70 -> 700


def test_finger_order_follows_the_config(device, port, config):
    device.move_to(pose_of(config, "W"), 900, 2000)
    values = port.drive_commands[0].split()[2:7]
    assert values == [
        str(round(config.poses["W"][finger] * 1000)) for finger in config.fingers
    ]
    assert config.fingers == ("thumb", "index", "middle", "ring", "pinky")


def test_neutral_and_safe_use_different_commands(device, port, config):
    """중립과 안전은 별개다 (R3). 명령 자체가 다르다."""
    device.neutral()
    device.safe()
    assert port.drive_commands == ["N", "Z"]


def test_safe_holds_for_the_configured_settle_time(device, clock, config):
    """펌웨어의 Z 는 유지하지 않는다. 유지는 어댑터가 기다려 만든다."""
    before = clock()
    device.safe()
    waited = (clock() - before).total_seconds() * 1000
    assert waited >= config.timing.hold_min_ms


def test_stop_does_not_wait(device, clock, config):
    before = clock()
    device.stop()
    waited = (clock() - before).total_seconds() * 1000
    assert waited < config.timing.hold_min_ms


def test_move_rejects_a_letter_name(device):
    """R2: 어댑터에 글자 이름을 넘길 수 없다."""
    with pytest.raises(TypeError) as exc:
        device.move_to("B", 900, 2000)
    assert "Pose" in str(exc.value)


def test_no_letter_name_ever_reaches_the_wire(config, clock, port):
    """R2 회귀 고정: 전송된 바이트에 글자 이름이 등장하지 않는다."""
    dev = build(config, clock, port)
    dev.connect()
    for letter in LETTERS:
        dev.move_to(pose_of(config, letter), 900, 2000)
    dev.neutral()
    dev.safe()

    wire = "\n".join(port.written)
    tokens = set(wire.replace("\n", " ").split())
    for letter in LETTERS:
        assert letter not in tokens, f"{letter} reached the wire: {wire}"
    # 남는 것은 명령 글자와 숫자뿐이다.
    assert tokens <= {"P", "H", "N", "Z"} | {t for t in tokens if _is_int(t)}


def _is_int(text):
    try:
        int(text)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# seq
# --------------------------------------------------------------------------- #

def test_seq_increases_with_every_move(device, port, config):
    for _ in range(3):
        device.move_to(pose_of(config, "B"), 900, 2000)
    seqs = [int(c.split()[1]) for c in port.drive_commands]
    assert seqs == [1001, 1002, 1003]


def test_seq_starts_from_the_injected_source(config, clock, port):
    """에포크 초에서 시작해 재연결·재시작에도 항상 커진다 (DECISIONS.md §37)."""
    dev = SerialDevice(
        config,
        port="COM-TEST",
        transport=port,
        clock=clock,
        sleep=clock.sleep,
        seq_source=lambda: 1789012345,
    )
    dev.connect()
    dev.move_to(pose_of(config, "B"), 900, 2000)
    assert port.drive_commands[0].split()[1] == "1789012346"


def test_the_default_seq_source_is_epoch_based(config):
    """재연결 시 펌웨어의 lastSeq 보다 항상 큰 값이 나가야 한다."""
    import time

    dev = SerialDevice(config, port="COM-TEST")
    assert dev._seq_source() >= int(time.time()) - 1


def test_a_mismatched_response_seq_is_an_error(config, clock):
    port = FakePort(
        clock,
        hash_value=calibration_hash(config),
        script=[["ACK 9999", "RDY 9999", "DON 9999"]],
    )
    dev = build(config, clock, port)
    dev.connect()

    with pytest.raises(DeviceProtocolError) as exc:
        dev.move_to(pose_of(config, "B"), 900, 2000)
    assert "9999" in str(exc.value)
    assert "1001" in str(exc.value)


def test_an_out_of_order_response_is_an_error(config, clock):
    """RDY 를 기다리는데 DON 이 오면 프로토콜이 어긋난 것이다."""
    port = FakePort(
        clock,
        hash_value=calibration_hash(config),
        script=[["ACK 1001", "DON 1001"]],
    )
    dev = build(config, clock, port)
    dev.connect()

    with pytest.raises(DeviceProtocolError):
        dev.move_to(pose_of(config, "B"), 900, 2000)


# --------------------------------------------------------------------------- #
# 도달 시각 (RDY)
# --------------------------------------------------------------------------- #

def test_arrival_time_is_the_rdy_moment(device, clock, config):
    """RDY 를 받은 시각이 관측 시작 시점이다. 역산하지 않는다."""
    assert device.last_arrival_at() is None

    before = clock()
    device.move_to(pose_of(config, "B"), 900, 3000)
    arrived = device.last_arrival_at()

    assert arrived is not None
    assert before <= arrived <= clock()
    # DON 은 그 뒤에 온다. 도달 시각이 완료 시각보다 앞이어야 한다.
    assert arrived <= clock()


def test_arrival_time_updates_every_move(device, clock, config):
    device.move_to(pose_of(config, "B"), 900, 2000)
    first = device.last_arrival_at()
    clock.advance(5)
    device.move_to(pose_of(config, "W"), 900, 2000)
    assert device.last_arrival_at() > first


def test_the_mock_does_not_report_arrival(config, clock):
    """Mock 은 도달 시점을 모른다. 상위 계층이 역산한다 (DECISIONS.md §36)."""
    mock = MockDevice(config, sleep=clock.sleep)
    mock.connect()
    mock.move_to(pose_of(config, "B"), 900, 2000)
    assert mock.last_arrival_at() is None


# --------------------------------------------------------------------------- #
# 펌웨어 거부
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "code, expected",
    [
        ("EPARSE", DeviceProtocolError),
        ("ERANGE", DeviceRangeError),
        ("ETIME", DeviceTimingError),
        ("EBUSY", DeviceBusyError),
        ("ESEQ", DeviceSequenceError),
    ],
)
def test_each_firmware_code_maps_to_its_own_exception(config, clock, code, expected):
    port = FakePort(
        clock, hash_value=calibration_hash(config), script=[[f"ERR 1001 {code}"]]
    )
    dev = build(config, clock, port)
    dev.connect()

    with pytest.raises(expected) as exc:
        dev.move_to(pose_of(config, "B"), 900, 2000)
    assert code in str(exc.value)


def test_an_unknown_firmware_code_is_a_protocol_error(config, clock):
    port = FakePort(
        clock, hash_value=calibration_hash(config), script=[["ERR 1001 EWAT"]]
    )
    dev = build(config, clock, port)
    dev.connect()
    with pytest.raises(DeviceProtocolError):
        dev.move_to(pose_of(config, "B"), 900, 2000)


# --------------------------------------------------------------------------- #
# 타임아웃과 단절
# --------------------------------------------------------------------------- #

def test_a_missing_don_times_out_and_sends_z(config, clock):
    """응답이 끊기면 손 상태를 모른다. Z 로 안전 자세까지 내린다."""
    port = FakePort(
        clock, hash_value=calibration_hash(config), script=[["ACK 1001", "RDY 1001"]]
    )
    dev = build(config, clock, port)
    dev.connect()

    with pytest.raises(DeviceTimeoutError) as exc:
        dev.move_to(pose_of(config, "B"), 900, 2000)

    assert "DON" in str(exc.value)
    assert port.drive_commands[-1] == "Z"


def test_a_missing_ack_times_out_and_sends_z(config, clock):
    port = FakePort(clock, hash_value=calibration_hash(config), script=[[]])
    dev = build(config, clock, port)
    dev.connect()

    with pytest.raises(DeviceTimeoutError) as exc:
        dev.move_to(pose_of(config, "B"), 900, 2000)
    assert "ACK" in str(exc.value)
    assert port.drive_commands[-1] == "Z"


def test_the_device_is_not_busy_after_a_timeout(config, clock):
    port = FakePort(clock, hash_value=calibration_hash(config), script=[[]])
    dev = build(config, clock, port)
    dev.connect()
    with pytest.raises(DeviceTimeoutError):
        dev.move_to(pose_of(config, "B"), 900, 2000)
    assert dev.is_busy is False


def test_a_vanished_port_becomes_not_connected(config, clock, port):
    dev = build(config, clock, port)
    dev.connect()

    def explode(data):
        raise OSError("port went away")

    port.write = explode
    with pytest.raises(DeviceNotConnectedError):
        dev.move_to(pose_of(config, "B"), 900, 2000)
    assert dev.is_connected is False


def test_driving_before_connect_is_rejected(config, clock, port):
    dev = build(config, clock, port)
    with pytest.raises(DeviceNotConnectedError):
        dev.move_to(pose_of(config, "B"), 900, 2000)
    assert port.drive_commands == []


def test_disconnect_closes_the_port(device, port):
    device.disconnect()
    assert port.closed is True
    assert device.is_connected is False


# --------------------------------------------------------------------------- #
# 어댑터 선택
# --------------------------------------------------------------------------- #

def test_the_default_mode_is_mock(config, monkeypatch):
    monkeypatch.delenv("DEVICE_MODE", raising=False)
    assert isinstance(create_device(config), MockDevice)


def test_an_unknown_mode_falls_back_to_mock(config, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("DEVICE_MODE", "seriel")
    with caplog.at_level(logging.WARNING, logger="server.device"):
        device = create_device(config)
    assert isinstance(device, MockDevice)
    assert any("seriel" in r.getMessage() for r in caplog.records), (
        "오타로 Mock 이 뜬 것을 손 고장으로 오해하지 않도록 경고를 남긴다"
    )


def test_serial_mode_builds_a_serial_device(config, monkeypatch):
    monkeypatch.setenv("DEVICE_MODE", "serial")
    monkeypatch.setenv("SERIAL_PORT", "COM3")
    device = create_device(config)
    assert isinstance(device, SerialDevice)
    assert device.is_connected is False, "만들기만 하고 열지 않는다"


def test_serial_mode_without_a_port_is_an_error(config, monkeypatch):
    monkeypatch.setenv("DEVICE_MODE", "serial")
    monkeypatch.delenv("SERIAL_PORT", raising=False)
    with pytest.raises(DeviceConfigError) as exc:
        create_device(config)
    assert "SERIAL_PORT" in str(exc.value)


def test_a_blank_serial_port_is_an_error(config, monkeypatch):
    monkeypatch.setenv("DEVICE_MODE", "serial")
    monkeypatch.setenv("SERIAL_PORT", "   ")
    with pytest.raises(DeviceConfigError):
        create_device(config)


def test_the_mode_can_be_passed_explicitly(config, monkeypatch):
    monkeypatch.setenv("DEVICE_MODE", "serial")
    assert isinstance(create_device(config, mode="mock"), MockDevice)


def test_selection_lives_in_one_place():
    """라우트가 따로 판단하지 않는다. 두 곳에 두면 한쪽만 고치게 된다."""
    import pathlib

    routes = pathlib.Path("server/routes/__init__.py").read_text(encoding="utf-8")
    assert "MockDevice(" not in routes
    assert "SerialDevice(" not in routes
    assert "create_device(" in routes


# --------------------------------------------------------------------------- #
# 비상 정지 (R4 — 이 테스트는 완화하지 않는다)
# --------------------------------------------------------------------------- #

def test_stop_sends_z_while_a_move_is_running(config, clock):
    """**구동 중에 정지가 실제로 나가야 한다.**

    실기 첫 구동에서 잡힌 결함이다. `stop()` 이 `_run` 을 거치면 busy 검사에
    걸려 `Z` 를 한 바이트도 쓰지 못했다 — 정지가 필요한 바로 그 순간에
    정지가 나가지 않았다 (DECISIONS.md §42).

    Mock 은 busy 를 보지 않아 이 결함이 Mock 테스트에 잡히지 않았다.
    """
    # P 에 ACK 만 돌려주고 RDY 를 주지 않는다. 그 대기 중에 stop() 을 부른다.
    port = FakePort(
        clock, hash_value=calibration_hash(config), script=[["ACK 1001"]]
    )
    device = build(config, clock, port)
    device.connect()

    stopped = []
    original_readline = port.readline

    def interrupt():
        if not stopped and device.is_busy:
            stopped.append(True)
            device.stop()
        return original_readline()

    port.readline = interrupt

    with pytest.raises(Exception):
        # 취소된 이동은 완료로 끝나지 않는다. 어떤 예외인지는 §42 참조.
        device.move_to(pose_of(config, "B"), 900, 2000)

    assert stopped, "이동 중에 stop() 이 호출되어야 하는 시나리오다"
    assert "Z" in port.drive_commands, (
        "구동 중 stop() 이 Z 를 보내지 않았다 — 비상 정지가 막혔다"
    )


def test_stop_does_not_need_the_device_to_be_idle(config, clock):
    """busy 여도 거부하지 않는다. 거부하면 비상 정지가 아니다."""
    port = FakePort(clock, hash_value=calibration_hash(config))
    device = build(config, clock, port)
    device.connect()

    device._busy = True  # 다른 스레드가 이동 중인 상태를 흉내낸다
    device.stop()

    assert port.drive_commands == ["Z"]


def test_stop_while_idle_clears_its_own_response(config, clock):
    """정지 응답을 비우지 않으면 다음 명령의 seq 가 어긋난다."""
    port = FakePort(clock, hash_value=calibration_hash(config))
    device = build(config, clock, port)
    device.connect()

    device.stop()
    device.move_to(pose_of(config, "B"), 900, 2000)  # 남은 줄이 있으면 여기서 깨진다

    assert port.drive_commands[0] == "Z"
    assert port.drive_commands[1].startswith("P 1001 ")


# --------------------------------------------------------------------------- #
# 응답 찌꺼기 (DECISIONS.md §42)
# --------------------------------------------------------------------------- #

def test_a_stop_during_a_move_raises_cancelled_not_a_protocol_error(config, clock):
    """취소는 통신 오류가 아니다. 프로토콜은 멀쩡하고 사람이 멈춘 것이다."""
    port = FakePort(
        clock, hash_value=calibration_hash(config), script=[["ACK 1001"]]
    )
    device = build(config, clock, port)
    device.connect()

    original_readline = port.readline
    stopped = []

    def interrupt():
        if not stopped and device.is_busy:
            stopped.append(True)
            device.stop()
        return original_readline()

    port.readline = interrupt

    with pytest.raises(DeviceCancelledError):
        device.move_to(pose_of(config, "B"), 900, 2000)
    assert not isinstance(
        DeviceCancelledError("x"), DeviceProtocolError
    ), "취소를 프로토콜 오류로 분류하면 상위 계층이 둘을 구분할 수 없다"


def test_a_cancelled_move_drains_the_stop_response(config, clock):
    """취소된 이동이 `Z` 의 응답을 마저 비운다.

    비우지 않으면 다음 `P` 가 `ACK -1` 을 자기 ACK 자리에서 읽는다. 실기
    세션에서 실제로 이렇게 깨졌다 (DECISIONS.md §42).
    """
    port = FakePort(
        clock, hash_value=calibration_hash(config), script=[["ACK 1001"]]
    )
    device = build(config, clock, port)
    device.connect()

    original_readline = port.readline
    stopped = []

    def interrupt():
        if not stopped and device.is_busy:
            stopped.append(True)
            device.stop()
        return original_readline()

    port.readline = interrupt
    with pytest.raises(DeviceCancelledError):
        device.move_to(pose_of(config, "B"), 900, 2000)

    port.readline = original_readline
    assert port.pending == [], "Z 의 응답이 포트에 남아 있다"

    # 그리고 다음 명령이 자기 seq 로 정상 진행한다.
    device.move_to(pose_of(config, "W"), 900, 2000)
    assert port.drive_commands[-1].startswith("P 1002 ")


def test_a_leftover_minus_one_response_is_discarded_with_a_warning(
    config, clock, caplog
):
    """비우지 못한 `-1` 이 남아 있어도 다음 명령이 깨지지 않는다.

    **조용히 넘기지는 않는다.** 찌꺼기가 남았다는 것 자체가 어딘가 잘못됐다는
    신호이므로 로그를 남긴다.
    """
    port = FakePort(
        clock,
        hash_value=calibration_hash(config),
        # 앞선 Z 의 응답이 통째로 남은 채로 P 를 보낸 상황.
        script=[
            [
                f"ACK {NO_SEQ}",
                f"RDY {NO_SEQ}",
                f"DON {NO_SEQ}",
                "ACK 1001",
                "RDY 1001",
                "DON 1001",
            ]
        ],
    )
    device = build(config, clock, port)
    device.connect()

    with caplog.at_level("WARNING", logger="server.device.serial"):
        device.move_to(pose_of(config, "B"), 900, 2000)

    discarded = [r for r in caplog.records if "stray" in r.getMessage()]
    assert len(discarded) == 3, "버린 줄마다 로그가 남아야 한다"
    assert "a previous stop left it unread" in discarded[0].getMessage()


def test_a_minus_one_response_does_not_satisfy_a_real_seq(config, clock):
    """`RDY -1` 을 `P` 의 RDY 로 받아들이면 안 된다.

    태그만 보면 맞지만 seq 가 다르다. 받아들이면 도달하지 않은 손을 도달한
    것으로 치고 관찰 시간을 재기 시작한다.
    """
    port = FakePort(
        clock,
        hash_value=calibration_hash(config),
        script=[["ACK 1001", f"RDY {NO_SEQ}"]],
    )
    device = build(config, clock, port)
    device.connect()

    with pytest.raises(DeviceTimeoutError):
        # -1 은 버려지고, 진짜 RDY 는 오지 않아 타임아웃으로 끝난다.
        device.move_to(pose_of(config, "B"), 900, 2000)
    assert device.last_arrival_at() is None


def test_a_wrong_real_seq_is_still_an_error(config, clock):
    """`-1` 만 예외다. 다른 seq 는 여전히 프로토콜 오류다 (완화하지 않는다)."""
    port = FakePort(
        clock, hash_value=calibration_hash(config), script=[["ACK 999"]]
    )
    device = build(config, clock, port)
    device.connect()

    with pytest.raises(DeviceProtocolError) as exc:
        device.move_to(pose_of(config, "B"), 900, 2000)
    assert "reported seq 999" in str(exc.value)


def test_stop_is_refused_only_when_disconnected(config, clock, port):
    device = build(config, clock, port)
    with pytest.raises(DeviceNotConnectedError):
        device.stop()
    assert port.drive_commands == []


# --------------------------------------------------------------------------- #
# .env 로딩과 모드 결정
# --------------------------------------------------------------------------- #

def write_env(tmp_path, **values):
    """임시 .env 를 만든다. 실제 .env 는 건드리지 않는다."""
    path = tmp_path / ".env"
    path.write_text(
        "".join(f"{k}={v}{chr(10)}" for k, v in values.items()), encoding="utf-8"
    )
    return path


@pytest.fixture
def clean_env():
    """관련 환경변수를 비우고, 끝나면 원래대로 되돌린다.

    `load_env` 는 `os.environ` 을 직접 고치므로 monkeypatch 가 되돌리지
    못한다. 되돌리지 않으면 이 테스트가 다른 테스트에 값을 흘린다.
    """
    keys = ("DEVICE_MODE", "SERIAL_PORT", "SERIAL_BAUD", "ANTHROPIC_API_KEY")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


def test_env_file_is_loaded(tmp_path, clean_env):
    from server.env import load_env

    path = write_env(tmp_path, DEVICE_MODE="serial", SERIAL_PORT="COM9")
    result = load_env(path)

    assert os.environ["DEVICE_MODE"] == "serial"
    assert os.environ["SERIAL_PORT"] == "COM9"
    assert set(result.applied) == {"DEVICE_MODE", "SERIAL_PORT"}
    assert result.found is True


def test_an_explicit_variable_wins_over_the_file(tmp_path, clean_env):
    """셸에서 준 값이 파일보다 우선한다. 아니면 지정한 것과 다른 것이 뜬다."""
    from server.env import load_env

    os.environ["DEVICE_MODE"] = "mock"
    path = write_env(tmp_path, DEVICE_MODE="serial", SERIAL_PORT="COM9")

    result = load_env(path)

    assert os.environ["DEVICE_MODE"] == "mock", "파일이 명시적 설정을 덮었다"
    assert "DEVICE_MODE" in result.skipped
    assert "SERIAL_PORT" in result.applied


def test_a_missing_env_file_is_not_an_error(tmp_path, clean_env):
    from server.env import load_env

    result = load_env(tmp_path / "nope.env")
    assert result.found is False
    assert result.applied == () and result.skipped == ()
    assert "DEVICE_MODE" not in os.environ


def test_the_load_report_carries_no_values(tmp_path, clean_env):
    """.env 에는 API 키가 들어 있다. 보고에는 이름만 남긴다."""
    import dataclasses

    from server.env import load_env

    path = write_env(tmp_path, ANTHROPIC_API_KEY="sk-secret-value")
    result = load_env(path)

    dumped = repr(dataclasses.asdict(result))
    assert "ANTHROPIC_API_KEY" in dumped
    assert "sk-secret-value" not in dumped


def test_env_loading_happens_only_at_the_server_entry_point():
    """테스트가 지나는 경로에서는 .env 를 읽지 않는다.

    개발자 기계의 .env 가 새면 DEVICE_MODE=serial 로 테스트가 실기를 잡는다.
    """
    import pathlib as _p

    routes = _p.Path("server/routes/__init__.py").read_text(encoding="utf-8")
    assert routes.count("load_env()") == 1
    entry = routes[routes.index("def __getattr__"):]
    assert "load_env()" in entry, "진입점 밖에서 부르고 있다"


def test_the_mode_decision_lives_in_one_place():
    """두 곳에서 정하면 한쪽의 경고가 묻힌다 (DECISIONS §43)."""
    import pathlib as _p

    routes = _p.Path("server/routes/__init__.py").read_text(encoding="utf-8")
    assert "resolve_device_mode(" in routes
    assert 'os.environ.get("DEVICE_MODE")' not in routes


def test_an_unknown_mode_still_warns_through_build_station(config, clock, caplog):
    """라우트가 조용히 mock 으로 떨어뜨리지 않는다."""
    import logging

    from server.db import Database
    from server.routes import build_station
    from tests.conftest import REAL_POSES, REAL_WORDS

    with caplog.at_level(logging.WARNING, logger="server.device"):
        station = build_station(
            config_path=REAL_POSES,
            words_path=REAL_WORDS,
            database=Database.in_memory(),
            device=MockDevice(config, sleep=clock.sleep),
            device_mode="seriel",
            clock=clock,
        )

    assert station.device_mode == "mock"
    assert any("seriel" in r.getMessage() for r in caplog.records)


def test_serial_mode_reports_the_port_at_startup(config, clean_env, caplog):
    """실기인지 Mock 인지 로그만 보고 알 수 있어야 한다."""
    import logging

    os.environ["DEVICE_MODE"] = "serial"
    os.environ["SERIAL_PORT"] = "COM9"
    with caplog.at_level(logging.INFO, logger="server.device"):
        create_device(config)

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "serial" in messages and "COM9" in messages


def test_mock_mode_says_so_at_startup(config, clean_env, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="server.device"):
        create_device(config)
    assert any("mock" in r.getMessage() for r in caplog.records)
