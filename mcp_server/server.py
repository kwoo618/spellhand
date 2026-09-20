"""spellhand MCP 서버 (PRD 12장). 전송은 stdio.

클라이언트는 Claude Desktop (시연 ⑤) 과 이후의 `agent.py` 다.

**이 파일은 얇은 어댑터다.** 판정도 적응도 안전 검사도 여기서 하지 않는다.
도구 하나가 스테이션 서버의 REST 호출 하나로 내려가고, 구동은 기존
`/api/sessions/...` 라우트 → `SessionController` → `SafetyGate` 를 지난다.
그래서 **MCP 경로가 안전 게이트를 우회할 길이 구조적으로 없다** (R4).

별도 프로세스인 이유는 디바이스와 세션 컨트롤러가 한 프로세스에만 있어야
하기 때문이다. Claude Desktop 이 이 파일을 띄우고, 이 파일이 이미 떠 있는
스테이션 서버를 부른다. 서버가 없으면 모든 도구가 `station_unreachable`
구조화 실패를 돌려준다 (DECISIONS.md §46).

**도구 스키마에 flex·각도·transition 파라미터가 없다.** LLM 이 각도를 지정하는
경로는 스키마 수준에서 존재하지 않는다 (R1, PRD 15.2 의 "설명 슬라이드" 근거).
`tests/test_mcp.py` 가 이 사실을 회귀 테스트로 고정한다.

띄우는 방법:

    .venv\\Scripts\\python.exe -m mcp_server.server

도구 목록만 확인하려면:

    .venv\\Scripts\\python.exe -m mcp_server.server --list-tools
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence

import httpx2 as httpx
from mcp.server import MCPServer
from pydantic import BaseModel, Field

from server.db import BOUNDARIES, TRACKS
from server.env import load_env

__all__ = ["StationClient", "SessionPlanInput", "create_server", "main"]

logger = logging.getLogger(__name__)

STATION_URL_ENV = "STATION_URL"
DEFAULT_STATION_URL = "http://127.0.0.1:8000"

SERVER_NAME = "spellhand"
SERVER_VERSION = "0.1.0"

# 구동 요청은 손이 끝날 때까지 돌아오지 않는다 (DECISIONS.md §46). 최악은
# 7글자를 또박또박·느린 재생·최대 유지로 내는 경우이고, 전이 900·느린 전이
# 1800·최대 유지 3000 으로 계산하면 1800+3000 + 6×(900+1800+3000) = 39.0초다.
# 그 두 배를 넘겨 잡는다 — 여기서 먼저 끊으면 손은 계속 움직이는데 호출자만
# 실패를 받는다.
REQUEST_TIMEOUT_SECONDS = 120.0

# 스테이션이 붙인 거부 코드가 없을 때만 쓰는 어댑터 쪽 이름 두 개.
# 나머지 `reason` 은 전부 스테이션의 거부 코드를 소문자로 바꾼 것이다 —
# 거부 사유를 여기서 새로 짓지 않는다.
REASON_UNREACHABLE = "station_unreachable"
REASON_INVALID_ARGUMENTS = "invalid_arguments"

# 구조화된 실패의 `next_step` (PRD 12.2). LLM 이 다음에 뭘 할지 알아야 한다.
# 코드가 여기 없으면 `next_step` 없이 `reason` 과 `detail` 만 나간다 — 없는
# 코드에 대해 그럴듯한 다음 단계를 지어내지 않는다.
NEXT_STEP: Dict[str, str] = {
    "LOCK_NOT_HELD": "call start_session() first",
    "LOCK_UNAVAILABLE": (
        "another session holds the hand; call get_station_status() to see "
        "which one, then wait for it to end"
    ),
    "UNSUPPORTED_LETTERS": "call get_item_bank() with supported letters only",
    "CURRICULUM_VIOLATION": (
        "use demo_letter for these, or choose non-depth pairs"
    ),
    "RATE_LIMIT": (
        "wait transition_ms (see get_capabilities) and call again"
    ),
    "SESSION_QUOTA": "call end_session() and start a new session",
    "DEVICE_BUSY": (
        "call get_station_status() until state is idle, then call again"
    ),
    "CAL_MISMATCH": (
        "the firmware calibration no longer matches poses.json; it has to be "
        "regenerated and re-uploaded before the hand can move"
    ),
    "DEVICE_TIMEOUT": (
        "the hand did not report completion; call get_station_status() and "
        "end the session if it stays busy"
    ),
    "SESSION_STATE": "call get_station_status() to see where the session is",
    "SESSION_FINISHED": "call end_session() for the summary",
}

INSTRUCTIONS = """\
지문자(ASL 알파벳) 수용 훈련 로봇손 스테이션이다.

