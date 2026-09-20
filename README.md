# spellhand

지문자(fingerspelling) **수용** 훈련용 로봇손 스테이션.

로봇손이 지문자를 한 글자씩 만들어 보이면 학습자가 그것을 읽어 답을 입력한다.
학습자가 손을 만드는 것이 아니라 **읽는 것**을 훈련한다. 오답 패턴을 모아 다음
세션의 문항을 구성하는 AI 에이전트가 붙어 있다.

캡스톤 과제로 만들었다. 로봇손 하드웨어는 전기공학 전공이, 소프트웨어와 AI
계층은 컴퓨터소프트웨어 전공이 맡았다. 이 저장소는 소프트웨어 쪽이다.

---

## 왜 수용 훈련인가

지문자는 수어 대화에서 합의된 수어가 없는 개념, 전문 용어, 인명·지명, 강조,
음성언어와의 코드 스위칭에 쓰인다. 정보 밀도가 높은 부분을 담당한다.

**병목은 표현이 아니라 읽기다.** 알파벳 손 모양 자체는 짧은 시간에 익힌다.
어려운 것은 상대가 표현한 지문자를 읽어내는 수용 능력이고, 통역사 양성 과정에서
"가장 먼저 배우지만 가장 마지막에 숙달되는 기술"로 불린다.

**그런데 수용 훈련은 혼자 할 수 없다.** 누군가가 나를 향해 표현해 주어야
성립한다. 기존 도구는 영상이나 웹 3D 아바타라 화면 각도에 갇혀 있고, 물리적
연습 상대는 사실상 없다. 로봇손은 그 자리를 메우려는 것이다 — 실습실에 비치해
두고 취약 학습자 개별 훈련과 정기 진단에 쓰는 **공용 스테이션**이다.

자세한 배경과 근거는 [기획서 1장](docs/지문자_학습_로봇손_기획서_v3.md)에 있다.

---

## AI가 하는 일 — 세 층위

이 프로젝트에서 "AI가 한다"는 말은 세 가지 다른 것을 뜻한다. 섞으면 안 된다.

| 층위 | 무엇이 하는가 | 무엇을 정하는가 |
|---|---|---|
| **① 세션 계획 생성** | LLM (Claude) | 글자 이름, 트랙, 유지 시간, 글자 경계 |
| **② 도구 통로** | MCP 서버 | 아무것도 정하지 않는다. 호출을 기존 계층으로 넘긴다 |
| **③ 문항별 적응** | **규칙** (`RuleAdaptation`) | 정답·오답에 따른 다음 문항의 난이도 |

### ① 세션 계획 생성 (LLM)

"V와 W가 계속 헷갈린다고 한다. 그 둘을 집중적으로 보고 단어로 마무리해 줘" 같은
자연어 요청을 받아, 학습자의 트랙별 기록·혼동 통계·현재 난이도를 보고 이번
세션의 문항 목록을 만든다. 출력은 JSON 하나다.

```json
{ "rationale": "...",
  "target_pairs": [["V", "W"]],
  "items": [ { "track": "discrimination", "letters": "VWB",
               "hold_ms": 3000, "boundary": "explicit" } ],
  "estimated_seconds": 124 }
```

**LLM이 정할 수 있는 것은 이 네 필드뿐이다.** `server/agent.py` 의
`PlannedItem` 에 다른 필드가 없고, 출력 스키마에도 없다.

프롬프트에 글자 목록을 적어 두지 않는다. 지원 글자·혼동쌍·단어 목록·난이도
범위는 전부 실행 시점에 `config/poses.json` 과 `config/words.json` 에서 읽어
넣는다. 그래서 문자 세트가 바뀌면 설정 파일만 고치면 된다.

### ② MCP 서버 (도구 통로)

