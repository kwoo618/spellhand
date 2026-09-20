"""세션 상태 브로드캐스트 (PRD 11장 `/ws/session/{id}`).

**WS 는 단방향이다.** 상태를 내보내기만 하고 명령을 받지 않는다. 명령은 전부
REST 로 들어온다 — 구동 경로가 둘이면 안전 게이트를 두 곳에서 통과시켜야 하고,
언젠가 한쪽을 빠뜨린다 (DECISIONS.md §27).

디바이스 동작은 블로킹이라 백그라운드 스레드에서 돈다. 그 스레드가 이벤트를
발행하고, 여기서 이벤트 루프로 넘겨 구독자에게 흘린다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Mapping, Optional, Set

__all__ = ["EventHub", "MCP_LOG_CHANNEL"]

logger = logging.getLogger(__name__)

# MCP 호출 로그는 세션에 속하지 않는다 — 세션이 시작되기 **전에** 오는 호출
# (`get_capabilities`, 락 없는 `present_sequence` 거부)이 시연 ⑥ 의 핵심이기
# 때문이다. 그래서 같은 허브의 별도 채널을 쓴다. 세션 id 는 SQLite
# autoincrement 로 1부터이므로 0 과 겹치지 않는다 (DECISIONS.md §46).
MCP_LOG_CHANNEL = 0

# 구독자 큐가 이만큼 밀리면 가장 오래된 것부터 버린다. 화면이 멈춰 있는 탭
# 하나 때문에 서버 메모리가 늘어나면 안 된다.
_MAX_QUEUED_EVENTS = 256

# 늦게 접속한 클라이언트에게 되돌려 줄 최근 이벤트 수. 문항 하나가 만드는
# 이벤트(presenting + ready x 7 + done + judged)를 덮을 만큼이면 충분하다.
_REPLAY_DEPTH = 32


class EventHub:
    """세션별 이벤트 팬아웃.

    `publish()` 는 **어느 스레드에서 불러도 된다.** 이벤트 루프에 안전하게
    넘긴다. `subscribe()` 는 이벤트 루프 안에서만 부른다.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: Dict[int, Set["asyncio.Queue[Mapping[str, object]]"]] = {}
        self._recent: Dict[int, List[Mapping[str, object]]] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """앱 기동 시 이벤트 루프를 붙인다. 워커 스레드가 이 루프로 넘긴다."""
        self._loop = loop

    # ------------------------------------------------------------------ #

    def subscribe(self, session_id: int) -> "asyncio.Queue[Mapping[str, object]]":
        queue: "asyncio.Queue[Mapping[str, object]]" = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(queue)
        # 접속 직후 최근 상태를 넘겨준다. 문항 표현 도중에 붙어도 화면이
        # 비어 있지 않도록.
        for event in self._recent.get(session_id, []):
            queue.put_nowait(event)
        return queue

    def unsubscribe(
        self, session_id: int, queue: "asyncio.Queue[Mapping[str, object]]"
    ) -> None:
        subscribers = self._subscribers.get(session_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(session_id, None)

    def forget(self, session_id: int) -> None:
        self._recent.pop(session_id, None)

    def subscriber_count(self, session_id: int) -> int:
        return len(self._subscribers.get(session_id, ()))

    # ------------------------------------------------------------------ #

    def publish(self, session_id: int, event: Mapping[str, object]) -> None:
        """이벤트를 흘린다. 워커 스레드에서 불러도 안전하다."""
        history = self._recent.setdefault(session_id, [])
        history.append(event)
        del history[:-_REPLAY_DEPTH]

        loop = self._loop
        if loop is None or not self._subscribers.get(session_id):
            return
        try:
            loop.call_soon_threadsafe(self._fanout, session_id, event)
        except RuntimeError:
            # 루프가 이미 닫혔다. 서버가 내려가는 중이므로 조용히 넘어간다.
            logger.debug("event loop is closed; dropping %r", event.get("type"))

    def _fanout(self, session_id: int, event: Mapping[str, object]) -> None:
        for queue in tuple(self._subscribers.get(session_id, ())):
            if queue.qsize() >= _MAX_QUEUED_EVENTS:
                # 읽지 않는 구독자다. 오래된 것부터 버린다.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)
