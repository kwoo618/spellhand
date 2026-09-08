"""시리얼 어댑터 (PRD 6장, 7장).

`MockDevice` 로 완성한 계층 아래에 이것을 끼우면 위 계층은 한 줄도 바뀌지
않는다. `DeviceAdapter` 계약을 그대로 지킨다 — 동기이고, `Pose` 를 받고,
글자 이름은 내려가지 않는다 (R2).

프로토콜은 `firmware/hand_controller/hand_controller.ino` 와 짝이다.

```
서버 -> 펌웨어   P <seq> <t> <i> <m> <r> <p> <trans_ms> <hold_ms>
                 H | Z | N
펌웨어 -> 서버   ACK <seq> | RDY <seq> | DON <seq> | ERR <seq> <code> | HSH <hash>
```

**연결 직후 해시를 대조한다.** 불일치면 연결 자체를 거부하고 어떤 구동 명령도
보내지 않는다. 건너뛰는 옵션은 없다 (DECISIONS.md §35).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Protocol

from server.calibration import calibration_hash
from server.config import Config
from server.db import utcnow
from server.device import (
    FIRMWARE_ERRORS,
    CalibrationMismatchError,
    DeviceAdapter,
    DeviceBusyError,
    DeviceError,
    DeviceNotConnectedError,
    DeviceProtocolError,
    DeviceTimeoutError,
    Pose,
    pose_to_permille,
)

__all__ = ["SerialDevice", "SerialTransport", "DEFAULT_BAUD"]

logger = logging.getLogger(__name__)

DEFAULT_BAUD = 115200

# PRD 6.5: "P 전송 후 trans_ms + hold_ms + 2000ms 내 DON 미수신 -> Z 전송".
# 이 2000ms 가 그 여유다. poses.json 에 출처가 없어 여기 둔다 (DECISIONS.md §19).
RESPONSE_GRACE_MS = 2000

# ACK 는 펌웨어가 파싱만 하면 바로 보낸다. 여유를 크게 잡을 이유가 없다.
ACK_TIMEOUT_MS = 2000

# seq 없는 명령(Z, N)의 응답에 쓰는 값. 펌웨어와 맞춰 둔 약속이다.
NO_SEQ = -1


class SerialTransport(Protocol):
    """`serial.Serial` 이 만족하는 최소 계약.

    테스트가 가짜를 끼울 수 있도록 프로토콜로 좁혀 두었다. 실제 포트 없이
    어댑터 전체를 검증할 수 있어야 한다.
    """

    def write(self, data: bytes) -> Optional[int]: ...

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


class SerialDevice(DeviceAdapter):
    """실기 손. 한 번에 한 명령만 보내고 완료까지 기다린다.

    동기 구현이라 리더 스레드가 없다. 보내고, 읽고, 돌아온다 — 상위 계층이
    이미 그 전제로 짜여 있다 (DECISIONS.md §14).
    """

    def __init__(
        self,
        config: Config,
        *,
        port: str,
        baud: int = DEFAULT_BAUD,
        transport: Optional[SerialTransport] = None,
        open_transport: Optional[Callable[[], SerialTransport]] = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        seq_source: Callable[[], int] = lambda: int(time.time()),
        ack_timeout_ms: int = ACK_TIMEOUT_MS,
        response_grace_ms: int = RESPONSE_GRACE_MS,
    ) -> None:
        self._config = config
        self._port = port
        self._baud = baud
        self._transport = transport
        self._open_transport = open_transport
        self._clock = clock
        self._sleep = sleep
        self._seq_source = seq_source
        self._ack_timeout_ms = ack_timeout_ms
        self._response_grace_ms = response_grace_ms

        self._connected = False
        self._busy = False
        self._seq = 0
        self._arrival_at: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # 연결
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """포트를 열고 **해시를 대조한 뒤에만** 사용 가능 상태가 된다."""
        if self._transport is None:
            self._transport = self._make_transport()

        expected = calibration_hash(self._config)
        try:
            reported = self._read_hash()
        except DeviceError:
            self._close_transport()
            raise

        if reported != expected:
            self._close_transport()
            raise CalibrationMismatchError(
                f"firmware CAL_HASH is {reported!r} but {self._config.source_path.name} "
                f"produces {expected!r}. The firmware was built from a different "
                f"calibration, so the hand would not make the poses the server "
                f"intends.\n"
                f"  Fix: python tools/gen_calibration.py\n"
                f"       then re-upload firmware/hand_controller to the board."
            )

        # seq 는 에포크 초에서 시작한다. 재연결이든 서버 재시작이든 항상 직전보다
        # 큰 값이 나가므로 펌웨어의 ESEQ 와 엇갈리지 않는다 (DECISIONS.md §37).
        self._seq = self._seq_source()
        self._connected = True
        self._busy = False
        self._arrival_at = None
        logger.info(
            "serial device connected on %s (CAL_HASH %s, seq from %d)",
            self._port,
            reported,
            self._seq,
        )

    def _make_transport(self) -> SerialTransport:
        if self._open_transport is not None:
            return self._open_transport()

        import serial  # 지연 import: mock 모드에서는 필요 없다

        # readline() 이 영원히 막히지 않도록 짧은 타임아웃을 둔다. 기다리는
        # 시간은 이 어댑터가 데드라인으로 관리한다.
        return serial.Serial(
            self._port, self._baud, timeout=_TRANSPORT_READ_TIMEOUT_S
        )

    def disconnect(self) -> None:
        self._connected = False
        self._busy = False
        self._close_transport()

    def _close_transport(self) -> None:
        if self._transport is None:
            return
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001 - 닫다 실패해도 더 할 일이 없다
            logger.warning("failed to close serial port %s", self._port, exc_info=True)
        self._transport = None

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def is_connected(self) -> bool:
        return self._connected

    def last_arrival_at(self) -> Optional[datetime]:
        """`RDY` 를 받은 시각. 관측 시간의 기준점이다 (DECISIONS.md §36)."""
        return self._arrival_at

    # ------------------------------------------------------------------ #
    # 동작
    # ------------------------------------------------------------------ #

    def move_to(self, pose: Pose, transition_ms: int, hold_ms: int) -> None:
        if not isinstance(pose, Pose):
            raise TypeError(
                f"move_to() takes a Pose, got {type(pose).__name__} ({pose!r}). "
                f"The device layer never receives letter names (R2)."
            )
        self._require_connected()
        if transition_ms < 0 or hold_ms < 0:
            raise ValueError(
                f"transition_ms={transition_ms}, hold_ms={hold_ms}; "
                f"expected both to be zero or positive"
            )

        permille = pose_to_permille(pose)
        values = " ".join(str(permille[finger]) for finger in self._config.fingers)

        self._seq += 1
        seq = self._seq
        command = f"P {seq} {values} {transition_ms} {hold_ms}"
        self._run(command, seq, transition_ms=transition_ms, hold_ms=hold_ms)

    def neutral(self) -> None:
        """중립 자세. `N` 명령이다. safe() 를 호출하지 않는다 (R3)."""
        self._require_connected()
        timing = self._config.timing
        self._run(
            "N", NO_SEQ, transition_ms=timing.transition_ms, hold_ms=0
        )

    def safe(self) -> None:
        """안전 자세로 가서 정착할 때까지 유지한다. neutral() 을 호출하지 않는다 (R3).

        펌웨어의 `Z` 는 즉시 도달하고 유지하지 않는다. 유지는 어댑터가 기다려
        만든다 — MockDevice 와 같은 의미를 갖게 하기 위해서다 (DECISIONS.md §17).
        """
        self._require_connected()
        self._run("Z", NO_SEQ, transition_ms=0, hold_ms=0)
        self._sleep(self._config.timing.hold_min_ms / 1000)

    def stop(self) -> None:
        """비상 정지. 즉시 안전 자세로 만들고 기다리지 않는다."""
        self._require_connected()
        self._run("Z", NO_SEQ, transition_ms=0, hold_ms=0)

    # ------------------------------------------------------------------ #
    # 프로토콜
    # ------------------------------------------------------------------ #

    def _require_connected(self) -> None:
        if not self._connected or self._transport is None:
            raise DeviceNotConnectedError(
                f"serial device on {self._port} is not connected; call connect() first"
            )

    def _run(
        self, command: str, seq: int, *, transition_ms: int, hold_ms: int
    ) -> None:
        """명령 하나를 보내고 ACK -> RDY -> DON 을 끝까지 기다린다."""
        if self._busy:
            raise DeviceBusyError(
                "serial device is already running a command; "
                "wait for it to finish before sending another"
            )

        self._busy = True
        try:
            self._write(command)
            self._await(command, seq, "ACK", self._ack_timeout_ms)
            arrival_budget = transition_ms + self._response_grace_ms
            self._await(command, seq, "RDY", arrival_budget)
            # RDY 를 받은 순간이 손이 목표에 도달한 시각이다. 역산하지 않는다.
            self._arrival_at = self._clock()
            self._await(
                command, seq, "DON", hold_ms + self._response_grace_ms
            )
        finally:
            self._busy = False

    def _write(self, command: str) -> None:
        assert self._transport is not None
        try:
            self._transport.write((command + "\n").encode("ascii"))
        except Exception as exc:  # noqa: BLE001 - 포트가 사라졌다
            self._connected = False
            raise DeviceNotConnectedError(
                f"serial port {self._port} went away while writing {command!r}: {exc}"
            ) from exc

    def _await(self, command: str, seq: int, tag: str, budget_ms: int) -> None:
        """기다리던 응답이 올 때까지 읽는다. 오지 않으면 안전 자세로 되돌린다."""
        deadline = self._clock() + timedelta(milliseconds=budget_ms)

        while True:
            line = self._read_line(deadline)
            if line is None:
                self._recover_after_timeout()
                raise DeviceTimeoutError(
                    f"no {tag} for {command!r} within {budget_ms}ms; "
                    f"sent Z to return the hand to the safe pose"
                )

            kind, parts = _split(line)
            if kind == "ERR":
                raise self._firmware_error(command, parts)
            if kind == tag:
                self._expect_seq(command, tag, seq, parts)
                return

            # 기다리던 것이 아닌 응답. 프로토콜이 어긋났다는 뜻이므로 넘기지 않는다.
            raise DeviceProtocolError(
                f"expected {tag} for {command!r} but the firmware said {line!r}"
            )

    def _read_line(self, deadline: datetime) -> Optional[str]:
        assert self._transport is not None
        while self._clock() < deadline:
            try:
                raw = self._transport.readline()
            except Exception as exc:  # noqa: BLE001 - 포트가 사라졌다
                self._connected = False
                raise DeviceNotConnectedError(
                    f"serial port {self._port} went away while reading: {exc}"
                ) from exc
            if raw:
                text = raw.decode("ascii", errors="replace").strip()
                if text:
                    return text
        return None

    def _expect_seq(self, command: str, tag: str, seq: int, parts: List[str]) -> None:
        if not parts:
            raise DeviceProtocolError(
                f"{tag} for {command!r} carried no seq"
            )
        try:
            reported = int(parts[0])
        except ValueError as exc:
            raise DeviceProtocolError(
                f"{tag} for {command!r} carried a non-numeric seq {parts[0]!r}"
            ) from exc
        if reported != seq:
            # 응답을 엉뚱한 글자에 대응시키게 된다. 조용히 넘어가면 관측 시각과
            # 채점이 어긋난다.
            raise DeviceProtocolError(
                f"{tag} for {command!r} reported seq {reported}, expected {seq}"
            )

    def _firmware_error(self, command: str, parts: List[str]) -> DeviceError:
        code = parts[1] if len(parts) > 1 else ""
        error = FIRMWARE_ERRORS.get(code, DeviceProtocolError)
        return error(
            f"firmware rejected {command!r} with {code or 'an unknown code'}"
        )

    def _recover_after_timeout(self) -> None:
        """응답이 끊겼을 때 손을 안전 자세로 되돌린다.

        타임아웃은 "손이 어떤 상태인지 모른다"는 뜻이다. 모르는 채로 두면
        서보가 어떤 자세를 계속 물고 있을 수 있으므로 `Z` 를 보내 최저 장력
        자세로 내린다. 응답은 기다리지 않는다 — 이미 응답이 안 오는 상황이다
        (DECISIONS.md §38).
        """
        try:
            self._write("Z")
        except DeviceError:
            logger.warning(
                "could not send Z after a timeout on %s; the port is gone",
                self._port,
            )

    def _read_hash(self) -> str:
        """`H` 를 보내고 `HSH <hash>` 를 받는다."""
        self._write("H")
        deadline = self._clock() + timedelta(milliseconds=self._ack_timeout_ms)
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise DeviceTimeoutError(
                    f"no HSH from the firmware on {self._port} within "
                    f"{self._ack_timeout_ms}ms; is the board running "
                    f"hand_controller?"
                )
            kind, parts = _split(line)
            if kind == "HSH" and parts:
                return parts[0]
            if kind == "ERR":
                raise self._firmware_error("H", parts)
            # 부팅 직후의 잡음일 수 있으므로 HSH 가 나올 때까지 계속 읽는다.


# 포트에서 한 번 읽을 때 최대로 기다리는 시간. 전체 대기는 어댑터가
# 데드라인으로 관리하므로 여기서는 짧게 잡는다.
_TRANSPORT_READ_TIMEOUT_S = 0.05


def _split(line: str):
    parts = line.split()
    return (parts[0] if parts else ""), parts[1:]