[MCP](https://modelcontextprotocol.io) 로 도구 9개를 노출한다. Claude Desktop
에서 직접 로봇손을 부릴 수 있다. 부작용 크기에 따라 3층으로 나눴다.

| 층 | 도구 | 부작용 |
|---|---|---|
| 1층 · 조회 | `get_capabilities` `get_learner_stats` `get_item_bank` `get_station_status` | 없음 |
| 2층 · 세션 | `start_session` `submit_answer` `end_session` | 되돌릴 수 있음 |
| 3층 · 구동 | `present_sequence` `demo_letter` | 손이 움직인다 |

거부는 예외가 아니라 구조화된 응답으로 돌아간다. LLM이 다음에 무엇을 할지 알 수
있어야 하기 때문이다.

```json
{ "ok": false, "reason": "unsupported_letters", "requested": "CAT",
  "unsupported": ["A", "C", "T"],
  "supported": ["B","D","E","F","I","L","O","S","V","W","Y"],
  "next_step": "call get_item_bank() with supported letters only" }
```

MCP 서버는 얇은 어댑터다. 스테이션 서버의 REST API를 부르는 클라이언트이고,
구동은 기존 라우트를 그대로 지난다 — 안전 게이트를 우회할 코드가 없다.
([DECISIONS §46](DECISIONS.md))

### ③ 문항별 적응 (규칙)

정답이면 유지 시간을 0.85배로 줄이고, 바닥을 치면 시퀀스 길이나 글자 경계를
올린다. 오답이면 되돌린다. **이것은 LLM이 하지 않는다.** 문항마다 API를
호출하면 학습자가 답한 뒤 다음 문항까지 수초를 기다려야 하고, 네트워크 장애가
계획 실패에서 세션 진행 실패로 번진다. 교체 가능한 인터페이스로 분리해 두었지만
교체 계획은 아니다. ([DECISIONS §47](DECISIONS.md))

---

## LLM이 각도를 만들 수 없다

이 프로젝트의 설계 핵심이다. 로봇손은 물리 장치이고, LLM이 서보 각도를 생성하는
경로가 있으면 잘못된 값 하나가 손을 부순다. 그래서 **그 경로를 아예 만들지
않았다.**

**1. 도구 스키마에 flex·각도·전이 시간 파라미터가 없다.** 프롬프트로 금지한
것이 아니라 파라미터 자체가 없다. LLM이 각도를 지정하려 해도 보낼 자리가 없다.
중첩 모델까지 훑는 회귀 테스트로 고정해 두었다 —
`tests/test_mcp.py::test_no_tool_takes_a_flex_or_angle_or_transition_parameter`,
`tests/test_agent.py::test_the_output_schema_has_no_flex_or_angle_or_transition_field`.

**2. 각도는 설정 파일에서만 나온다.** 글자 이름 → flex → 각도 변환은
`config/poses.json` 과 `server/calibration.py` 가 한다. 서버 코드에 서보 각도
리터럴이 하나도 없고, `scripts/verify.ps1` 이 매번 그것을 검사한다.

**3. 모든 구동 요청이 같은 안전 게이트를 지난다.**

```
연결 시점 → ⑧ 캘리브레이션 해시 대조        (불일치면 연결 거부 = 서버 기동 실패)
요청      → ① 세션 락 → ② 글자 존재 → ③ 커리큘럼 → ④ flex clamp
          → ⑤ 각도 clamp → ⑥ rate limit → ⑦ 디바이스 idle → 전송
```

clamp는 flex(0.0~1.0)와 각도 양쪽에 이중으로 걸고, 펌웨어도 한 번 더 자른다.

**4. HITL 승인 없이는 실행되지 않는다.** 계획 생성(`POST /api/agent/plan`)은
세션도 만들지 않고 락도 잡지 않는다. 사람이 승인해야 세션이 생기고, 승인된 계획의
문항만 `POST /api/sessions/{id}/planned` 로 나갈 수 있다.

**5. 검증 10항목을 서버가 자동으로 돌린다.** 지원하지 않는 글자가 있으면 재생성
(최대 2회), 그래도 안 되면 규칙 기반 폴백으로 내려간다. 범위 밖 유지 시간은
자르고, 잠긴 깊이쌍이나 최근 출제 중복은 제거한다. 제약 검사는 문항 생성기가
쓰는 것과 **같은 함수**다 (`server/items.py` 의 `*_violations()`).

---

## 훈련 방식

### 두 트랙을 섞지 않는다

| 트랙 | 문항 | 훈련하는 것 |
|---|---|---|
| `discrimination` (판별) | 뜻 없는 글자 시퀀스 (`VWB`) | 손 모양 판별 |
| `word` (통글자) | 실제 영단어 (`WISE`) | 통글자 읽기 |

**두 트랙의 통계는 절대 합치지 않는다.** 단어 문항의 정답은 철자 지식으로
보정된다 — `W_SE` 까지만 읽어도 `WISE` 를 맞힐 수 있다. 같은 표에 넣으면 판별
정확도가 오염된다. `server/stats.py` 의 모든 집계 함수는 `track` 을 **필수
인자**로 받고, 기본값도 없고 두 트랙을 합치는 함수도 없다.

### 난이도 3축

| 축 | 파라미터 | 범위 | 훈련하는 것 |
|---|---|---|---|
| ① 유지 시간 | `hold_ms` | 800~3000ms | 순간 판별 |
| ② 시퀀스 길이 | 글자 수 | 3~7 | 작업 기억 |
| ③ 글자 경계 | `boundary` | `explicit` / `direct` | 경계 탐지 |

**난이도는 세션을 넘어 이어진다.** 학습자·트랙별로 마지막 도달 지점을 저장하고
다음 세션이 거기서 시작한다. 이월이 없으면 10세션을 해도 매번 3글자에서 다시
시작하고, 반복 훈련이라는 모델 자체가 성립하지 않는다.

### 오답 처리

1. **오답 1회** → 같은 문항을 느린 전이·최대 유지·또박또박으로 **다시 보여 준다.**
   정답도 혼동 내역도 알려주지 않는다. 알려주면 재생이 의미를 잃는다.
2. **오답 2회** → 헷갈린 두 글자를 나란히 **시범**하고 힌트를 보여 준 뒤 난이도를
   내린다. 힌트 문구는 `poses.json` 의 혼동쌍에 있다 (예: "약지를 확인하세요.
   V는 굽힘, W는 폄").

### 중립 자세가 글자 경계다

연속된 글자를 그냥 이어 붙이면 어디서 한 글자가 끝났는지 알 수 없다. `explicit`
모드에서는 글자 사이에 **중립 자세**를 넣어 경계를 눈으로 보이게 한다.

중립 자세(중지만 0.70)와 안전 자세(전 손가락 0.00)는 **별개다.** 중립은 보여주기
위한 것이라 11자 어느 것과도 2개 손가락 이상 달라야 하고, 안전은 손을 쉬게 하기
위한 것이라 텐던 장력이 가장 낮아야 한다. 서로 대체하지 않는다.

### 커리큘럼

굽힘 깊이로만 갈리는 세 쌍(E/I, O/S, E/O)은 판별 난도가 높아 처음에는 출제하지
않는다. 최근 20문항 정확도가 0.80 이상이 되면 해금된다. D는 혼동쌍 6개에 걸쳐
있어 한 세션 내 출현 비율에 상한을 둔다.

---

## 구조

```
spellhand/
├── config/
│   ├── poses.json          11자 자세·캘리브레이션·핀·타이밍·혼동쌍 — 하드웨어 종속값 전부
│   └── words.json          통글자 트랙 단어 목록 (문자 집합으로 자동 필터)
├── server/
│   ├── config.py           설정 로딩·검증. 스키마 위반이면 서버가 뜨지 않는다
│   ├── calibration.py      flex ↔ 서보 각도, 이중 clamp, CAL_HASH 계산
│   ├── device/
│   │   ├── __init__.py     Pose·DeviceAdapter 인터페이스, 어댑터 팩토리
│   │   ├── mock.py         Mock 손. 실제로 시간을 소비하고 시간을 주입받는다
│   │   └── serial.py       시리얼 어댑터. 접속 시 해시 핸드셰이크
│   ├── safety.py           안전 게이트 ①~⑦, 세션 락
│   ├── session.py          세션 진행(단계형 상태 기계), 규칙 기반 적응
│   ├── judge.py            판정 — 정답/혼동/길이 오류/순서 오류
│   ├── items.py            문항 생성 + 제약 검사. DB를 보지 않는다
│   ├── stats.py            트랙별 통계. 모든 함수가 track 필수
│   ├── agent.py            LLM 계획 생성, 검증 10항목, HITL 저장소, 규칙 폴백
│   ├── db.py               SQLite 스키마
│   ├── env.py              .env 로딩 (진입점에서 한 번만)
│   └── routes/
│       ├── __init__.py     앱 조립, WebSocket
│       ├── api.py          REST 라우트
│       └── events.py       이벤트 팬아웃
├── mcp_server/server.py    MCP 서버 (stdio). 스테이션 REST의 클라이언트
├── web/
│   ├── practice.html       학습자 화면
│   ├── teacher.html        진단 뷰 — 혼동 행렬, 계획 승인, MCP 호출 로그
│   └── mockhand.html       Mock 손 시각화 (개발·폴백)
├── firmware/hand_controller/
│   ├── hand_controller.ino 아두이노 펌웨어
│   └── calibration.h       ← 생성 산출물. 커밋하지 않는다
├── tools/gen_calibration.py  poses.json → calibration.h 생성 + --check
├── scripts/verify.ps1      완료 판정 — 테스트 + 각도 리터럴 검사 + 해시 대조
└── tests/                  523개
```

### `poses.json` 단일 출처

11자의 flex 값, 손가락별 서보 각도, 서보 가동범위, 핀 번호, 타이밍, 혼동쌍,
커리큘럼 임계값이 **전부 이 파일 하나에만** 있다. 서버 코드에도 펌웨어 소스에도
복제본이 없다.

같은 표가 두 곳에 있으면 언젠가 한쪽만 고친다. 특히 위험한 형태는 *`poses.json`
의 각도를 고치고 서버는 재시작했는데 펌웨어 재업로드를 잊는* 경우다. 서버가
의도한 자세와 손이 실제로 만드는 자세가 조용히 달라지고, 그 오차는 채점 로그
어디에도 남지 않는다. 학습자는 잘못된 자세를 보고 답하게 된다.

그래서 `scripts/verify.ps1` 이 `server/` 안의 각도 리터럴을 매번 검사한다.
flex 값은 `0.0`(완전히 폄) ~ `1.0`(안전 최대 굽힘) 정규화 값이고 서보 각도가
아니다.

### 캘리브레이션 해시

펌웨어가 쓰는 각도 표는 `tools/gen_calibration.py` 가 `poses.json` 에서
`calibration.h` 로 생성한다. 그 헤더에는 캘리브레이션·핀·중립·안전 자세의
SHA-256 앞자리(`CAL_HASH`)가 박힌다.

서버는 기동 시 같은 방식으로 해시를 계산하고, 시리얼 접속 직후 `H` 명령으로
펌웨어 해시를 받아 대조한다. **불일치면 연결 자체를 거부하고 서버가 뜨지
않는다.** 요청 단위로 검사하지 않는 이유는, 게이트는 첫 구동 요청에서야 도는데
기동 시 나가는 안전 자세는 그 전에 이미 손으로 나가기 때문이다.

`calibration.h` 는 `.gitignore` 에 있다. 커밋해 두면 재생성하지 않고도 빌드가
통과해 버린다.

---

## 실행

### 설치

Python 3.12 기준이다. 저장소 루트에서:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**`.venv` 안의 인터프리터를 직접 호출한다.** 이 저장소의 모든 명령이 그 전제로
적혀 있다. `Activate.ps1` 로 활성화해도 되지만, 새 셸에서는 직접 호출이 안전하다.

의존성 파일이 두 개인 이유: `pyproject.toml` 은 **선언**(무엇이 필요한가),
`requirements.txt` 는 `pip freeze` **고정본**(무슨 버전이 깔려 있는가)이다.

### Mock 으로 돌리기

하드웨어 없이 전 계층이 동작한다. 이것이 기본 모드다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.routes:app --port 8000
```

| 화면 | 주소 |
|---|---|
| 학습자 | <http://127.0.0.1:8000/practice.html> |
| 진단 | <http://127.0.0.1:8000/teacher.html> |
| Mock 손 | <http://127.0.0.1:8000/mockhand.html> |

학습자 화면에서 이름을 넣고 시작하면 판별 8문항 + 단어 4문항 세션이 돈다.
`mockhand.html` 을 나란히 열어 두면 손가락 flex 값이 실시간으로 보인다 — 어느
자세인지는 브라우저가 flex 로 되짚는다.

**세션을 끝내지 않고 탭을 닫으면 다음 시작이 `LOCK_UNAVAILABLE` 로 막힌다.**
한 번에 한 세션만 손을 쓸 수 있고, 락은 마지막 heartbeat 로부터 60초 뒤에
만료된다. 기다리지 않으려면 학습자 화면 맨 아래의 **[세션 끝내고 손 놓기]** 를
누르거나(진행 중인 세션이 날아가므로 두 번 눌러야 실행된다):

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/device/reset
```

이것은 진행 중인 세션을 `aborted` 로 닫고 락을 놓는다. 손만 멈추는
`POST /api/device/stop`(비상 정지, 세션 유지)과 결과가 다르다.

**기동 로그에 어느 어댑터로 떴는지 남는다.** 실기인지 Mock 인지 헷갈리지 않으려면
이 줄을 본다.

```
INFO server.env: loaded ...\.env: applied ['SERIAL_PORT'], kept existing ['DEVICE_MODE']
INFO server.device: device adapter: mock
```

### 환경변수

기동 시 저장소 루트의 `.env` 를 읽는다. 파일이 없으면 기본값으로 뜬다.
`.env` 는 커밋되지 않는다.

| 변수 | 값 | 기본 |
|---|---|---|
| `DEVICE_MODE` | `mock` \| `serial` | `mock` |
| `SERIAL_PORT` | `COM3` 등. `serial` 일 때 **필수** | 없음 |
| `SERIAL_BAUD` | 통신 속도 | `115200` |
| `ANTHROPIC_API_KEY` | 계획 생성용. 없으면 규칙 폴백으로 동작 | 없음 |
| `AGENT_MODEL` | 계획 생성 모델 | `claude-opus-5` |
| `STATION_URL` | MCP 서버가 부를 스테이션 주소 | `http://127.0.0.1:8000` |

**셸에서 준 값이 `.env` 보다 우선한다.** 파일을 고치지 않고 한 번만 다르게 띄울
때 쓴다.

```powershell
$env:DEVICE_MODE="mock"
.\.venv\Scripts\python.exe -m uvicorn server.routes:app --port 8000
```

`DEVICE_MODE` 를 알아볼 수 없으면 경고를 남기고 `mock` 으로 뜬다. 오타로 Mock 이
뜬 것을 손 고장으로 오해하지 않도록 로그를 확인할 것.

### 실기로 연결

> **전기팀 입회 없이 실기를 구동하지 않는다.** 저장소 규칙이다
> ([CLAUDE.md](CLAUDE.md) 승인 항목).

```powershell
# 1) poses.json 에서 펌웨어 헤더를 생성한다
.\.venv\Scripts\python.exe tools\gen_calibration.py

# 2) firmware/hand_controller 를 보드에 업로드 (Arduino IDE)

# 3) 해시가 맞는지 먼저 확인한다
.\.venv\Scripts\python.exe tools\gen_calibration.py --check

# 4) serial 로 띄운다
$env:DEVICE_MODE="serial"; $env:SERIAL_PORT="COM3"
.\.venv\Scripts\python.exe -m uvicorn server.routes:app --port 8000
```

- 해시가 불일치하면 **서버가 뜨지 않는다.** 1~2단계를 다시 한다
- **`--reload` 를 쓰지 않는다.** 파일이 바뀔 때마다 새 프로세스가 같은 포트를
  열려다 실패한다
- 포트가 다른 프로그램(아두이노 IDE 시리얼 모니터 등)에 잡혀 있으면 기동에
  실패한다
- 진단 뷰 헤더에 모드·해시·일치 여부가 한 줄로 나온다

### Claude Desktop 연동

스테이션 서버를 먼저 띄운 뒤, `%APPDATA%\Claude\claude_desktop_config.json` 에:

```json
{
  "mcpServers": {
    "spellhand": {
      "command": "D:\\projects\\robot_hand\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "PYTHONPATH": "D:\\projects\\robot_hand",
        "STATION_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

경로는 실제 저장소 위치로 바꾼다. **`PYTHONPATH` 가 필수다** — Claude Desktop 은
자기 작업 디렉터리로 프로세스를 띄우므로 이것이 없으면 모듈을 찾지 못한다.

도구 목록만 먼저 확인하려면:

```powershell
.\.venv\Scripts\python.exe -m mcp_server.server --list-tools
```

스테이션 서버가 떠 있지 않으면 모든 도구가 `station_unreachable` 구조화 실패를
돌려준다. `teacher.html` 을 열어 두면 Claude Desktop 의 도구 호출이 실시간으로
지나가는 것이 보인다 — 차단된 호출은 사유와 함께 붉게 표시된다.

### 테스트

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

세 가지를 한다: `pytest` 전체(523개), `server/` 안의 서보 각도 리터럴 검사(R1),
`calibration.h` 해시 대조. 하나라도 실패하면 완료가 아니다.

API를 호출하는 테스트는 기본 실행에서 건너뛴다. 실제 호출로 계획을 하나
생성해 보려면:

```powershell
$env:SPELLHAND_LIVE_LLM="1"
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -k live -s
```

---

## 하드웨어

5자유도 텐던 구동 로봇손 1대. 손가락당 굽힘 축 하나씩이고, **엄지 대향과 손가락
벌림이 없다.** 그래서 손목 궤적을 요구하는 글자(G H J Q Z)와 벌림으로 갈리는
구분(U/V)은 표현할 수 없다. 남은 **11자(B D E F I L O S V W Y)** 로 훈련한다.

| 항목 | 값 |
|---|---|
| 보드 | Arduino UNO R4 WiFi |
| 서보 | 5개 (엄지·검지·중지·약지·소지) |
| 핀 | 2, 3, 6, 8, 10 — `poses.json` 의 `pins` 블록 |
| 통신 | USB 시리얼 115200 baud |
| 보간 | 20ms 틱 선형 보간 |
| 전이 시간 | 900ms (오답 재생 시 1800ms) |
| 유지 시간 | 800~3000ms |

### 시리얼 프로토콜

flex는 부동소수점 파싱을 피하려고 **0~1000 정수(퍼밀)** 로 보낸다.

| 방향 | 명령 | 뜻 |
|---|---|---|
| 서버 → 펌웨어 | `P <seq> <t> <i> <m> <r> <p> <trans> <hold>` | 이동·유지 |
| | `N` | 중립 자세 |
| | `Z` | 비상 정지 (즉시 안전 자세) |
| | `H` | 해시 조회 |
| 펌웨어 → 서버 | `ACK <seq>` | 수신 |
| | `RDY <seq>` | 목표 도달 — **관찰 시작 시점** |
| | `DON <seq>` | 유지 종료 |
| | `HSH <hash>` | 해시 |
| | `ERR <seq> <code>` | `EPARSE` `ERANGE` `EBUSY` `ETIME` `ESEQ` |

펌웨어는 글자를 모른다. 퍼밀 5개만 받고, 범위 밖 값은 자르지 않고 거부한다.
`seq` 는 서버가 붙이는 단조 증가 정수이고 상위 계층은 그 존재를 모른다.

전체 사양은 [PRD 6.5·7장](docs/개발_PRD_v2.1.md)에 있다.

---

## 현재 상태와 한계

소프트웨어는 완료 상태다. Mock 과 실기 양쪽에서 세션이 끝까지 돌고, MCP·에이전트·
진단 뷰가 동작한다. 테스트 523개.

### 아직 검증되지 않은 것

**11자 세트가 제3자 판별 검증을 통과하지 않았다.** 제작팀이 육안으로 11자 전부
구분됨을 확인했지만, **만든 사람은 자기가 무엇을 만들었는지 알고 본다.** 사전
지식 없는 제3자에게 무작위 제시하고 식별률을 측정하는 것이 남아 있다. 기준
미달인 글자가 나오면 자세를 재조정하거나 세트를 줄인다 — 그때 고치는 것은
`poses.json` 뿐이고 훈련 루프는 그대로 성립한다.

**학습 전이가 미검증이다.** 로봇손 훈련이 실제 사람의 지문자를 읽는 능력으로
이어지는지는 이 프로젝트 범위에서 확인하지 못한다. 특히 D·E·F·O는 로봇이 굽힘
깊이로 단서를 만들지만 사람은 엄지 위치로 구분한다.

### 미해결 항목

- **세션 안에서 ②·③축이 거의 발동하지 않는다.** 유지 시간 하한이 800ms로
  내려가면서 ①축의 단계가 9개로 늘어, 전부 맞히는 학습자도 12문항 세션에서
  1~2단까지만 오른다. 감쇠 계수나 단 전환 조건을 조정할지는 실측 오답률을 보고
  정한다 ([DECISIONS §39](DECISIONS.md))
- **E의 약지 값 확인 대기.** `ring: 0.57` 이 실기에서 덜 접혀 보인다. 하드웨어팀
  회신을 기다린다. `poses.json` 수치는 하드웨어팀 확정값이라 임의로 바꾸지 않는다
- 재캘리브레이션 주기, 30분 연속 구동 발열 — 하드웨어팀 확인 대기

### 구조적 한계

장비 1대, 세션당 약 5분, 준비·정리 포함 인당 7~8분이다. 시간당 8명 내외가
상한이므로 학급 전체 상시 훈련용이 아니다. **취약 학습자 집중 훈련과 정기 진단**
용도다. 결함이 아니라 제품 포지션이다.

11자는 단시간에 암기 가능하다. 혼동쌍 15개로 훈련 가치를 확보했지만, 난이도 축을
모두 소진한 학습자에게 추가로 줄 것이 없다. 전체 알파벳은 엄지 대향축과 벌림축이
있는 상위 하드웨어의 목표다 — 그때 바뀌는 것은 하드웨어 종속층뿐이고 훈련 루프·
MCP·에이전트·안전 게이트는 그대로 쓴다. 그것이 이 설계의 목표였다.

---

## 문서

| 문서 | 내용 |
|---|---|
| [기획서 v3](docs/지문자_학습_로봇손_기획서_v3.md) | 왜 이렇게 만드는지. 설계 의도, 한계, 검증 계획 |
| [개발 PRD v2.1](docs/개발_PRD_v2.1.md) | 구현 사양. 절 번호로 참조한다 |
| [DECISIONS.md](DECISIONS.md) | 설계 결정 48건. "무엇을 정했고 왜 그렇게 정했는가" |
| [CLAUDE.md](CLAUDE.md) | 작업 규칙과 현재 상태 |
| [하드웨어 측정 요청서](docs/하드웨어_측정_요청서.md) | 하드웨어팀 회신값의 출처 |

구현이 문서와 어긋나 보이면 `DECISIONS.md` 를 먼저 본다. 문서 작성 후 대화에서
바뀐 결정이 있고 근거가 거기 있다.
