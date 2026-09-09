"""FastAPI 앱 조립 (PRD 11장).

```
uvicorn server.routes:app
```

**구동 경로는 하나다.** 브라우저의 모든 명령은 REST 로 들어와
`SessionController` → `SafetyGate` 를 지난다. WS 는 상태를 내보내기만 한다
(DECISIONS.md §27). 라우트가 게이트를 우회하는 길은 없다 — 라우트는 디바이스를
직접 만지지 않고 컨트롤러만 부른다.

인증도 CORS 도 없다. 단일 학습자가 같은 서버에서 정적 파일까지 받아 가는
로컬 실행 전제다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from server.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_WORDS_PATH,
    Config,
    WordSet,
    load_config,
    load_words,
)
from server.db import Database, utcnow
from server.device import (
    DEVICE_MODE_MOCK,
    DeviceAdapter,
    DeviceCancelledError,
    DeviceError,
    create_device,
    resolve_device_mode,
)
from server.env import load_env
from server.routes.api import router as api_router
from server.routes.events import EventHub
from server.safety import SafetyError, SafetyGate, SessionLock
from server.session import SessionController, SessionError

__all__ = ["Station", "create_app", "app"]

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEB_ROOT = REPO_ROOT / "web"

# mockhand 로 flex 를 보내는 주기. 보간 틱(20ms)마다 보내면 초당 50건인데,
# 화면이 확인하려는 것은 "값이 맞는가" 이지 애니메이션 품질이 아니다.
# 50ms 면 900ms 전이 하나가 18프레임으로 그려져 움직임이 끊겨 보이지 않으면서
# 트래픽은 1/2.5 로 준다 (DECISIONS.md §41).
FLEX_FEED_INTERVAL_MS = 50


@dataclass
class Station:
    """서버가 들고 있는 것 전부. 라우트는 이것만 본다."""

    config: Config
    words: WordSet
    database: Database
    device: DeviceAdapter
    gate: SafetyGate
    lock: SessionLock
    hub: EventHub
    device_mode: str
    clock: Callable[[], datetime]
    rng: random.Random
    background: Callable[[Callable[[], None]], None]
    controllers: Dict[int, SessionController] = field(default_factory=dict)

    def new_controller(self) -> SessionController:
        """세션 하나를 위한 컨트롤러. 이벤트는 허브로 흘린다."""
        controller_ref: Dict[str, SessionController] = {}

        def emit(event: Dict[str, object]) -> None:
            controller = controller_ref.get("it")
            if controller is None or controller._session_id is None:
                return
            self.hub.publish(controller.session_id, event)

        controller = SessionController(
            self.config,
            self.words,
            self.database,
            self.device,
            self.gate,
            self.lock,
            clock=self.clock,
            rng=self.rng,
            on_event=emit,
        )
        controller_ref["it"] = controller
        return controller

    def run_in_background(self, session_id: int, work: Callable[[], object]) -> None:
        """디바이스 동작을 백그라운드로 돌린다.

        서보 동작은 물리적으로 블로킹이다 (DECISIONS.md §14). HTTP 요청을
        15초 붙잡아 두는 대신 즉시 돌려주고, 진행 상황은 WS 로 알린다.
        실패도 WS 로 나간다 — 요청은 이미 끝났기 때문이다.
        """

        def guarded() -> None:
            try:
                work()
            except DeviceCancelledError as exc:
                # 사람이 정지 버튼을 눌렀다. 통신도 세션도 멀쩡하므로 락을
                # 놓지 않는다 — 학습자는 "다시 보기" 로 이어 가면 된다
                # (DECISIONS.md §42).
                logger.info("presentation cancelled by a stop: %s", exc)
                self.hub.publish(
                    session_id,
                    {"type": "error", "code": "STOPPED", "detail": str(exc)},
                )
            except (SafetyError, SessionError) as exc:
                # 거부다. 아무것도 구동되지 않았고 세션은 그대로 살아 있다.
                logger.warning("background work rejected: %s", exc)
                self.hub.publish(
                    session_id,
                    {"type": "error", "code": exc.code, "detail": str(exc)},
                )
            except Exception as exc:  # noqa: BLE001 - 스레드가 조용히 죽지 않게
                # 예상하지 못한 실패다 — 손이 대답하지 않거나 버그다. 어느
                # 쪽이든 이 세션은 더 못 간다. 스레드가 그냥 죽으면 락을 쥔
                # 채로 남아 다음 세션이 만료까지 시작하지 못한다. 실제로
                # 그렇게 됐다 (DECISIONS.md §42).
                code = (
                    "DEVICE_FAILED" if isinstance(exc, DeviceError) else "INTERNAL"
                )
                logger.exception(
                    "background work failed; ending session %s", session_id
                )
                self.hub.publish(
                    session_id,
                    {"type": "error", "code": code, "detail": str(exc)},
                )
                self.abort_session(session_id)

        self.background(guarded)

    def abort_session(self, session_id: int) -> None:
        """세션을 접고 **락을 반드시 놓는다.** 두 번 불러도 안전하다.

        정리 도중 또 실패해도(손이 안전 자세로 가지 못하는 등) 락은 놓는다.
        여기까지 온 것은 이미 무언가 잘못된 상황이고, 여기서 또 멈추면 다음
        학습자가 시작조차 못 한다.
        """
        controller = self.controllers.pop(session_id, None)
        try:
            if controller is not None:
                controller.end(status="aborted")
        except Exception:  # noqa: BLE001 - 정리 실패가 락을 물게 두지 않는다
            logger.exception("could not close session %s cleanly", session_id)
        finally:
            # end() 안에서도 놓지만, 그 전에 죽었을 수 있다. 보유자가 아니면
            # release() 는 아무것도 하지 않으므로 두 번 불러도 무해하다.
            self.lock.release(session_id)

    def reset_device(self) -> Tuple[int, ...]:
        """물린 상태를 서버 재시작 없이 푼다 (PRD 11장).

        진행 중인 동작을 끊고, 이 서버가 아는 세션을 전부 aborted 로 닫고,
        락을 놓는다. **§42 수정의 대체가 아니라 보조다** — 원인을 고치는 것이
        먼저고, 이것은 시연 중에 서버를 껐다 켤 수 없어서 두는 마지막 수단이다.

        끊긴 세션 id 들을 돌려준다.
        """
        try:
            # 먼저 손을 멈춘다. 구동 중인 스레드가 이것을 보고 물러난다.
            self.device.stop()
        except Exception:  # noqa: BLE001 - 멈추지 못해도 락은 풀어야 한다
            logger.exception("the device did not accept the stop during reset")

        targets = list(self.controllers)
        holder = self.lock.holder()
        if holder is not None and holder not in targets:
            # 컨트롤러는 사라졌는데 락만 남은 경우. 락만 놓는다.
            targets.append(holder)
        for session_id in targets:
            self.abort_session(session_id)
        return tuple(targets)


def _thread_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def build_station(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    words_path: Path = DEFAULT_WORDS_PATH,
    database: Optional[Database] = None,
    device: Optional[DeviceAdapter] = None,
    device_mode: Optional[str] = None,
    clock: Callable[[], datetime] = utcnow,
    rng: Optional[random.Random] = None,
    background: Optional[Callable[[Callable[[], None]], None]] = None,
) -> Station:
    """설정을 읽고 계층을 조립한다. 설정이 틀리면 여기서 예외가 나고 서버는 뜨지 않는다."""
    config = load_config(config_path)
    words = load_words(words_path, letters=config.poses)

    # 어댑터 선택은 server.device 한 곳에만 있다. 여기서 다시 판단하지 않는다 —
    # 여기서 또 정규화하면 그쪽 경고가 묻힌다 (DECISIONS.md §43).
    mode = resolve_device_mode(device_mode)
    if device is None:
        device = create_device(config, mode=mode)

    db = database or Database()
    db.create_all()
    lock = SessionLock(db, clock=clock)
    gate = SafetyGate(config, device, lock, clock=clock)

    return Station(
        config=config,
        words=words,
        database=db,
        device=device,
        gate=gate,
        lock=lock,
        hub=EventHub(),
        device_mode=mode,
        clock=clock,
        rng=rng or random.Random(),
        background=background or _thread_background,
    )


def create_app(station: Optional[Station] = None, **kwargs: object) -> FastAPI:
    station = station or build_station(**kwargs)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        station.hub.bind_loop(asyncio.get_running_loop())
        # 서버 기동 시: 고아 락 정리, 디바이스 안전 자세 (PRD 10장).
        orphan = station.lock.clear_expired()
        if orphan is not None:
            logger.warning("cleared an orphaned lock from session %s", orphan)
        station.device.connect()
        yield

    app = FastAPI(
        title="spellhand", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.station = station

    # 거부는 safety.py / session.py 가 만든 코드를 그대로 싣는다.
    # 라우트에서 에러 문자열을 새로 짓지 않는다.
    def _rejection(exc) -> JSONResponse:
        body: Dict[str, object] = {"code": exc.code, "detail": str(exc)}
        alternatives = getattr(exc, "alternatives", None)
        if alternatives is not None:
            body["alternatives"] = list(alternatives)
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(SafetyError)
    def _on_safety_error(request: Request, exc: SafetyError) -> JSONResponse:
        return _rejection(exc)

    @app.exception_handler(SessionError)
    def _on_session_error(request: Request, exc: SessionError) -> JSONResponse:
        return _rejection(exc)


    @app.websocket("/ws/session/{session_id}")
    async def session_events(websocket: WebSocket, session_id: int) -> None:
        """세션 상태 브로드캐스트.

        **받지 않는다.** 이 소켓으로 들어온 것은 읽지 않는다 — 명령 경로는
        REST 하나뿐이다.
        """
        await websocket.accept()
        queue = station.hub.subscribe(session_id)
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(dict(event))
        except WebSocketDisconnect:
            pass
        finally:
            station.hub.unsubscribe(session_id, queue)

    @app.websocket("/ws/device")
    async def device_flex(websocket: WebSocket) -> None:
        """Mock 손의 flex 를 일정 주기로 흘려보낸다 (mockhand.html 용).

        **세션과 무관하다.** 디바이스만 두드려 보는 개발 중에도 열려야 하므로
        `/ws/session/{id}` 를 쓰지 않는다 (DECISIONS.md §41).

        **읽기 전용이고 Mock 전용이다.** 이 소켓으로 손을 움직일 수단은 없고,
        실기 모드에서는 열리지 않는다 — 실기는 실물을 보면 된다.
        """
        await websocket.accept()

        read_flex = getattr(station.device, "current_flex", None)
        if station.device_mode != DEVICE_MODE_MOCK or read_flex is None:
            await websocket.send_json(
                {
                    "type": "unavailable",
                    "reason": "MOCK_ONLY",
                    "detail": (
                        f"the flex feed is a mock-only development view; "
                        f"DEVICE_MODE is {station.device_mode!r}"
                    ),
                }
            )
            await websocket.close()
            return

        # 자세 표를 한 번만 보낸다. 어느 자세인지는 클라이언트가 flex 로
        # 되짚는다 — 서버가 "지금 무슨 글자" 를 들고 있지 않기도 하고,
        # 그렇게 해야 flex 와 자세가 맞는지를 확인하게 된다 (DECISIONS.md §41).
        await websocket.send_json(
            {
                "type": "hello",
                "fingers": list(station.config.fingers),
                "poses": {
                    letter: dict(pose)
                    for letter, pose in station.config.poses.items()
                },
                "neutral": dict(station.config.neutral),
                "safe": dict(station.config.safe),
                "interval_ms": FLEX_FEED_INTERVAL_MS,
            }
        )

        try:
            while True:
                await websocket.send_json(
                    {
                        "type": "flex",
                        "flex": dict(read_flex()),
                        "busy": station.device.is_busy,
                    }
                )
                await asyncio.sleep(FLEX_FEED_INTERVAL_MS / 1000)
        except WebSocketDisconnect:
            pass

    app.include_router(api_router)

    @app.get("/")
    def _index() -> RedirectResponse:
        return RedirectResponse(url="/practice.html")

    if WEB_ROOT.is_dir():
        # 정적 파일은 같은 서버가 낸다. 그래서 CORS 설정이 필요 없다.
        app.mount("/", StaticFiles(directory=str(WEB_ROOT)), name="web")

    return app


_app: Optional[FastAPI] = None


def __getattr__(name: str) -> object:
    """`uvicorn server.routes:app` 을 위한 지연 생성.

    import 만으로 설정을 읽고 station.db 를 만들지 않는다. 테스트가 이 모듈을
    import 할 때 파일이 생기면 곤란하다.
    """
    if name != "app":
        raise AttributeError(name)
    global _app
    if _app is None:
        # 서버를 띄우는 경로는 여기 하나다. `.env` 도 여기서만 읽는다 —
        # 테스트는 이 경로를 지나지 않으므로 개발자 기계의 .env 가 새지 않는다.
        _configure_logging()
        load_env()
        _app = create_app()
    return _app


def _configure_logging() -> None:
    """기동 로그가 보이도록 한다.

    실기인지 Mock 인지가 로그에 남지 않으면, 손이 안 움직일 때 원인을 찾는 데
    시간이 든다. 이미 핸들러가 있으면 basicConfig 는 아무것도 하지 않는다.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("server").setLevel(logging.INFO)
