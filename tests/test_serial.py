"""시리얼 어댑터 테스트 (PRD 6장, 7장).

**실제 포트를 열지 않는다.** 펌웨어 응답을 흉내내는 가짜 전송로를 끼워
어댑터 전체를 검증한다. 실기는 전기팀 입회가 필요하다.

시간은 주입한다. 가짜 전송로가 읽을 때마다 시계를 밀어, 타임아웃이 실제
운용과 같은 조건에서 걸리게 한다.
"""

import pytest

from server.calibration import calibration_hash
from server.device import (
    CalibrationMismatchError,
    DeviceBusyError,
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