**먼저 `get_capabilities()` 를 부른다.** 이 손이 만들 수 있는 글자, 트랙,
혼동쌍, 난이도 범위가 전부 거기서 나온다. 글자 목록을 추측하지 않는다.

구동(`present_sequence`, `demo_letter`)은 세션이 있어야 한다:
`start_session()` → 구동 → `submit_answer()` → `end_session()`.

거부는 예외가 아니라 `{"ok": false, "reason": ..., "next_step": ...}` 로 온다.
`next_step` 을 읽고 다음 호출을 정한다.

각도·flex·전이 시간을 지정할 수 없다. 자세는 스테이션의 설정 파일에만 있다.
"""


# --------------------------------------------------------------------------- #
# 스테이션 클라이언트
# --------------------------------------------------------------------------- #

def _stringify(detail: Any) -> str:
    """스테이션이 준 `detail` 을 문자열로 만든다. 새 문구를 섞지 않는다."""
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False)


class StationClient:
    """스테이션 서버(REST)로 내려가는 통로. 도구 호출 로그도 여기서 보낸다.

    `http` 를 주입할 수 있다. 테스트는 ASGI 클라이언트를 넣어 같은 프로세스
    안에서 실제 라우트를 지나게 한다 — 게이트를 우회하지 않는다는 것을 확인하는
    테스트가 실제 게이트를 지나야 의미가 있다.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        http: Optional[Any] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get(STATION_URL_ENV) or DEFAULT_STATION_URL
        ).rstrip("/")
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout)

    # ------------------------------------------------------------------ #

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> "tuple[int, Any]":
        clean = (
            {key: value for key, value in params.items() if value is not None}
            if params
            else None
        )
        response = self._http.request(
            method, path, params=clean or None, json=json_body
        )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"detail": response.text}

    def log(self, type_: str, **fields: Any) -> None:
        """호출 기록을 `/ws/mcp-log` 로 흘려보낸다 (시연 ⑥).

        **실패해도 도구를 실패시키지 않는다.** 스테이션이 없으면 도구 자체가
        곧 `station_unreachable` 로 답하므로 여기서 또 알릴 필요가 없다.
        """
        try:
            self._send("POST", "/api/mcp-log", json_body={"type": type_, **fields})
        except Exception:  # noqa: BLE001 - 로그가 도구를 막지 않는다
            logger.debug("could not publish an mcp-log event", exc_info=True)

    # ------------------------------------------------------------------ #

    def invoke(
        self,
        tool: str,
        summary: str,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        requested: Optional[str] = None,
        summarize: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> Dict[str, Any]:
        """도구 하나를 스테이션 호출 하나로 내려보낸다.

        `requested` 는 요청된 글자열이다. 구조화된 실패에 어떤 글자가 문제였는지
        담기 위한 것이며, 스테이션의 거부 메시지를 파싱하지 않기 위해 호출자가
        그대로 넘긴다.
        """
        self.log("tool_call", name=tool, summary=summary)

        try:
            status, body = self._send(
                method, path, params=params, json_body=json_body
            )
        except httpx.RequestError as exc:
            failure = {
                "ok": False,
                "reason": REASON_UNREACHABLE,
                "detail": f"the station at {self.base_url} did not answer: {exc}",
                "next_step": (
                    "start the station (uvicorn server.routes:app) and call again"
                ),
            }
            # 로그도 같은 서버로 가므로 이것 역시 도착하지 않는다. 그래도 부른다 —
            # 서버가 도중에 돌아오면 이후 기록이 이어진다.
            self.log(
                "blocked",
                name=tool,
                reason=REASON_UNREACHABLE,
                detail=failure["detail"],
            )
            return failure

        if status >= 400:
            failure = self._failure(body, requested=requested)
            self.log(
                "blocked",
                name=tool,
                reason=failure["reason"],
                detail=failure.get("detail", ""),
            )
            return failure

        result: Dict[str, Any] = {"ok": True}
        if isinstance(body, dict):
            result.update(body)
        else:
            result["result"] = body
        self.log(
            "tool_result",
            name=tool,
            summary=summarize(result) if summarize else summary,
        )
        return result

    def _failure(
        self, body: Any, *, requested: Optional[str]
    ) -> Dict[str, Any]:
        """스테이션의 거부를 구조화된 실패로 옮긴다 (PRD 12.2).

        `reason` 은 스테이션의 거부 코드를 소문자로 바꾼 것이다. 매핑 표를 두지
        않는 이유는, 표를 두면 스테이션에 새 거부 코드가 생길 때 여기를 고치지
        않으면 조용히 잘못된 사유가 나가기 때문이다.
        """
        if not isinstance(body, dict):
            body = {"detail": body}
        code = str(body.get("code") or "")
        detail = _stringify(body.get("detail"))

        if not code:
            # FastAPI 의 인자 검증 실패다. 스테이션이 거부 코드를 붙이지 않는
            # 유일한 경우이므로 여기서만 어댑터 쪽 이름을 쓴다.
            return {
                "ok": False,
                "reason": REASON_INVALID_ARGUMENTS,
                "detail": detail,
                "next_step": (
                    "check the arguments against get_capabilities() and call again"
                ),
            }

        failure: Dict[str, Any] = {
            "ok": False,
            "reason": code.lower(),
            "detail": detail,
        }
        step = NEXT_STEP.get(code)
        if step is not None:
            failure["next_step"] = step

        if code == "UNSUPPORTED_LETTERS":
            supported = [str(letter) for letter in body.get("alternatives") or []]
            asked = (requested or "").upper()
            failure["requested"] = requested or ""
            failure["unsupported"] = sorted(
                {letter for letter in asked if letter not in supported}
            )
            failure["supported"] = supported

        if code == "CURRICULUM_VIOLATION" and requested:
            failure["blocked_pairs"] = self.depth_pairs_in(requested)

        return failure

    def depth_pairs_in(self, letters: str) -> List[List[str]]:
        """이 글자열이 품고 있는 깊이쌍.

        쌍 목록을 여기 적어 두지 않고 `get_capabilities` 에서 읽는다 —
        `poses.json` 이 단일 출처다 (DECISIONS.md §1). 읽지 못하면 빈 목록을
        낸다. 추측한 쌍을 내보내는 것보다 비어 있는 것이 낫다 (R5).
        """
        try:
            status, body = self._send("GET", "/api/capabilities")
        except httpx.RequestError:
            return []
        if status >= 400 or not isinstance(body, dict):
            return []
        present = set(letters.upper())
        return [
            [str(letter) for letter in pair.get("pair") or []]
            for pair in body.get("confusion_pairs") or []
            if pair.get("depth")
            and set(str(letter) for letter in pair.get("pair") or []) <= present
        ]


# --------------------------------------------------------------------------- #
# 도구 인자 모델
# --------------------------------------------------------------------------- #

class SessionPlanInput(BaseModel):
    """세션 구성. 생략하면 스테이션의 기본값(판별 8 + 통글자 4)이 쓰인다.

    기본값을 여기 적지 않는 것은 `session.SessionPlan` 이 단일 출처이기
    때문이다. 비워 두면 스테이션이 자기 기본값을 넣는다.
    """

    discrimination_items: Optional[int] = Field(
        None, description="판별 트랙 문항 수"
    )
    word_items: Optional[int] = Field(None, description="통글자 트랙 문항 수")


# --------------------------------------------------------------------------- #
# 서버
# --------------------------------------------------------------------------- #

def create_server(station: Optional[StationClient] = None) -> MCPServer:
    """도구가 등록된 MCP 서버를 만든다.

    `station` 을 주입할 수 있다. 테스트가 같은 프로세스의 실제 라우트를 지나게
    하기 위한 것이다.
    """
    client = station or StationClient()
    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
    )

    # ------------------------------------------------------------------ #
    # 1층 · 조회 (부작용 없음)
    # ------------------------------------------------------------------ #

    @server.tool(
        description=(
            "이 로봇손의 런타임 능력 선언. 만들 수 있는 글자, 트랙, 혼동쌍, "
            "난이도 범위(유지 시간·길이·경계 모드), 전이 시간, 단어 수. "
            "**다른 도구를 부르기 전에 먼저 부른다.** 글자 목록을 추측하지 않는다. "
            "learner_name 을 주면 그 학습자의 깊이쌍 해금 여부가 함께 나온다."
        )
    )
    def get_capabilities(learner_name: Optional[str] = None) -> dict[str, Any]:
        return client.invoke(
            "get_capabilities",
            "능력 선언 조회",
            "GET",
            "/api/capabilities",
            params={"learner_name": learner_name},
            summarize=lambda result: (
                f"글자 {len(result.get('letters') or [])}자, "
                f"단어 {result.get('word_count')}개"
            ),
        )

    @server.tool(
        description=(
            "학습자의 트랙별 성적. 정확도, 혼동쌍별 발생 횟수, 순서 오류율, "
            "유지 시간별 정확도, 최근 문항. **트랙별로만 나온다** — 판별과 "
            "통글자를 합친 수치는 없다. 단어 트랙의 정답은 철자 지식으로 "
            "보정되므로 두 트랙을 합치면 지표가 오염된다. "
            "기록이 없는 이름도 정상이며 0 으로 채워 나온다."
        )
    )
    def get_learner_stats(learner_name: str) -> dict[str, Any]:
        return client.invoke(
            "get_learner_stats",
            f"학습자 {learner_name} 조회",
            "GET",
            f"/api/learners/{learner_name}/stats",
            summarize=lambda result: _stats_summary(result),
        )

    @server.tool(
        description=(
            "제약을 만족하는 문항 후보. 판별 트랙은 주어진 글자로 시퀀스를 "
            "만들고, 통글자 트랙은 단어 목록에서 고른다. 최근 출제된 것과 "
            "잠긴 깊이쌍은 빠진다. 후보가 0개로 나오는 것도 정상적인 답이다 — "
            "제약을 동시에 만족하는 조합이 없다는 뜻이다. "
            "letters 를 생략하면 이 손이 만들 수 있는 글자 전부를 쓴다."
        )
    )
    def get_item_bank(
        track: Literal[TRACKS],
        length: int,
        count: int,
        letters: Optional[List[str]] = None,
        exclude_recent: bool = True,
        learner_name: Optional[str] = None,
    ) -> dict[str, Any]:
        return client.invoke(
            "get_item_bank",
            f"{track} 후보 {count}개 요청",
            "GET",
            "/api/item-bank",
            params={
                "track": track,
                "length": length,
                "count": count,
                "letters": letters or None,
                "exclude_recent": exclude_recent,
                "learner_name": learner_name,
            },
            requested="".join(letters or []),
            summarize=lambda result: (
                f"후보 {len(result.get('candidates') or [])}개"
            ),
        )

    @server.tool(
        description=(
            "스테이션 상태. state 는 idle 또는 busy, locked_by 는 손을 쥐고 있는 "
            "세션 id (없으면 null), device_mode 는 mock 또는 serial."
        )
    )
    def get_station_status() -> dict[str, Any]:
        result = client.invoke(
            "get_station_status",
            "스테이션 상태 조회",
            "GET",
            "/api/device/status",
            summarize=lambda body: (
                f"{'busy' if body.get('busy') else 'idle'}, "
                f"락 {body.get('lock_holder')}"
            ),
        )
        if not result.get("ok"):
            return result
        return {
            "ok": True,
            "state": "busy" if result.get("busy") else "idle",
            "locked_by": result.get("lock_holder"),
            "device_mode": result.get("mode"),
        }

    # ------------------------------------------------------------------ #
    # 2층 · 세션 (되돌릴 수 있음)
    # ------------------------------------------------------------------ #

    @server.tool(
        description=(
            "훈련 세션을 시작하고 로봇손 락을 잡는다. 구동 도구는 세션이 "
            "있어야 쓸 수 있다. 다른 세션이 손을 쥐고 있으면 거부된다."
        )
    )
    def start_session(
        learner_name: str, plan: Optional[SessionPlanInput] = None
    ) -> dict[str, Any]:
        body: Dict[str, Any] = {"learner_name": learner_name, "source": "agent"}
        if plan is not None:
            for key, value in plan.model_dump().items():
                if value is not None:
                    body[key] = value
        return client.invoke(
            "start_session",
            f"{learner_name} 세션 시작",
            "POST",
            "/api/sessions",
            json_body=body,
            summarize=lambda result: (
                f"세션 {result.get('session_id')}, {result.get('total_items')}문항"
            ),
        )

    @server.tool(
        description=(
            "직전에 present_sequence 로 표현한 문항에 대한 학습자 응답을 "
            "채점한다. 오답 1회째에는 정답도 혼동 내역도 돌아오지 않는다 — "
            "같은 글자열을 present_sequence 로 다시 내보내야 한다."
        )
    )
    def submit_answer(session_id: int, answer: str) -> dict[str, Any]:
        return client.invoke(
            "submit_answer",
            f"세션 {session_id} 채점",
            "POST",
            f"/api/sessions/{session_id}/answer",
            json_body={"answer": answer},
            summarize=lambda result: str(result.get("result")),
        )

    @server.tool(
        description=(
            "세션을 닫고 락을 놓고 요약을 돌려준다. 요약은 **트랙별로만** "
            "나온다. 손은 안전 자세로 돌아간다."
        )
    )
    def end_session(session_id: int) -> dict[str, Any]:
        return client.invoke(
            "end_session",
            f"세션 {session_id} 종료",
            "POST",
            f"/api/sessions/{session_id}/end",
            summarize=lambda result: (
                f"세션 {result.get('session_id')} 요약"
            ),
        )

    # ------------------------------------------------------------------ #
    # 3층 · 구동 (안전 게이트)
    # ------------------------------------------------------------------ #

    @server.tool(
        description=(
            "글자열을 로봇손으로 표현한다. 채점 대상이 되므로 그다음 "
            "submit_answer 가 이 글자열로 채점한다. **손이 끝날 때까지 "
            "돌아오지 않는다.** hold_ms 는 글자당 유지 시간이며 생략하면 "
            "스테이션 기본값을 쓰고, 범위를 벗어나면 잘린 값이 응답에 담긴다. "
            "boundary=explicit 은 글자 사이에 중립 자세를 넣어 경계를 보여 주고, "
            "direct 는 글자에서 글자로 바로 넘어간다."
        )
    )
    def present_sequence(
        session_id: int,
        letters: str,
        track: Literal[TRACKS],
        boundary: Literal[BOUNDARIES],
        hold_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        return client.invoke(
            "present_sequence",
            f"세션 {session_id} 에 {len(letters)}글자 표현",
            "POST",
            f"/api/sessions/{session_id}/sequence",
            json_body={
                "letters": letters,
                "track": track,
                "boundary": boundary,
                "hold_ms": hold_ms,
            },
            requested=letters,
            summarize=lambda result: (
                f"문항 {result.get('index')}/{result.get('total')} 표현 완료"
            ),
        )

    @server.tool(
        description=(
            "글자 **하나**를 시범으로 보여 준다. 채점하지 않고 기록하지 않으며 "
            "진행 중인 문항도 건드리지 않는다. 두 글자의 차이를 보여 주려면 "
            "두 번 부른다. **손이 끝날 때까지 돌아오지 않는다.**"
        )
    )
    def demo_letter(
        session_id: int, letter: str, hold_ms: Optional[int] = None
    ) -> dict[str, Any]:
        return client.invoke(
            "demo_letter",
            f"세션 {session_id} 에 {letter} 시범",
            "POST",
            f"/api/sessions/{session_id}/demo-letter",
            json_body={"letter": letter, "hold_ms": hold_ms},
            requested=letter,
            summarize=lambda result: f"{result.get('letter')} 시범 완료",
        )

    return server


def _stats_summary(result: Dict[str, Any]) -> str:
    """로그 패널에 띄울 한 줄. **트랙별로** 센다."""
    by_track = result.get("by_track") or {}
    parts = [
        f"{track} {values.get('answered', 0)}문항"
        for track, values in by_track.items()
    ]
    return ", ".join(parts) if parts else "기록 없음"


# --------------------------------------------------------------------------- #
# 진입점
# --------------------------------------------------------------------------- #

def _configure_logging() -> None:
    """**stderr 로만** 로그를 낸다.

    stdout 은 MCP 프로토콜 전용이다. 한 줄이라도 섞이면 클라이언트가 JSON-RPC
    파싱에 실패하고, 증상은 "도구가 하나도 안 보인다" 로 나타난다.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _configure_logging()
    # `.env` 는 진입점에서만 읽는다 (DECISIONS.md §43). STATION_URL 이 여기서
    # 올라온다.
    load_env()

    server = create_server()

    if "--list-tools" in args:
        # stdio 로 띄우지 않고 도구 목록만 확인하는 길. 연동 전에 스키마가
        # 제대로 나오는지 보기 위한 것이다.
        import asyncio

        for tool in asyncio.run(server.list_tools()):
            print(f"{tool.name}({', '.join(tool.input_schema.get('properties', {}))})")
        return 0

    logger.info(
        "spellhand MCP server on stdio; station at %s",
        os.environ.get(STATION_URL_ENV) or DEFAULT_STATION_URL,
    )
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
