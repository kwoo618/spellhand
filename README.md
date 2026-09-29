# spellhand

5서보 로봇손이 영어 지문자(ASL 알파벳)를 만들고, 학습자는 그 손을 **읽는** 연습을 하는
훈련 스테이션이다. 캡스톤 과제로, 하드웨어는 전기공학 전공이, 이 저장소(소프트웨어·AI)는
컴퓨터소프트웨어 전공이 맡았다.

## 목차

1. [이 프로젝트가 무엇인가](#1-이-프로젝트가-무엇인가)
2. [빠른 시작 — 5분 안에 Mock 으로](#2-빠른-시작--5분-안에-mock-으로)
3. [웹 화면 전체 설명](#3-웹-화면-전체-설명)
4. [MCP — 이 프로젝트의 XAI 부분](#4-mcp--이-프로젝트의-xai-부분)
5. [하드웨어 연결](#5-하드웨어-연결)
6. [문제 해결](#6-문제-해결)
7. [하면 안 되는 것](#7-하면-안-되는-것)
8. [프로젝트 구조](#8-프로젝트-구조)
9. [개발](#9-개발)

---

## 1. 이 프로젝트가 무엇인가

> 📷 **사진 자리** — 로봇손 실물 사진과 `station.html` 홈 화면 캡처가 들어갈 자리다. 아직 없다.

- **Arduino UNO R4 WiFi** 에 물린 5서보 텐던 로봇손이 ASL 지문자를 한 글자씩 만든다.
- 학습자는 손을 **읽는** 연습을 한다. 만드는 연습이 아니다. 손 모양은 금방 외우지만 남이
  만든 지문자를 읽는 능력은 누군가 내 앞에서 만들어 줘야만 연습할 수 있고, 그 상대를
  로봇이 대신한다 ([기획서 1장](docs/지문자_학습_로봇손_기획서_v3.md)).
- **영어 전용이다.** 한글 지화는 이 5자유도 손으로 표현할 수 없어 제외했다(손목 움직임이
  필요하다 — 기획서 12.3).
- 지원 글자는 **11자: B D E F I L O S V W Y.** 손가락마다 굽힘 축 하나뿐이고 엄지 대향과
  손가락 벌림이 없어서, 손목 궤적이 필요한 글자(G H J Q Z)와 벌림으로 갈리는 구분(U/V)이
  빠졌다. 글자 목록은 `config/poses.json` 에만 있다.

훈련 문항은 두 **트랙**으로 나뉘고 통계를 절대 합치지 않는다.

| 트랙 | 문항 예 | 훈련하는 것 |
|---|---|---|
| 판별 (`discrimination`) | 뜻 없는 글자열 `VWB` | 손 모양 판별 |
| 단어 (`word`) | 영단어 `WISE` | 통글자 읽기 — 철자 지식으로 짐작이 가능해서 판별과 섞으면 지표가 오염된다 |

난이도는 유지 시간(0.8~3초) · 글자 수(3~7) · 글자 경계(또박또박/연결) 세 축이고,
학습자·트랙별로 다음 세션에 이어진다. 오답 1회면 같은 문항을 천천히 다시 보여 주고,
2회면 정답과 헷갈린 글자를 나란히 시범한다.

---

## 2. 빠른 시작 — 5분 안에 Mock 으로

하드웨어 없이 모든 화면이 돈다. **Windows · PowerShell 기준**, 명령은 전부 저장소 루트에서 친다.

**준비물:** Python **3.12** (`py -0p` 로 목록에 3.12 가 보여야 한다), Git.

```powershell
git clone https://github.com/kwoo618/spellhand.git
cd spellhand
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**`.venv` 안의 `python.exe` 를 직접 부른다.** 이 README 의 모든 명령이 그 전제다.
전역 `python` 에는 의존성이 없다.

### `.env` — Mock 이면 만들지 않아도 된다

파일이 없으면 `DEVICE_MODE=mock`, API 키 없음으로 뜬다. 만들 때는 저장소 루트에 `.env`
로 둔다 (`.gitignore` 에 있다). **형식만 보인다 — 실제 키를 README·커밋·채팅에 적지 않는다.**

```
DEVICE_MODE=mock
# 실기일 때만 (5장)
# SERIAL_PORT=COM3
# 계획 생성에 LLM 을 쓸 때만. 없으면 규칙 폴백으로 계획이 나온다
# ANTHROPIC_API_KEY=sk-ant-...여기에-본인-키...
```

| 변수 | 값 | 기본 |
|---|---|---|
| `DEVICE_MODE` | `mock` \| `serial` | `mock` |
| `SERIAL_PORT` | `COM3` 등. `serial` 이면 필수 | 없음 |
| `SERIAL_BAUD` | 통신 속도 | `115200` |
| `ANTHROPIC_API_KEY` | 계획 생성용 | 없음 → 규칙 폴백 |
| `AGENT_MODEL` | 계획 생성 모델 | `claude-opus-5` |
| `STATION_URL` | MCP 서버가 부를 스테이션 주소 | `http://127.0.0.1:8000` |

셸에서 준 값(`$env:DEVICE_MODE="mock"`)이 `.env` 보다 우선한다.

### 띄우기

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.routes:app --port 8000
```

브라우저로 <http://127.0.0.1:8000/> 을 연다 (`/station.html` 로 넘어간다).

### 잘 됐는지 확인

1. 기동 로그 둘째 줄이 **`device adapter: mock`** 이다. 실기인지 Mock 인지는 이 줄로만 판단한다.
   ```
   INFO server.env: no .env at ...\.env; using the environment as it is
   INFO server.device: device adapter: mock
   ```
2. 화면 왼쪽 위에 **`11자`**, 가운데에 **"지문자 읽기 훈련"** 이 보인다.
3. 다른 PowerShell 에서 `curl.exe http://127.0.0.1:8000/api/device/status` →
   `"mode":"mock"` 이 들어 있다.
4. Mock 에는 실물 손이 없다. 손 모양을 보려면 [교사 모드] → **[모의 손 화면 (새 창)]** 을
   먼저 띄운다 (3장).

`station.db` 는 이 명령을 친 폴더에 처음 기동할 때 생긴다.

---

## 3. 웹 화면 전체 설명

화면은 `web/station.html` **한 장**이고 **학습자 모드 / 교사 모드** 두 모드가 있다. 모의 손만
별도 창이다. 예전 주소 `/practice.html` · `/teacher.html` 은 이 화면으로 넘어간다.

**학습자 모드에는 혼동쌍·깊이쌍이 나오지 않는다.** "V 와 W 를 헷갈린다" 를 알고 풀면 V 가
나올 때마다 W 를 의심하게 되어 재려는 판별력이 오염된다 ([DECISIONS §52](DECISIONS.md)).
그것은 교사 모드의 혼동 행렬에서 본다.

### 학습자 모드

탭 줄: **홈 · 글자 · 훈련 · 내 진행**, 오른쪽 끝에 작은 **[교사 모드]**.

#### 홈
처음 여는 화면. 무엇을 하는 곳인지, 처음이면 **글자 → 훈련 → 내 진행** 순서로 가라는 안내,
세 메뉴 버튼, "훈련은 이렇게 진행됩니다" 네 줄, 이 손이 만드는 글자 타일이 있다. 선생님이
승인한 세션이 열려 있으면 여기서도 알린다.

#### 글자 (공부)
**글자를 눌러 그 손 모양을 보는 곳. 채점도 기록도 없다.** 처음이거나 모양을 다시 볼 때 쓴다.

| 버튼 | 하는 일 |
|---|---|
| [글자 보기 시작] | 이름을 넣고 누른다. 로봇손을 잡는다 (훈련과 같은 락) |
| 글자 카드 (파랑) | 누르면 손이 그 자세를 만든다. 목록은 서버가 `poses.json` 에서 준다 |
| 글자가 아닌 자세 (초록) | **중립**(훈련에서 글자 사이에 지나가는 자세)과 **쉬는 자세**. 둘은 다른 자세다 |
| [멈추기] | 손이 움직이는 동안만 보인다. 손을 멈춘다 |
| [글자 보기 마치기] | 정상 종료. 손을 놓는다 |

손이 움직이는 동안에는 화면이 **어두워지고** 상태 한 줄만 남는다 — 화면이 아니라 손을 보라는
뜻이다. 혼동쌍 힌트는 여기에도 없다 ([§56](DECISIONS.md)).

#### 훈련
**시작 화면**
- **답 입력 방법** 셋 중 하나: **타자**(자판) · **버튼**(글자 단추를 차례로) · **끌어 놓기**
  (글자 조각을 답 칸으로). 세션 중에는 바꿀 수 없다 — 섞이면 응답 시간이 두 방법의 혼합이 된다.
- **이름** 칸 → [세션 시작]. 기록은 이 이름으로 남고, 내 진행에서 같은 이름으로 본다.
- 선생님이 승인한 세션이 있으면 이름 칸 대신 **[시작] / [나중에]** 가 나온다. [나중에] 는
  세션을 살려 둔 채 빠져나오고, "세션 N 으로 참여하기" 버튼으로 돌아올 수 있다.

**문항** — 상태 배지가 차례를 알려 준다.

| 단계 | 화면 | 할 일 |
|---|---|---|
| 손을 보세요 | 어두운 화면, 입력 숨김 | 손을 본다. [멈추기] 로 멈출 수 있다 |
| 확인 중 | | 기다린다 |
| 답할 차례 | 입력이 열린다 | 읽은 글자를 넣고 **[제출]**. **[다시 보기]** 로 한 번 더 볼 수 있다 |
| 결과 | 정답·내 답을 나란히 | **[다음 문항]**. 두 번 틀렸으면 **[시범 다시 보기]** 도 있다 |
| 멈춤 | "정지했습니다" | [다시 보기] 로 이어서 본다 |

버튼·끌어 놓기 방식의 글자 단추는 문항과 무관하게 **표현 가능한 글자 전부**다.
[한 글자 지우기] · [모두 지우기] 가 있다.

**요약** — 세션이 끝나면 **판별 / 단어 표가 따로** 나온다. 합친 수치는 없다. 한 번 틀려 다시
본 문항은 두 번 답한 것으로 센다. 순서 오류율(글자는 맞고 순서만 바뀐 비율), 평균 응답(손이
멈춘 뒤 답할 때까지)이 있다. [처음으로] 로 돌아간다.

#### 내 진행
이름을 넣고 [보기].
- **누적** — 트랙별 답한 횟수와 정확도
- **지금 난이도** — 다음 세션이 시작할 유지 시간·글자 수·경계. "(처음)" 은 기록이 없다는 뜻
- **깊이쌍 단계** — 비슷한 손 모양이 굽힘 깊이로만 달라지는 짝이 문항에 나오기 시작하는
  단계. 최근 판별 문항 정확도가 기준을 넘으면 열린다. **어느 글자인지는 적지 않는다**
- **세션별 정확도** — 트랙별로 따로

#### 맨 아래 [세션 끝내고 손 놓기]
**복구 수단이다.** 손이 멈춘 채 풀리지 않거나 "다른 사람이 쓰고 있다" 며 시작이 안 될 때만
쓴다. 로봇손 락이 실제로 잡혀 있을 때만 보이고, 진행 중인 세션이 `aborted` 로 끝나므로
**8초 안에 두 번**("정말 끝냅니다 — 한 번 더") 눌러야 실행된다. [멈추기]는 손만 멈추고 세션을
살려 두는 비상 정지라 둘은 다르다.

### 교사 모드

학습자 모드 오른쪽 끝 [교사 모드] 또는 <http://127.0.0.1:8000/station.html?mode=teacher>.
**세션 중에는 잠긴다** ("세션 중에는 잠깁니다"). 돌아갈 때는 [학습자 모드로]. 인증은 없다 —
실습실 PC 한 대에서 쓰는 로컬 도구다.

#### 로봇손 상태 카드
손이 안 움직이면 여기부터 본다. 세 줄:
- **연결** — "실제 로봇손" 또는 "모의 장치 (하드웨어 없음)"
- **펌웨어 대조** — "펌웨어 설정 일치" / "불일치" / "대조 없음 — 모의 장치에는 펌웨어가 없습니다" /
  "확인 전". 불일치면 "전기팀에 펌웨어 재업로드를 요청하세요" 가 크게 뜬다
- **쓰는 사람** — "지금 로봇손을 쓰는 사람 없음" 또는 "세션 N 이 로봇손을 쓰는 중"

**[기술 정보 — 전기팀·개발자용]** 을 펼치면 원래 값(`mode`, 캘리브레이션 해시,
`calibration_hash_state`, `lock_holder`)이 나온다. 해시는 전기팀과 `CAL_HASH` 를 맞춰 볼 때 쓴다.
이 카드는 교사 모드 진입·로그 연결·승인/거부/손 놓기 때만 다시 읽는다.

#### 계획 만들기 · 승인
1. **학습자 이름**(학습자 화면과 같은 이름이어야 그 기록으로 계획한다), **요청**(평소 말로,
   예: "V와 W가 계속 헷갈린다고 한다…"), **판별 / 단어 문항 수**를 넣는다.
2. **[계획 생성]** — 모델을 부르면 20초 안팎. 모델을 쓸 수 없거나 검증에 두 번 실패하면
   **규칙 폴백**으로 만든다. **이 단계는 세션도 락도 만들지 않는다.**
3. 화면에 **만든 계획**(계획 번호 · 만든 방법 · 모델에 물은 횟수 · 예상 시간), 근거, 문항 표,
   **검증 결과**(한국어 문장 + 아래 작은 줄에 검사 이름·원문)가 나온다.
4. **[승인 — 세션 시작]** — 여기서 처음으로 세션이 생기고 로봇손을 잡는다. **학습자가 화면
   앞에 있을 때 누른다.** 학습자가 60초 안에 [시작] 을 누르지 않으면 락이 만료되어 세션이
   사라진다 — 고치지 않기로 한 설계다 ([§50.5](DECISIONS.md)).
   **[거부]** — 계획을 버린다. 세션은 만들어지지 않는다.
5. 승인이 막히면 할 일이 첫 줄, 거부 코드와 서버 원문이 아래 작은 줄에 나온다. 락이 물린
   경우에만 [세션 끝내고 손 놓기] 가 함께 나온다.

#### 계획 표의 각 열

| 열 | 뜻 |
|---|---|
| # | 문항 순서 |
| 트랙 | 판별 / 단어 |
| 글자 | 손이 보여 줄 글자열 |
| 유지 | 손이 한 글자를 보여 주는 시간 ("글자당 3.0초") |
| 경계 | **또박또박** = 글자 사이에 중립 자세, **연결** = 곧바로 이어짐 |

#### 혼동 행렬
모든 학습자의 기록을 합쳐, 로봇손이 보여 준 글자를 무엇으로 읽었는지 센 격자.
- **세로**(왼쪽) = 보여 준 글자, **가로**(위) = 학습자가 고른 답, 숫자 = 횟수
- **대각선**(하늘색) = 맞힌 횟수. **대각선 밖**(노란 칸) = 다른 글자로 읽은 횟수 —
  "V 를 W 로" 와 "W 를 V 로" 는 다른 칸이다
- **주황 테두리** = 설정의 혼동쌍 목록에 있는 칸. 한 번도 안 틀렸으면 0
- 글자 수가 다르거나 순서만 바뀐 답은 칸에 넣지 않고 격자 아래에 문항 수만 적는다
- **[판별] · [단어]** 로 트랙을 고른다. 합친 화면은 없다. 저절로 갱신되지 않으니 훈련 뒤
  **[새로고침]**

#### MCP 호출 로그 (실시간)
Claude Desktop 과 에이전트가 스테이션의 도구를 부를 때마다 한 줄씩, 최신이 위로 쌓인다.
줄 종류는 **도구 호출 / 호출 결과 / 계획·승인 / 차단**(붉은 줄, 거부 코드와 사유). 보기만
하는 패널이고 여기서 실행되는 것은 없다. 제목 옆 점과 문구가 실시간 연결 상태다. 예시는
[4장 (라)](#라-실제-사용-예시).

#### 모의 손 화면 (새 창)
탭 줄의 **[모의 손 화면 (새 창)]** 이 `mockhand.html` 을 따로 연다. 손가락별 flex 값과 자세를
그린다. 실물 손이 없을 때(Mock, 발표 폴백) 쓴다. 팝업이 막히면 주소창에
<http://127.0.0.1:8000/mockhand.html>. 교사 모드는 세션 중 잠기므로 **세션 전에** 띄워 둔다.
알려진 결함: 주먹 자세에서 손가락이 겹쳐 그려진다 (시각화만의 문제).

---

## 4. MCP — 이 프로젝트의 XAI 부분

### (가) 왜 있는가

- **LLM 은 글자 이름까지만 다룬다.** 각도·flex·전이 시간은 MCP 도구 스키마에도, 계획 출력
  스키마에도 **파라미터 자체가 없다.** 프롬프트로 금지한 것이 아니라 보낼 자리가 없다.
  회귀 테스트로 고정했다 (`tests/test_mcp.py::test_no_tool_takes_a_flex_or_angle_or_transition_parameter`,
  `tests/test_agent.py::test_the_output_schema_has_no_flex_or_angle_or_transition_field`).
- **도구는 3계층이다:** 조회(부작용 없음) / 세션(되돌릴 수 있음) / 구동(손이 움직임).
- **HITL:** 교사 모드에서 LLM 이 계획을 내도 **사람이 [승인] 하기 전에는 세션도 락도 없고
  손은 움직이지 않는다.** 승인된 계획의 글자열만 그대로 나간다.

**왜 이렇게 했는가.** 로봇손은 물리 장치다. 모델이 서보 각도를 직접 만들 수 있으면 잘못된
값 하나가 손가락을 허용 범위 밖으로 끌어 텐던과 서보를 망가뜨리고, 무엇이 왜 그렇게
움직였는지 나중에 설명할 수 없다. 그래서 모델에게는 "어떤 글자를, 어떤 순서로, 얼마나
오래" 까지만 주고, 글자 → flex → 각도 변환은 `config/poses.json` 과 서버가 한다. 모든 구동은
같은 안전 게이트(세션 락 → 글자 존재 → 커리큘럼 → flex clamp → 각도 clamp → rate limit →
장치 idle)를 지나고, 각도는 **서버와 펌웨어에서 이중으로** 잘린다. 무엇이 요청되고 무엇이
막혔는지는 교사 모드 로그에 그대로 남는다.

> ⚠️ **HITL 이 걸리는 경로를 정확히 적는다.** 승인 단계는 **교사 모드의 계획 경로**
> (`POST /api/agent/plan` → [승인])에 있다. **MCP 도구 9개에는 스테이션 쪽 승인 단계가 없다.**
> `start_session` 은 부르는 즉시 세션을 열고 락을 잡으며, `present_sequence` 는 LLM 이 준
> 글자열을 바로 구동한다(`/sequence` = "MCP·에이전트의 자유 구동", `server/routes/api.py`).
> MCP 경로를 막는 것은 승인이 아니라 **스키마(각도·flex 파라미터 없음)와 요청마다 도는 검사**다
> — 순서대로 `LOCK_NOT_HELD` → `PLAN_SESSION_RESTRICTED` → 세션 상태(`SESSION_STATE`·`DEVICE_BUSY`)
> → 안전 게이트 ①~⑦(`LOCK_NOT_HELD` · `UNSUPPORTED_LETTERS` · `CURRICULUM_VIOLATION` · flex·각도
> 이중 clamp · `RATE_LIMIT` · `SESSION_QUOTA` · `DEVICE_BUSY`). 조건은
> [기획서 6.5 의 "구현 시 달라진 점"](docs/지문자_학습_로봇손_기획서_v3.md) 표에 있다.
> MCP 로 계획을 만들거나 승인을 요청하는 도구는 없다.
>
> **승인된 계획의 세션은 계획대로만 움직인다.** 그 세션 id 로 `present_sequence` · `demo_letter`
> 를 부르면 `PLAN_SESSION_RESTRICTED`(409, JSON)로 거부된다. 계획 세션은 학습자 화면의 [시작]
> (`/planned`)으로만 나간다. 계획 세션이 아닌 세션(MCP `start_session`, 학습자 화면 직접 시작)은
> 지금처럼 자유 구동된다 ([DECISIONS §62](DECISIONS.md)).
>
> **로컬 전용 · 단일 사용자 전제.** 스테이션 REST 에는 인증이 없다. 그래서 MCP 가 아니어도
> **같은 기계에서 도는 무엇이든 요청 두 번**(`POST /api/sessions` → `POST /api/sessions/{id}/sequence`)
> 으로 손을 움직일 수 있다. 실습실 PC 한 대에서 한 사람이 쓰는 졸업작품 범위의 의도된 전제이고,
> 네트워크에 열어 두는 용도가 아니다. 서버는 `127.0.0.1` 에만 띄운다.

### (나) 도구 전체 목록

`mcp_server/server.py` 에 등록된 도구는 **9개**다 (`--list-tools` 와 MCP 클라이언트
`list_tools` 로 확인). 모든 도구는 실패를 예외가 아니라 `{"ok": false, "reason", "detail", "next_step"}`
로 돌려준다.

| 도구 | 계층 | 하는 일 | 받는 것 | 돌려주는 것 | 사람 승인 |
|---|---|---|---|---|---|
| `get_capabilities` | 조회 | 이 손의 능력 선언. **가장 먼저 부른다** | `learner_name?` | 글자 11자, 트랙, 혼동쌍(힌트 포함), 깊이쌍 해금 여부, 난이도 축, 전이 시간, 단어 수 | 없음 |
| `get_station_status` | 조회 | 스테이션 상태 | — | `state`(idle/busy), `locked_by`(세션 id 또는 null), `device_mode` | 없음 |
| `get_learner_stats` | 조회 | 학습자 성적, **트랙별로만** | `learner_name` | 트랙별 정확도·혼동·순서 오류율·유지 시간별 정확도·최근 문항. 없는 이름은 0 으로 | 없음 |
| `get_item_bank` | 조회 | 제약을 만족하는 문항 후보. 최근 출제·잠긴 깊이쌍 제외 | `track`, `length`, `count`, `letters?`, `exclude_recent?`, `learner_name?` | `candidates`, `excluded_recent`, `depth_pairs_unlocked`. 0개도 정상 | 없음 |
| `start_session` | 세션 | 세션을 열고 **로봇손 락을 잡는다** | `learner_name`, `plan?`(`discrimination_items`, `word_items`) | `session_id`, `total_items`, `state` | 없음 (즉시 락) |
| `submit_answer` | 세션 | 직전 구동 문항을 채점. 오답 1회째엔 정답을 주지 않는다 | `session_id`, `answer` | `result`(correct/wrong/order/…), `retry`, `finished`, `expected` | 없음 |
| `end_session` | 세션 | 세션 종료, 락 해제, 안전 자세 | `session_id` | 트랙별 요약, 트랙별 혼동 | 없음 |
| `present_sequence` | 구동 | 글자열을 손으로 표현 (채점 대상). 손이 끝날 때까지 기다린다 | `session_id`, `letters`, `track`, `boundary`(explicit/direct), `hold_ms?` | `index/total`, 실제로 쓰인 `hold_ms`(범위 밖이면 잘린 값), `transition_ms` | 없음 — 안전 게이트. 승인된 계획 세션에서는 거부 (`PLAN_SESSION_RESTRICTED`) |
| `demo_letter` | 구동 | 글자 **하나** 시범. 채점·기록 없음 | `session_id`, `letter`, `hold_ms?` | `letter`, `hold_ms`, `transition_ms` | 없음 — 안전 게이트. 승인된 계획 세션에서는 거부 (`PLAN_SESSION_RESTRICTED`) |

**어느 도구에도 각도·flex·전이 시간 파라미터가 없다.** `hold_ms`(글자당 유지 시간)가 LLM 이
줄 수 있는 유일한 시간 값이고, 설정 범위(800~3000ms)로 잘린다.

### (다) Claude Desktop 에 붙이는 법

**0. 스테이션 서버를 먼저 띄운다** (2장). MCP 서버는 스테이션 REST 의 클라이언트일 뿐이라,
서버가 없으면 모든 도구가 `station_unreachable` 을 돌려준다. MCP 서버는 Claude Desktop 이
직접 띄우므로 따로 터미널을 열 필요가 없다.

**1. 도구 목록이 나오는지 먼저 본다** (저장소 루트에서):

```powershell
.\.venv\Scripts\python.exe -m mcp_server.server --list-tools
```

9줄(`get_capabilities(learner_name)` … `demo_letter(session_id, letter, hold_ms)`)이 나와야 한다.

**2. 설정 파일을 연다.** 위치는 `%APPDATA%\Claude\claude_desktop_config.json` 이다.
Claude Desktop 의 설정 → 개발자 → 구성 편집으로 열거나, **PowerShell 에서** 이렇게 연다:

```powershell
notepad "$env:APPDATA\Claude\claude_desktop_config.json"
```

> ⚠️ 위 `notepad ...` 줄은 **PowerShell 에 치는 명령이다. 설정 파일 안에 붙여 넣지 않는다.**
> 파일 안에 이런 줄이 섞이면 JSON 이 깨져 Claude Desktop 이 서버를 하나도 읽지 못한다.
> 실제로 이렇게 깨진 적이 있다. 파일에는 아래 JSON 만 들어간다.

**3. `mcpServers` 에 `spellhand` 를 넣는다.** `<프로젝트 경로>` 두 곳을 저장소의
**절대 경로**로 바꾼다. JSON 안에서는 `\` 를 `\\` 로 두 번 쓴다 (예: `D:\\projects\\spellhand`).

```json
{
  "mcpServers": {
    "spellhand": {
      "command": "<프로젝트 경로>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "PYTHONPATH": "<프로젝트 경로>",
        "STATION_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

파일에 이미 다른 키(`preferences` 등)나 다른 서버가 있으면 **파일 전체를 덮어쓰지 말고**
`mcpServers` 안에 `"spellhand": {...}` 한 덩어리만 더한다. 쉼표를 빠뜨리지 않는다.

- **`PYTHONPATH` 가 필요한 이유.** Claude Desktop 은 이 프로세스를 저장소가 아니라 자기 작업
  폴더에서 띄운다. `-m mcp_server.server` 는 현재 폴더 기준으로 패키지를 찾으므로 저장소 루트를
  알려 주지 않으면 `ModuleNotFoundError: No module named 'mcp_server'` 로 바로 죽는다
  (저장소 밖에서 실행해 확인했다).
- **경로는 절대 경로로.** `.venv\...` 같은 상대 경로는 Claude Desktop 의 작업 폴더 기준으로
  풀려서 찾지 못한다. `command` 는 전역 `python` 이 아니라 `.venv` 의 것이어야 한다.

**4. Claude Desktop 을 완전히 종료하고 다시 연다.** 창만 닫으면 트레이에 남아 설정을 다시 읽지
않는다 — 트레이 아이콘에서 종료한다.

**5. 확인.** 설정 → 개발자에서 `spellhand` 가 실행 중인지, 대화 입력창의 도구(커넥터) 메뉴에
`spellhand` 가 켜져 있고 도구 9개가 보이는지 본다. 교사 모드를 열어 두고 Claude 에게
"spellhand 의 get_capabilities 를 불러 줘" 라고 하면 로그 패널에 `get_capabilities` 줄이 생긴다.

**연결이 안 될 때 볼 곳**
- `%APPDATA%\Claude\logs\mcp.log` 와 `mcp-server-spellhand.log` (있으면) — 서버가 죽은 이유가 여기 있다
- `ModuleNotFoundError` → `PYTHONPATH` 누락. `No such file` → `command` 경로 오타
- 도구가 하나도 안 보이는데 로그에 에러가 없다 → 설정 JSON 이 깨졌다 (2단계 경고)
- 도구는 보이는데 전부 `station_unreachable` → 스테이션 서버(uvicorn)가 안 떠 있거나 포트가 다르다

### (라) 실제 사용 예시

**무엇을 실제로 돌렸고 무엇이 TBD 인가** (2026-09-29, Mock)

| 부분 | 상태 |
|---|---|
| 교사 모드 계획 → 승인/거부 → 구동, 로그 | **실제 실행.** 새로 복제한 저장소(`.env` 없음)에서 REST 로 화면과 같은 호출을 보냈다. 키가 없어 계획은 **규칙 폴백**이다 |
| MCP 도구 9개의 호출·응답·로그 | **실제 실행.** Claude Desktop 과 같은 방식(stdio, 저장소 밖 작업 폴더 + `PYTHONPATH`)으로 MCP 서버를 띄우고 MCP 클라이언트로 불렀다 |
| Claude Desktop 에 친 문장과, 모델이 고른 호출 순서 | **TBD.** Claude Desktop 대화를 이 작업에서 돌리지 못했다. 필요한 것: 스테이션을 띄운 상태에서 Claude Desktop 으로 한 번 대화하고, 대화 화면과 교사 모드 로그를 캡처해 이 자리를 채운다 |
| LLM 이 만든 계획의 모습 (`source: llm`) | **실제 실행.** API 키가 있는 프로젝트 폴더의 서버(Mock, 스크래치 DB)에서 브라우저 교사 모드로 계획 1회 생성. 승인하지 않았다 |

#### ① 교사 모드 — 계획은 승인 전에 멈춘다 (HITL)

교사가 요청란에 이렇게 적고 [계획 생성] (학습자 "민지", 판별 3 · 단어 1):

> V와 W가 계속 헷갈린다고 한다. 그 둘을 집중적으로 보고 단어로 마무리해 줘.

돌아온 계획 (응답에서 발췌). **글자 이름·트랙·유지 시간·경계뿐이다.**

```json
{ "plan_id": 2, "status": "pending", "source": "rule", "session_id": null,
  "target_pairs": [["V","W"], ["W","B"]],
  "items": [
    { "track": "discrimination", "letters": "WVF", "hold_ms": 3000, "boundary": "explicit" },
    { "track": "discrimination", "letters": "WBW", "hold_ms": 3000, "boundary": "explicit" },
    { "track": "discrimination", "letters": "SVW", "hold_ms": 3000, "boundary": "explicit" },
    { "track": "word",           "letters": "WEB", "hold_ms": 3000, "boundary": "explicit" } ],
  "estimated_seconds": 78, "findings": [] }
```

**여기서 멈춘다.** `session_id` 가 `null` 이고, 이때 장치 상태는 `"lock_holder": null` —
아무도 손을 잡지 않았다. 교사 모드 로그:

```
계획 2 준비됨 — 승인 대기 (실행 안 됨)
규칙 폴백 · 문항 4개 · 예상 78초
```

**LLM 으로 만들었을 때** (기록 없는 새 학습자 "지유", 판별 3 · 단어 1, 모델 호출 1회, 2026-09-29).
교사 모드 화면에 나온 그대로다:

```
계획 1 · LLM · 시도 1회 · 예상 95초
```

| # | 트랙 | 글자 | 유지 | 경계 |
|---|---|---|---|---|
| 1 | 판별 | VWB | 글자당 3.0초 | 또박또박 |
| 2 | 판별 | BWV | 글자당 3.0초 | 또박또박 |
| 3 | 판별 | LDS | 글자당 3.0초 | 또박또박 |
| 4 | 단어 | OWL | 글자당 3.0초 | 또박또박 |

근거(모델이 쓴 `rationale`, 발췌): "지유는 아직 답한 문항이 없어 측정된 약점 데이터가 없다.
따라서 이번 세션은 기준선(baseline) 측정용으로 짠다. … 대상 쌍은 측정된 약점이 아니라 설정
파일의 기본 순서에 있는 V/W, W/B를 우선 사용하고, 판별 축을 넓히기 위해 엄지축(D/L)과
검지축(D/S)도 한 문항에 넣는다." 기록이 없는 학습자는 설정 순서의 기본 쌍으로 계획하도록 되어
있어서([DECISIONS §54](DECISIONS.md)), 요청에 글자를 적지 않아도 V·W 가 들어간다. 모델도
**글자 이름·트랙·유지 시간·경계만** 냈다. 검증 결과는 "확인할 것 없음"이고, 접힌 내부 정보에
경고 1건(예상 시간 95초 vs 스테이션 계산 78초)이 있었다. 로그:

```
계획 1 준비됨 — 승인 대기 (실행 안 됨)
LLM · 문항 4개 · 예상 95초
```

*뽑은 요청:* 요청 칸에 **"이번 세션 계획을 짜 줘."** (글자를 지정하지 않은 중립 문구. 요청 칸은
비워 둘 수 없다 — 화면과 서버가 둘 다 거부한다.)

교사가 **[승인 — 세션 시작]** 을 누르면 그제야 세션 2 가 생기고 `"lock_holder": 2`,
`"state": "awaiting_item"` 이 된다. 로그:

```
계획 2 승인됨 — 세션 2 시작
사람이 승인했다. 이제부터 손이 움직인다.
```

학습자 화면의 훈련 탭에 [시작] 이 나타나고, 누르면 **승인된 글자열 `WVF` 그대로** 손이
표현한다(`/api/sessions/2/planned` → `"index": 1, "total": 4, "remaining_items": 3`). 학습자가
틀리게 답하면 `"result": "wrong", "retry": true, "expected": null` — 정답은 알려 주지 않고
같은 문항을 다시 보여 준다.

**거부했을 때.** 계획 1 을 [거부] 하면 로그에 이것만 남고 세션은 생기지 않는다:

```
계획 1 거부됨
세션은 만들어지지 않았다.
```

거부한 계획을 다시 승인하려 해도 막힌다 — `409 {"code": "PLAN_STATE", "detail": "plan 1 is
already rejected; a decided plan cannot be decided again"}`.

#### ② MCP 도구로 직접 구동 — 막히는 것과 통과하는 것

아래는 MCP 클라이언트로 실제로 부른 순서와 응답이다 (발췌). Claude Desktop 에서 모델이 이
순서를 그대로 고른다는 뜻은 아니다 — 그 부분은 위 표의 TBD 다.

```
get_capabilities(learner_name="민지")
  → ok, letters [B D E F I L O S V W Y], confusion_pairs[{pair:[V,W], hint:"약지를 확인하세요. V는 굽힘, W는 폄"}, …]

present_sequence(session_id=1, letters="VWB", …)        ← 세션 없이 구동 시도
  → {"ok": false, "reason": "lock_not_held",
     "detail": "session 1 does not hold the device lock (holder=None); no motion is allowed without it",
     "next_step": "call start_session() first"}

start_session(learner_name="민지", plan={discrimination_items: 2, word_items: 0})
  → {"ok": true, "session_id": 1, "total_items": 2, "state": "awaiting_item"}

present_sequence(session_id=1, letters="CAT", …)        ← 이 손이 못 만드는 글자
  → {"ok": false, "reason": "unsupported_letters", "unsupported": ["A","C","T"],
     "supported": ["B","D","E","F","I","L","O","S","V","W","Y"],
     "next_step": "call get_item_bank() with supported letters only"}

present_sequence(session_id=1, letters="VWB", track="discrimination", boundary="explicit", hold_ms=5000)
  → {"ok": true, "index": 1, "total": 2, "hold_ms": 3000, "transition_ms": 900, "status": "presented"}
     ← 5000ms 를 요청했지만 상한 3000ms 로 잘린 값이 돌아온다

submit_answer(session_id=1, answer="VWB")  → {"result": "correct", "expected": "VWB"}
present_sequence(… letters="WVB" …)        → {"index": 2, "total": 2}
submit_answer(session_id=1, answer="VWB")  → {"result": "order", "retry": true, "expected": null}
demo_letter(session_id=1, letter="W")      → {"status": "demonstrated", "letter": "W", "hold_ms": 3000}
get_station_status()                       → {"state": "idle", "locked_by": 1, "device_mode": "mock"}
end_session(session_id=1)
  → by_track.discrimination: answered 2, correct 1, order_errors 1, accuracy 0.5 / word: answered 0
```

같은 호출이 교사 모드 로그에는 이렇게 남는다 (최신이 위지만 여기서는 시간순):

```
get_capabilities            능력 선언 조회
get_capabilities →          글자 11자, 단어 32개
present_sequence            세션 1 에 3글자 표현
차단: lock_not_held          present_sequence
                            session 1 does not hold the device lock (holder=None); …
start_session →             세션 1, 2문항
차단: unsupported_letters    present_sequence
                            letters ['A', 'C', 'T'] are not in poses; this hand can express [...]
present_sequence →          문항 1/2 표현 완료
submit_answer →             correct
submit_answer →             order
demo_letter →               W 시범 완료
end_session →               세션 1 요약
```

### (마) 웹만 쓸 때 vs MCP 까지 쓸 때

| | 웹만 | 웹 + MCP (Claude Desktop) |
|---|---|---|
| PowerShell | 1개 — `uvicorn` | 1개 — `uvicorn` (MCP 서버는 Claude Desktop 이 띄운다) |
| 브라우저 | `station.html` 1개. 교사·학습자를 동시에 보려면 탭 2개(`?mode=teacher`) | 교사 모드 1개 — 로그 패널을 보려고 |
| 모의 손 창 | Mock 이면 1개 (실물 손이 있으면 0) | Mock 이면 1개 |
| Claude Desktop | 없음 | 1개 |
| API 키 | 없어도 됨 (규칙 폴백) | 교사 모드 계획에만 쓰인다. MCP 도구는 키를 쓰지 않는다 |

---

## 5. 하드웨어 연결

> **`serial` 은 전기팀이 있을 때만 돌린다.** 헤더 생성과 펌웨어 업로드도 승인 항목이다
> ([CLAUDE.md](CLAUDE.md)).

| | `DEVICE_MODE=mock` | `DEVICE_MODE=serial` |
|---|---|---|
| 손 | 없음. 시간만 실제로 흐르고 `mockhand.html` 로 그린다 | UNO R4 WiFi 에 물린 실물 |
| 펌웨어 해시 대조 | 없음 ("대조 없음") | 연결 직후 필수 |
| 필요한 것 | 없음 | `SERIAL_PORT`(예: `COM3`), 속도 **115200** |

순서는 **헤더 → 업로드 → 서버**다.

```powershell
# 1) poses.json 에서 펌웨어 헤더 firmware/hand_controller/calibration.h 를 만든다
.\.venv\Scripts\python.exe tools\gen_calibration.py
# 2) 헤더가 poses.json 과 맞는지 확인 (보드와 통신하지 않는다)
.\.venv\Scripts\python.exe tools\gen_calibration.py --check
```

`gen_calibration.py` 가 만드는 `calibration.h` 에는 손가락별 각도 표·핀·중립/안전 자세와,
그것들의 SHA-256 앞자리인 **`CAL_HASH`** 가 들어간다. 생성 산출물이라 손으로 고치지 않고
커밋하지 않는다(`.gitignore`).

3) Arduino IDE 로 `firmware/hand_controller/hand_controller.ino` 를 열고, 보드
**Arduino UNO R4 WiFi**, 포트(예: COM3)를 골라 업로드한다. **업로드 뒤 시리얼 모니터를 닫는다**
— 열려 있으면 서버가 포트를 못 연다.

4) `.env` 를 `DEVICE_MODE=serial`, `SERIAL_PORT=COM3` 으로 두고 2장과 같은 명령으로 띄운다.
**`--reload` 를 붙이지 않는다** (같은 포트를 두 번 잡다가 죽는다). 기동 로그가
`device adapter: serial on COM3 @ 115200 baud` 여야 한다.

**해시가 안 맞으면 연결이 거부되고 서버가 뜨지 않는다. 건너뛰는 방법은 없다.** 서버는 연결
직후 펌웨어에 `H` 를 보내 `CAL_HASH` 를 받고 자기가 `poses.json` 으로 계산한 값과 대조한다.
다르면 `poses.json` 을 고치고 재업로드를 잊은 것이다 — 1~3단계를 다시 한다. 손이 의도와 다른
자세를 조용히 만드는 것을 막는 장치라 우회 옵션을 두지 않았다.

프로토콜(`P` 이동 · `N` 중립 · `Z` 비상 정지 · `H` 해시)은 [PRD 6.5·7장](docs/개발_PRD_v2.1.md).
PRD 에는 보드가 `Arduino Uno` 로 적혀 있지만 실제는 UNO R4 WiFi 다 — 핀은 `poses.json` 에서
오고 펌웨어는 보드를 모른다.

---

## 6. 문제 해결

| 증상 | 원인과 처리 |
|---|---|
| 시리얼 포트를 열 수 없다 / 8000 포트가 이미 쓰인다 | **남은 python 프로세스가 물고 있다.** 실제로 4개가 남아 COM 포트를 막은 적이 있다. 아래 명령으로 찾아 정리한다. 아두이노 IDE 시리얼 모니터도 닫는다 |
| 브라우저에 스테이션 화면 대신 `{"error":{"code":"not_found",...}}` 같은 다른 응답이 온다 | 다른 프로그램(개발 PC 에서는 Docker · WSL)이 `0.0.0.0:8000` / `[::1]:8000` 을 쥐고 있다. `localhost` 는 `::1` 로 풀려 그쪽으로 갈 수 있다. **주소는 `127.0.0.1` 로 쓴다.** 누가 쥐었는지는 `netstat -ano \| findstr :8000` |
| MCP 커넥터에 spellhand 가 안 보인다 | 4장 (다) "연결이 안 될 때 볼 곳". 대부분 깨진 JSON, `PYTHONPATH` 누락, 상대 경로, 완전 종료 안 함 |
| 서버가 기동 중 해시 불일치로 죽는다 | 펌웨어가 지금 `poses.json` 과 다르다. 5장 1~3단계 재실행. 건너뛸 수 없다 |
| 실기인데 손이 안 움직인다 | 기동 로그가 `device adapter: mock` 이다. `DEVICE_MODE` 오타이거나 셸의 `$env:DEVICE_MODE` 가 `.env` 를 덮었다 |
| 훈련·글자 보기가 시작되지 않는다 ("다른 사람이 쓰고 있다", `LOCK_UNAVAILABLE`) | 끝나지 않은 세션이 락을 쥐고 있다. 락은 마지막 신호 뒤 **60초**에 만료된다. 기다리거나 화면 아래 [세션 끝내고 손 놓기] (두 번), 또는 `curl.exe -X POST http://127.0.0.1:8000/api/device/reset`. 교사 모드 상태 카드의 "쓰는 사람" 줄로 누가 쥐었는지 본다 |
| 승인했는데 세션이 사라졌다 | 승인 뒤 60초 안에 학습자가 [시작] 을 누르지 않았다. 설계대로다. 계획을 다시 만들어 학습자가 화면 앞에 있을 때 승인한다 |
| 교사 화면에 영어 코드(`LOCK_UNAVAILABLE` 등)와 원문이 작은 글씨로 뜬다 | **일부러 남긴 것이다.** 첫 줄의 한국어 문장이 할 일이고, 아래 줄은 교사가 옮겨 적어 개발자에게 전하라고 남긴 원문이다. 학습자 화면에는 영어·코드가 나오지 않는다 |

**남은 python 프로세스 찾기와 정리.** `.venv` 의 `python.exe` 는 실제 인터프리터를 자식으로
띄우므로 **서버 하나가 두 줄로 보인다.** 둘 다 정리한다.

```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Select-Object ProcessId, ParentProcessId, CommandLine

Stop-Process -Id <ProcessId>
```

`uvicorn` 과 `tools\pilot.py` 가 포트를 쥐는 프로세스다. `mcp_server.server` 는 Claude Desktop
이 띄운 것이고 시리얼 포트를 열지 않는다.

---

## 7. 하면 안 되는 것

- **`config/poses.json` 의 값을 바꾸지 않는다.** 전기팀 측정값이다. 바꾸려면 전기팀 확인 →
  헤더 재생성 → 재업로드까지 함께 간다.
- **코드에 각도·flex 리터럴을 쓰지 않는다.** 하드웨어 값은 `poses.json` 에만 있다.
  `scripts/verify.ps1` 이 `server/` 를 검사한다.
- **`station.db` 를 직접 열어 고치거나 지우지 않는다.** 마이그레이션 스크립트로만.
- **`tools/pilot_key_*.txt` 를 커밋하지 않는다.** 제3자 판별 파일럿의 정답지다. 참가자가 보면
  파일럿이 무효가 된다 (`.gitignore` 에 있다).
- **`.env` 를 커밋하지 않는다.** API 키가 들어 있다.
- `firmware/hand_controller/calibration.h` 를 손으로 고치지 않는다. `gen_calibration.py` 로만.

---

## 8. 프로젝트 구조

```
config/poses.json        11자 자세·캘리브레이션·핀·타이밍·혼동쌍 — 하드웨어 값의 유일한 출처
config/words.json        단어 트랙 목록 (지원 글자로 자동 필터)
server/                  스테이션 서버 (FastAPI)
  routes/                REST + WebSocket, 정적 파일
  device/                DeviceAdapter · Mock · Serial (해시 핸드셰이크)
  safety.py              안전 게이트 ①~⑦, 세션 락
  session.py study.py    훈련 세션 / 글자(공부) 세션
  agent.py               LLM 계획 생성, 검증 10항목, HITL, 규칙 폴백
  calibration.py         flex ↔ 각도, 이중 clamp, CAL_HASH
  items.py judge.py stats.py db.py env.py config.py
mcp_server/server.py     MCP 서버 (stdio). 스테이션 REST 의 얇은 클라이언트
web/station.html         학습자·교사 두 모드 한 화면
web/mockhand.html        모의 손 시각화 (새 창)
firmware/hand_controller/ 아두이노 펌웨어 (calibration.h 는 생성물)
tools/gen_calibration.py poses.json → calibration.h, --check
tools/pilot.py           제3자 판별 파일럿 (임시 도구, 세션·DB 를 거치지 않음)
scripts/verify.ps1       완료 판정
tests/                   pytest
docs/                    기획서 v3, 개발 PRD v2.1(기준), 하드웨어 측정 요청서
DECISIONS.md             설계 결정 기록 §1~§62 — 무엇을 왜 그렇게 정했는가. 문서와 코드가 어긋나 보이면 여기를 먼저 본다
CLAUDE.md                작업 규칙과 현재 상태
```

---

## 9. 개발

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify.ps1
```

`verify.ps1` 은 세 가지를 하고 하나라도 실패하면 완료가 아니다.

1. **`pytest` 전체** — clamp · 세션 락 · 캘리브레이션 해시 테스트는 최소 방어선이라 약화시키지 않는다
2. **각도 리터럴 검사** — `server/` 의 `.py` 에 서보 각도로 보이는 숫자가 있으면 실패 (R1)
3. **캘리브레이션 해시** — `gen_calibration.py --check`. `calibration.h` 가 `poses.json` 과 맞는지.
   갓 복제한 저장소에는 헤더가 없어 `skip: ... has not been generated yet` 으로 나오고 통과한다

테스트만 돌릴 때: `.\.venv\Scripts\python.exe -m pytest -q`.
API 를 실제로 부르는 테스트는 기본으로 건너뛴다. 한 번 돌려 보려면:

```powershell
$env:SPELLHAND_LIVE_LLM="1"
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -k live -s
```

남은 일(제3자 판별 파일럿, 실데이터 수집, 시연 리허설)과 알려진 한계는
[CLAUDE.md](CLAUDE.md) "현재 상태" 와 [기획서](docs/지문자_학습_로봇손_기획서_v3.md)에 있다.
