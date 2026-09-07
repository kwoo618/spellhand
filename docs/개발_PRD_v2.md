# 지문자 수용 훈련 로봇손 — 개발 PRD
 
> **버전:** 2.0
> **작성일:** 2026-09-03
> **상위 문서:** 지문자_학습_로봇손_기획서_v3.md (기획 확정본)
> **개발자:** 최강우 (1인)
> **기간:** 5주 (2026-09-03 ~ 2026-10-05)
> **하드웨어 상태:** 제작 완료. 자세·타이밍 실측 완료. 실기 결합은 1~2주차.
> **소프트웨어 상태:** Phase 0(기반)·Phase 1(훈련 루프)이 v1 사양으로 완료됨. **v2의 1~2주차는 신규 개발이 아니라 v3 사양으로의 마이그레이션이다.**
 
---
 
## 0. v1 대비 변경 요약
 
| 항목 | v1 | v2 | 근거 (기획서 v3) |
|---|---|---|---|
| 문자 세트 | A급 7자 채점 + B급 4자 시범 | **11자 전부 채점** (B D E F I L O S V W Y). 등급 개념 삭제 | 2.2 |
| 자세값 | ASL 추정치 | **실측 확정값** | 2.2, 부록 A |
| 혼동쌍 | 5쌍 | **15쌍**, 판별 축 태그 + 시범 힌트 문구 포함 | 2.3 |
| 굽힘 깊이 쌍 | — | E/I, O/S, E/O는 **정확도 조건 충족 후 도입** | 2.3, 4.1 |
| 훈련 단위 | 글자 시퀀스만 | **판별 트랙 + 통글자 트랙**, 통계 분리 | 3.3, 4.2 |
| 전이 시간 | 자세 쌍별 500~2000ms 선형 | **900ms 고정 (하한)**. 느린 재생 시에만 늘림 | 3.4 |
| 유지 시간 | 400~3000ms | **2000~3000ms**. 하한의 성격은 확인 중 | 3.2, 9.1 |
| 중립 자세 | 전 손가락 0.0 | **중지만 0.70** | 3.5 |
| 안전 자세 | 중립과 동일 | **분리.** 전 손가락 0.0. 부팅·타임아웃·비상정지 전용 | 3.5 |
| 펌웨어 자세 테이블 | `PROGMEM` 복제 | **복제 금지.** 서버가 flex 값을 전송 | 7.3 |
| 시리얼 프로토콜 | `P <letter> ...` | **`P <seq> <flex×5> ...`** 글자 개념이 펌웨어에서 사라짐 | 7.3 |
| 캘리브레이션 일치 검증 | 불가능 | **해시 핸드셰이크.** 펌웨어 헤더를 `poses.json`에서 생성하고 접속 시 해시 대조 | 7.3 (신규 해법) |
| 안전 게이트 ③ | 등급 검사 | **트랙·깊이쌍 게이팅 검사**로 대체 | 2.3, 3.3 |
| 판정 | 길이 불일치만 예외 | + **애너그램은 순서 오류**로 분리 기록 | 통계 오염 방지 |
| 시연 ⑥ 차단 사례 | LLM 각도 지정·flex 1.4 | **실제 발생 가능한 사례로 재구성.** 스키마 차단은 설명 슬라이드 | 11.1 재검토 |
| 시연 ① 훅 | `V-W-B` | `WISE`. **E 탈락 대비 폴백 단어 지정** | 11 |
| 일정 | 6주, Phase 0부터 | **5주, 마이그레이션부터.** 제3자 판별 파일럿 1주차로 전진 | 13 |
 
---
 
## 1. 개발 원칙
 
| 원칙 | 내용 |
|---|---|
| **하드웨어 종속 격리** | 하드웨어에 의존하는 값은 전부 `config/poses.json` 한 파일에 모은다. 코드에 각도 리터럴을 쓰지 않는다 |
| **단일 출처** | 자세 테이블은 서버에만 존재한다. 펌웨어는 캘리브레이션 테이블만 가지며, 그것도 `poses.json`에서 생성한다 |
| **Mock 우선** | 모든 기능은 Mock 디바이스로 완성한다. 실기 연결은 어댑터 교체로 끝나야 한다 |
| **LLM 격리** | LLM 출력은 글자 이름·난이도 파라미터·트랙까지다. flex·각도·시리얼 명령을 LLM이 생성하지 않는다 |
| **안전 우선** | clamp는 서버와 펌웨어 양쪽에 중복 구현한다. 안전 범위를 절대 확장하지 않는다. 중복을 제거하지 않는다 |
| **문제 우선** | 범위 확장 전에 발견된 문제를 먼저 해결한다 |
| **시연 역산** | 기획서 11장 시연 시나리오에 나오지 않는 기능은 만들지 않는다 |
 
---
 
## 2. 기술 스택
 
v1과 동일. 변경 없음.
 
| 계층 | 선택 |
|---|---|
| 언어 | Python 3.11+ |
| 웹 | FastAPI (REST + WebSocket) |
| DB | SQLite + SQLAlchemy 2.x |
| 시리얼 | pyserial, 115200 baud |
| MCP | `mcp` Python SDK, stdio |
| LLM | Anthropic API, 구조화 출력 |
| 프론트 | 단일 HTML + vanilla JS |
| 펌웨어 | Arduino C++, `Servo.h` |
| 테스트 | pytest |
 
**의도적으로 쓰지 않는 것:** Docker, React/빌드 도구, Redis, Celery, Postgres, 인증 라이브러리.
 
---
 
## 3. 프로젝트 구조
 
```
spellhand/
├── config/
│   ├── poses.json              ← 하드웨어 종속 값 전부 (유일)
│   └── words.json              ← 통글자 트랙 단어 목록 (문자 집합으로 자동 필터)
├── tools/
│   └── gen_calibration.py      ← poses.json → firmware/calibration.h 생성
├── firmware/
│   └── hand_controller/
│       ├── hand_controller.ino
│       └── calibration.h       ← 생성 산출물. 수기 편집 금지
├── server/
│   ├── main.py
│   ├── config.py               poses.json / words.json 로딩·검증
│   ├── db.py
│   ├── calibration.py          flex → angle, 양방향 clamp
│   ├── device/
│   │   ├── base.py
│   │   ├── serial_device.py
│   │   └── mock_device.py
│   ├── safety.py               SafetyGate, rate limit, 락
│   ├── session.py              세션 진행·적응
│   ├── judge.py                판정·혼동·순서 오류
│   ├── items.py                문항 생성 (2트랙, 규칙 기반)
│   ├── stats.py                혼동 통계 (트랙 분리)
│   ├── agent.py
│   └── routes/
│       ├── practice.py
│       ├── teacher.py
│       └── ws.py
├── mcp_server/
│   └── server.py
├── web/
│   ├── practice.html
│   ├── teacher.html            (선택. MCP 로그 패널은 필수)
│   └── mockhand.html           Mock 시각화 (개발·폴백)
├── tests/
└── station.db
```
 
---
 
## 4. 설정 파일
 
### 4.1 `config/poses.json`
 
**하드웨어 교체 시 유일하게 바뀌는 산출물.** 아래 값은 실측 확정값이다 (하드웨어팀 회신 2026-09-01, D 중지값 09-03 정정).
 
```json
{
  "schema_version": 2,
  "device_id": "hand-01",
  "dof": { "flexion": 5, "opposition": 0, "abduction": 0 },
  "fingers": ["thumb", "index", "middle", "ring", "pinky"],
 
  "calibration": {
    "thumb":  { "open": 180, "closed": 80  },
    "index":  { "open": 0,   "closed": 120 },
    "middle": { "open": 0,   "closed": 125 },
    "ring":   { "open": 0,   "closed": 105 },
    "pinky":  { "open": 180, "closed": 80  }
  },
 
  "timing": {
    "transition_ms": 900,
    "transition_slow_ms": 1800,
    "hold_min_ms": 2000,
    "hold_max_ms": 3000,
    "hold_default_ms": 3000,
    "interpolation_tick_ms": 20,
    "observe_from": "arrival",
    "settle_ms": 0,
    "idle_timeout_ms": 30000,
    "_note": "transition/hold는 실측. hold_min의 성격(정착/부하)은 미확인. 정착 시간으로 확정되면 observe_from을 settled로 바꾸고 settle_ms를 채운 뒤 hold_min을 내린다"
  },
 
  "poses": {
    "B": { "thumb": 0.70, "index": 0.00, "middle": 0.00, "ring": 0.00, "pinky": 0.00 },
    "W": { "thumb": 0.70, "index": 0.00, "middle": 0.00, "ring": 0.00, "pinky": 1.00 },
    "V": { "thumb": 0.70, "index": 0.00, "middle": 0.00, "ring": 1.00, "pinky": 1.00 },
    "F": { "thumb": 0.70, "index": 0.83, "middle": 0.00, "ring": 0.00, "pinky": 0.00 },
    "D": { "thumb": 0.70, "index": 0.00, "middle": 0.80, "ring": 0.76, "pinky": 0.70 },
    "L": { "thumb": 0.00, "index": 0.00, "middle": 1.00, "ring": 1.00, "pinky": 1.00 },
    "I": { "thumb": 0.70, "index": 0.83, "middle": 0.80, "ring": 0.76, "pinky": 0.00 },
    "Y": { "thumb": 0.00, "index": 1.00, "middle": 1.00, "ring": 1.00, "pinky": 0.00 },
    "O": { "thumb": 0.70, "index": 0.83, "middle": 0.80, "ring": 0.76, "pinky": 0.70 },
    "E": { "thumb": 0.90, "index": 0.50, "middle": 0.48, "ring": 0.57, "pinky": 0.40 },
    "S": { "thumb": 0.70, "index": 1.00, "middle": 1.00, "ring": 1.00, "pinky": 1.00 }
  },
 
  "neutral": { "thumb": 0.00, "index": 0.00, "middle": 0.70, "ring": 0.00, "pinky": 0.00 },
  "safe":    { "thumb": 0.00, "index": 0.00, "middle": 0.00, "ring": 0.00, "pinky": 0.00 },
 
  "confusion_pairs": [
    { "pair": ["V","W"], "axis": "count", "depth": false, "hint": "약지를 확인하세요. V는 굽힘, W는 폄" },
    { "pair": ["W","B"], "axis": "count", "depth": false, "hint": "소지를 확인하세요. W는 굽힘, B는 폄" },
    { "pair": ["B","F"], "axis": "count", "depth": false, "hint": "검지를 확인하세요. B는 폄, F는 굽힘" },
    { "pair": ["I","Y"], "axis": "thumb", "depth": false, "hint": "엄지를 확인하세요. I는 굽힘, Y는 폄" },
    { "pair": ["D","L"], "axis": "thumb", "depth": false, "hint": "엄지를 확인하세요. D는 굽힘, L은 폄" },
    { "pair": ["I","S"], "axis": "pinky", "depth": false, "hint": "소지를 확인하세요. I는 폄, S는 굽힘" },
    { "pair": ["I","O"], "axis": "pinky", "depth": false, "hint": "소지를 확인하세요. I는 폄, O는 굽힘" },
    { "pair": ["D","S"], "axis": "index", "depth": false, "hint": "검지를 확인하세요. D는 폄, S는 굽힘" },
    { "pair": ["D","O"], "axis": "index", "depth": false, "hint": "검지를 확인하세요. D는 폄, O는 굽힘" },
    { "pair": ["D","E"], "axis": "index", "depth": false, "hint": "검지를 확인하세요. D는 폄, E는 반쯤 굽힘" },
    { "pair": ["D","V"], "axis": "middle_ring", "depth": false, "hint": "중지·약지를 확인하세요. D는 굽힘, V는 폄" },
    { "pair": ["E","F"], "axis": "middle_ring", "depth": false, "hint": "중지·약지를 확인하세요. E는 굽힘, F는 폄" },
    { "pair": ["E","I"], "axis": "depth", "depth": true,  "hint": "굽힘 깊이를 보세요. E는 얕게, I는 깊게" },
    { "pair": ["O","S"], "axis": "depth", "depth": true,  "hint": "굽힘 깊이를 보세요. O는 중간, S는 끝까지" },
    { "pair": ["E","O"], "axis": "depth", "depth": true,  "hint": "굽힘 깊이를 보세요. E는 얕게, O는 중간" }
  ],
 
  "curriculum": {
    "depth_pairs_unlock": { "window_items": 20, "min_accuracy": 0.80 },
    "heavy_letters": { "D": 0.5 },
    "_note": "depth 쌍은 최근 20문항(깊이쌍 제외)에서 정확도 0.80 이상일 때 도입. heavy_letters는 해당 글자 포함 문항 비율 상한"
  }
}
```
 
**로딩 시 검증 (서버 기동 실패 조건)**
- `poses`, `neutral`, `safe`의 모든 flex 값이 `0.0 ≤ x ≤ 1.0`, 손가락 5개 전부 존재
- `calibration`의 open/closed가 0~180
- `confusion_pairs`의 모든 글자가 `poses`에 존재, 쌍 중복 없음
- `neutral`이 모든 글자와 **손가락 2개 이상** 차이 (기획서 3.5 근거를 코드로 강제)
- `safe`의 모든 값이 0.0 (텐던 최저 장력 자세)
- `transition_ms ≤ transition_slow_ms`, `hold_min_ms ≤ hold_default_ms ≤ hold_max_ms`
- `observe_from == "settled"`이면 `settle_ms > 0`
### 4.2 `config/words.json`
 
```json
{
  "words": ["YES","OWL","OLD","ODE","ELF","WEB","LID","DIE","VIE","FIB",
            "LOVE","WISE","BODY","WOLF","IDLE","BOLD","SIDE","WIDE","LOSE","SILO","DOVE",
            "FIELD","SOLID","VIDEO","BLESS","DEVIL","SLIDE","BIBLE",
            "DIESEL","FOSSIL","BESIDE","WILDLIFE"]
}
```
 
로딩 시 `poses`에 없는 글자를 포함한 단어는 **경고 로그 후 제외**한다. 기동 실패 조건이 아니다 — 판별 테스트에서 글자가 빠져도 서버는 떠야 한다. 같은 글자가 연속되는 단어(`BLESS`의 SS)는 연결 모드에서 경계가 사라지므로 **또박또박 모드로만 출제**한다.
 
---
 
## 5. 캘리브레이션 계층
 
```
angle = OPEN + (CLOSED − OPEN) × clamp(flex, 0.0, 1.0)
angle = clamp(angle, min(OPEN, CLOSED), max(OPEN, CLOSED))
```
 
- 입력(flex)과 출력(angle) 양쪽 clamp. 서버·펌웨어 각각 구현. **제거 금지.**
- 안전 범위는 `poses.json`의 `calibration`에서만 온다. **확장 금지.**
- clamp가 실제로 값을 바꿨으면 경고 로그 + `/ws/mcp-log`에 `blocked` 이벤트.
### 5.1 펌웨어 헤더 생성
 
```
python tools/gen_calibration.py  →  firmware/hand_controller/calibration.h
```
 
생성 헤더에는 캘리브레이션 테이블과 그 **SHA-256 앞 8자리(`CAL_HASH`)** 가 들어간다. 서버는 기동 시 같은 방식으로 해시를 계산하고, 시리얼 접속 직후 `?`로 펌웨어 해시를 받아 대조한다. **불일치 시 서버는 구동을 거부하고 `CAL_MISMATCH`를 띄운다.** `poses.json`을 고치고 펌웨어 재업로드를 잊는 사고를 이 핸드셰이크가 막는다.
 
---
 
## 6. 디바이스 계층
 
### 6.1 인터페이스
 
펌웨어는 글자를 모른다. 어댑터도 글자가 아니라 **flex 벡터**를 받는다.
 
```python
Flex = dict[str, float]          # 손가락 5개 → 0.0~1.0
 
class DeviceAdapter(ABC):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
 
    async def move(self, seq: int, flex: Flex,
                   transition_ms: int, hold_ms: int) -> None:
        """자세 이동·유지. RDY(seq)/DON(seq) 이벤트를 큐로 발행."""
 
    async def safe(self) -> None:
        """비상 정지 → 즉시 안전 자세. 진행 중 명령 취소."""
 
    @property
    def state(self) -> Literal["idle", "moving", "holding", "error"]: ...
 
    def events(self) -> AsyncIterator[DeviceEvent]:
        """RDY(seq) / DON(seq) / ERR(code)"""
```
 
글자 → flex 변환과 중립 삽입은 `session.py`가 담당한다. 어댑터 위에서는 `neutral`도 그냥 하나의 flex 벡터다.
 
### 6.2 Mock 디바이스
 
```python
class MockDevice(DeviceAdapter):
    async def move(self, seq, flex, transition_ms, hold_ms):
        self._state = "moving"
        await self._interpolate(flex, transition_ms)   # 20ms 틱 동기 보간
        self._emit(DeviceEvent("RDY", seq))
        self._state = "holding"
        await asyncio.sleep(hold_ms / 1000)
        self._emit(DeviceEvent("DON", seq))
        self._state = "idle"
```
 
**v1의 "flex 차이 비례 전이 시간"은 삭제한다.** 실측 결과 동기 보간에서는 자세 쌍과 무관하게 900ms로 일정했다. Mock도 요청된 `transition_ms`를 그대로 쓴다.
 
### 6.3 Mock 시각화 `mockhand.html`
 
SVG 손 하나에 손가락 5개 회전각을 flex에 바인딩. **개발 도구이자 발표 폴백이며 제품 기능이 아니다.** 학습자 뷰에 기본 노출하지 않는다. v1 5.4의 목적·주의사항 그대로.
 
### 6.4 환경변수
 
```
DEVICE_MODE=mock | serial     (기본 mock)
SERIAL_PORT=COM?              ← 하드웨어팀 회신 대기
SERIAL_BAUD=115200
```
 
### 6.5 시리얼 프로토콜
 
flex는 부동소수점 파싱을 피하기 위해 **0~1000 정수(‰)** 로 전송한다. 순서는 `fingers` 배열 순(엄지·검지·중지·약지·소지).
 
**서버 → 펌웨어**
 
| 명령 | 형식 | 예 |
|---|---|---|
| 이동·유지 | `P <seq> <t> <i> <m> <r> <p> <trans_ms> <hold_ms>\n` | `P 17 700 0 0 1000 1000 900 2000` (V) |
| 비상 정지 | `Z\n` | 즉시 안전 자세, 진행 중 명령 취소 |
| 상태 조회 | `?\n` | |
 
**펌웨어 → 서버**
 
| 응답 | 형식 | 시점 |
|---|---|---|
| 도달 | `RDY <seq>\n` | 목표 도달. `observe_from=arrival`이면 관찰 시작점 |
| 완료 | `DON <seq>\n` | 유지 종료 |
| 상태 | `ST <state> <cal_hash>\n` | `?` 응답. 접속 직후 해시 대조에 사용 |
| 오류 | `ERR <code>\n` | `ERANGE`(clamp 발생) `EBUSY` `EPARSE` |
 
`seq`는 서버가 붙이는 단조 증가 정수. 펌웨어는 의미를 모르고 그대로 되돌려준다. 이것으로 RDY/DON을 어느 글자에 대응시킬지 서버가 결정한다.
 
**타임아웃:** `P` 전송 후 `trans_ms + hold_ms + 2000ms` 내 `DON` 미수신 → `Z` 전송, `DEVICE_TIMEOUT`.
 
---
 
## 7. 펌웨어 사양
 
### 7.1 보유 데이터
 
캘리브레이션 테이블(`calibration.h`, 생성 산출물)과 안전 자세(전 손가락 open)뿐이다. **자세 테이블·중립 자세·글자 이름은 펌웨어에 존재하지 않는다.**
 
### 7.2 상태 머신
 
```
IDLE ──P──▶ MOVING ──도달──▶ HOLDING ──hold 경과──▶ IDLE
              │ RDY 발행                 │ DON 발행
              └──────── Z ───────────────┴──▶ MOVING(안전 자세) ──▶ IDLE
```
 
`loop()`에서 `delay()`를 쓰지 않는다. `millis()` 기반 20ms 틱.
 
### 7.3 동기 보간
 
v1과 동일. 다섯 손가락이 **동시에 도달**하도록 `transition_ms / TICK_MS` 스텝으로 선형 보간.
 
### 7.4 안전 (펌웨어 측)
 
| 항목 | 규칙 |
|---|---|
| 부팅 | **안전 자세**로 이동 후 IDLE (중립이 아님) |
| 무명령 타임아웃 | `idle_timeout_ms` 경과 시 안전 자세 |
| flex 범위 초과 (‰ > 1000) | clamp 후 진행, `ERR ERANGE` |
| 각도 범위 초과 | clamp (이중화). `toAngle`은 v1 6.3과 동일 |
| 파싱 실패 | `ERR EPARSE`, 자세 변경 없음 |
| 이동 중 `P` 수신 | `ERR EBUSY`, 무시 |
 
---
 
## 8. 데이터 모델
 
```sql
CREATE TABLE learners (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMP NOT NULL
);
 
CREATE TABLE sessions (
    id            INTEGER PRIMARY KEY,
    learner_id    INTEGER NOT NULL REFERENCES learners(id),
    status        TEXT NOT NULL,          -- planned|approved|running|done|aborted
    source        TEXT NOT NULL,          -- agent|rule|manual
    plan_json     TEXT,
    started_at    TIMESTAMP,
    ended_at      TIMESTAMP
);
 
CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER NOT NULL REFERENCES sessions(id),
    seq           INTEGER NOT NULL,
    track         TEXT NOT NULL,          -- discrimination|word
    letters       TEXT NOT NULL,          -- "VWB" / "WISE"
    hold_ms       INTEGER NOT NULL,
    transition_ms INTEGER NOT NULL,       -- 900 또는 느린 재생값
    boundary      TEXT NOT NULL,          -- explicit|direct
    presented_at  TIMESTAMP,
    ready_at      TIMESTAMP,              -- 마지막 글자 RDY. 응답 시간 측정 기준
    answered_at   TIMESTAMP,
    answer        TEXT,
    result        TEXT,                   -- correct|wrong|length|order
    attempt       INTEGER NOT NULL DEFAULT 1
);
 
CREATE TABLE confusions (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES items(id),
    track        TEXT NOT NULL,           -- items.track 복제. 집계 시 JOIN 회피
    position     INTEGER NOT NULL,
    expected     TEXT NOT NULL,
    given        TEXT NOT NULL
);
 
CREATE TABLE device_lock (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    session_id   INTEGER REFERENCES sessions(id),
    acquired_at  TIMESTAMP,
    heartbeat_at TIMESTAMP
);
```
 
**v1 → v2 마이그레이션:** `items`에 `track`, `transition_ms`, `ready_at` 추가, `correct BOOLEAN` → `result TEXT`. `confusions`에 `track` 추가. 기존 데이터는 개발용이므로 DB 재생성으로 처리한다.
 
---
 
## 9. 핵심 로직
 
### 9.1 난이도 3축
 
| 축 | 파라미터 | 범위 | 기본 | 상태 |
|---|---|---|---|---|
| ① 유지 시간 | `hold_ms` | 2000 ~ 3000 | 3000 | **약화.** 하한 성격 확인 대기 |
| ② 시퀀스 길이 | `len(letters)` | 3 ~ 7 | 3 | 정상 |
| ③ 글자 경계 | `boundary` | explicit / direct | explicit | 정상 |
 
`transition_ms`는 난이도 축이 아니다. 기본 900, 오답 1회 느린 재생에서만 `transition_slow_ms`를 쓴다.
 
**①축 대응 분기.** `observe_from`이 `settled`로 바뀌면(정착 시간으로 확정) 서버는 RDY 수신 후 `settle_ms` 경과 시점부터 관찰 시간을 세고 `hold_min_ms`를 내린다. 코드 변경 없이 설정만 바뀐다. 부하 한계로 확정되면 설정을 그대로 두고 적응 규칙의 ①축 가중을 낮춘다(9.4).
 
### 9.2 트랙
 
| 트랙 | 출처 | 제약 |
|---|---|---|
| `discrimination` | `items.py` 생성 | 같은 글자 연속 금지, 최근 20문항 중복 금지, 깊이쌍 게이팅, D 비중 상한 |
| `word` | `words.json` | 최근 20문항 중복 금지, 연속 동일 글자 단어는 explicit만 |
 
**두 트랙의 통계는 절대 합치지 않는다.** `stats.py`의 모든 집계 함수는 `track`을 필수 인자로 받는다.
 
### 9.3 판정
 
```python
def judge(expected: str, answer: str) -> Judgement:
    a = answer.upper().replace(" ", "")
    if a == expected:
        return Judgement(result="correct", confusions=[])
    if len(a) != len(expected):
        return Judgement(result="length", confusions=[])
    if sorted(a) == sorted(expected):
        return Judgement(result="order", confusions=[])      # 애너그램: 순서 오류
    conf = [Confusion(i, e, g) for i, (e, g) in enumerate(zip(expected, a)) if e != g]
    return Judgement(result="wrong", confusions=conf)
```
 
- 길이 불일치: 위치 대응이 불확실 → 혼동 미기록
- 애너그램: 글자는 다 읽었고 순서만 틀림 → 혼동 미기록, `order`로 별도 집계 (작업기억 지표)
- 혼동 기록은 `confusion_pairs`에 있는 쌍만 통계에 쓴다. 나머지는 저장하되 `unlisted`로 표시
### 9.4 적응 규칙 (규칙 기반)
 
```python
ADAPT = {
    "correct":      lambda s: s.with_(hold_ms=clamp(int(s.hold_ms * 0.85), HOLD_MIN, HOLD_MAX)),
    "wrong_first":  lambda s: s.with_(hold_ms=HOLD_MAX,
                                      transition_ms=TRANSITION_SLOW,
                                      boundary="explicit", retry=True),
    "wrong_second": lambda s: s.demo_pair().with_(length=max(3, s.length - 1),
                                                  hold_ms=HOLD_DEFAULT),
}
```
 
- `correct`가 3000→2550→2167→2000에서 바닥을 친다. ①축이 3단계뿐이라는 뜻이며, 그 뒤 정답은 `length` 증가 또는 `direct` 전환으로 넘어간다 (②·③축 우선 순서: length 3→5 → direct → length 5→7)
- `order` 결과는 `wrong_first`와 같이 처리하되 `demo_pair()` 대상이 없으므로 2회째도 시범 없이 후퇴만 한다
- `demo_pair()`: 혼동 첫 위치의 (expected, given) 쌍을 `demo_letter(expected, HOLD_MAX) → neutral → demo_letter(given, HOLD_MAX)`로 시범. 힌트 문구는 `confusion_pairs[].hint`에서 조회. 시연 ④의 하이라이트
### 9.5 문항 생성 (규칙 기반 폴백)
 
```python
def generate_items(stats, count, track, target_pairs=None):
    pairs = target_pairs or stats.weakest_pairs(track, top=2, exclude_depth=not stats.depth_unlocked())
    ...
```
 
- 깊이쌍 게이팅: `curriculum.depth_pairs_unlock` 조건 충족 전엔 E/I, O/S, E/O 미출제
- D 비중: `heavy_letters["D"]` 비율 초과 시 D 미포함 문항으로 대체
- 세션 구성 기본: 판별 8 + 통글자 4 (판별로 약점, 통글자로 마무리)
---
 
## 10. 안전 게이트
 
**모든 구동 요청은 예외 없이 이 경로를 통과한다.**
 
```
요청 → ① 세션 락 → ② 글자 존재 → ③ 커리큘럼 → ④ flex 조회·clamp
     → ⑤ 각도 변환·clamp → ⑥ rate limit → ⑦ 디바이스 idle → ⑧ 캘리브레이션 해시 → 전송
```
 
| # | 검사 | 실패 시 |
|---|---|---|
| ① | `session_id`가 락 보유자와 일치 | `409 LOCK_NOT_HELD` |
| ② | 모든 글자가 `poses`에 존재 | `400 UNSUPPORTED_LETTERS` + 대안 목록 |
| ③ | 깊이쌍 미해금 상태에서 깊이쌍 포함 채점 문항 / 트랙 값 유효 | `400 CURRICULUM_VIOLATION` (시범 `demo_letter`는 면제) |
| ④ | `0.0 ≤ flex ≤ 1.0` | clamp + 경고 + `blocked` 로그 |
| ⑤ | 각도가 캘리브레이션 범위 내 | clamp + 경고 + `blocked` 로그 |
| ⑥ | 직전 **구동 요청**으로부터 `transition_ms` 경과 | `429 RATE_LIMIT` |
| ⑦ | 디바이스 `idle` | `409 DEVICE_BUSY` |
| ⑧ | serial 모드에서 펌웨어 해시 일치 | `503 CAL_MISMATCH` |
 
**rate limit은 구동 요청 단위다.** 글자 단위로 걸면 시퀀스가 스스로 차단된다. 시퀀스 내 글자 간격은 전이 시간이 물리적으로 보장한다.
 
| 추가 제한 | 값 |
|---|---|
| 세션당 최대 구동 요청 | 200 |
| 락 heartbeat | 15초 |
| 락 만료 | 마지막 heartbeat + 60초 |
| 서버 기동 시 | 고아 락 정리, 디바이스 안전 자세 |
 
---
 
## 11. REST API
 
### 학습자
 
| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/learners` | 이름 목록 |
| `POST` | `/api/sessions` | 세션 시작, 락 획득 |
| `GET` | `/api/sessions/{id}/next` | 다음 문항 (`track`, 길이 포함) |
| `POST` | `/api/sessions/{id}/present` | 현재 문항 표현 |
| `POST` | `/api/sessions/{id}/answer` | 판정 + 적응 |
| `POST` | `/api/sessions/{id}/end` | 종료, 락 해제, 요약 (트랙별 정확도·순서 오류율·평균 응답 시간) |
| `POST` | `/api/sessions/{id}/heartbeat` | 락 유지 |
 
### 진단 / 에이전트 / 디바이스
 
| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/stats/confusion?track=` | 혼동 행렬. `track` 필수 |
| `GET` | `/api/stats/learners/{id}` | 학습자 상세 (트랙 분리) |
| `POST` | `/api/agent/plan` | 자연어 → 계획 (실행 안 함) |
| `POST` | `/api/agent/plans/{id}/approve` | HITL 승인 → 세션 생성 |
| `POST` | `/api/agent/plans/{id}/reject` | 거부 |
| `GET` | `/api/device/status` | 상태, 모드, 캘리브레이션 해시 일치 여부 |
| `POST` | `/api/device/stop` | 비상 정지 (안전 자세) |
 
### WebSocket `/ws/session/{id}`
 
```
{ "type": "presenting", "index": 2, "total": 12, "track": "word", "length": 4 }
{ "type": "ready",      "index": 2, "letter_pos": 0 }    // 글자별 RDY
{ "type": "done",       "index": 2 }                     // 마지막 글자 DON. 입력 활성화
{ "type": "judged",     "result": "wrong", "confusions": [...] }
{ "type": "demo",       "letters": ["V","W"], "hint": "약지를 확인하세요. V는 굽힘, W는 폄" }
{ "type": "ended",      "summary": {...} }
{ "type": "error",      "code": "DEVICE_TIMEOUT" }
```
 
### WebSocket `/ws/mcp-log`
 
```
{ "type": "tool_call",   "name": "get_learner_stats", "summary": "학습자 12명 조회" }
{ "type": "tool_result", "name": "get_item_bank",     "summary": "문항 후보 52개" }
{ "type": "plan_ready",  "items": 12, "estimated_seconds": 312 }
{ "type": "blocked",     "reason": "UNSUPPORTED_LETTERS", "detail": "C 미지원 → 대안 11자 반환" }
{ "type": "blocked",     "reason": "LOCK_NOT_HELD",       "detail": "세션 없는 present_sequence" }
```
 
---
 
## 12. MCP 서버
 
**전송:** stdio. **클라이언트:** 자체 에이전트 + Claude Desktop (시연 ⑤ 필수).
 
### 12.1 도구
 
#### 1층 · 조회 (부작용 없음)
 
**`get_capabilities()`** — 프롬프트에 글자를 하드코딩하지 않기 위한 런타임 능력 선언.
```json
{
  "device_id": "hand-01",
  "dof": { "flexion": 5, "opposition": 0, "abduction": 0 },
  "letters": ["B","D","E","F","I","L","O","S","V","W","Y"],
  "tracks": ["discrimination", "word"],
  "confusion_pairs": [ { "pair": ["V","W"], "axis": "count", "depth": false }, ... ],
  "depth_pairs_unlocked": false,
  "difficulty_axes": {
    "hold_ms":  { "min": 2000, "max": 3000, "note": "floor under review" },
    "length":   { "min": 3, "max": 7 },
    "boundary": ["explicit", "direct"]
  },
  "transition_ms": 900,
  "word_count": 32
}
```
 
**`get_learner_stats(learner_name)`** — 트랙별 정확도, 혼동쌍별 오답률, 순서 오류율, `accuracy_by_hold_ms`, 최근 20문항.
**`get_item_bank(track, letters[], length, count, exclude_recent=true)`** — 제약을 만족하는 후보. `word` 트랙은 `words.json`에서.
**`get_station_status()`** — `{ "state": "idle", "locked_by": null, "device_mode": "mock" }`
 
#### 2층 · 세션 (되돌릴 수 있음)
 
**`start_session(learner_name, plan)`** → `session_id`, 락 획득
**`submit_answer(session_id, answer)`** → 판정
**`end_session(session_id)`** → 요약, 락 해제
 
#### 3층 · 구동 (안전 게이트)
 
**`present_sequence(session_id, letters, hold_ms, boundary, track)`**
**`demo_letter(session_id, letter, hold_ms)`**
 
**도구 스키마에 flex·각도·transition 파라미터가 없다.** LLM이 각도를 지정하는 경로는 스키마 수준에서 존재하지 않는다. `session_id` 필수, 락 미보유 호출은 거부.
 
### 12.2 구조화된 실패
 
```json
{ "ok": false, "reason": "unsupported_letters", "requested": "CAT",
  "unsupported": ["C","A","T"], "supported": ["B","D","E","F","I","L","O","S","V","W","Y"],
  "next_step": "call get_item_bank() with supported letters only" }
 
{ "ok": false, "reason": "lock_not_held",
  "detail": "present_sequence requires an active session", "next_step": "call start_session() first" }
 
{ "ok": false, "reason": "curriculum_violation", "blocked_pairs": [["E","I"]],
  "detail": "depth pairs locked until accuracy ≥ 0.80 over 20 items",
  "next_step": "use demo_letter for these, or choose non-depth pairs" }
```
 
### 12.3 Claude Desktop 연동 (시연 ⑤)
 
v1 11.3과 동일. **시연 대사:** "이 로봇손으로 V랑 W 차이를 보여줘" → `get_capabilities` → `start_session` → `demo_letter` 2회 → `end_session`.
 
---
 
## 13. 에이전트
 
### 13.1 역할 분담
 
v1과 동일. LLM은 해석·선별·구성·배열·요약. 규칙은 판정·적응·변환·clamp·락·통계.
 
### 13.2 출력 스키마 (강제)
 
```json
{
  "rationale": "V/W 오답률 34%로 최고. 판별 8문항으로 집중 후 단어 4문항으로 마무리",
  "target_pairs": [["V","W"], ["W","B"]],
  "items": [
    { "track": "discrimination", "letters": "VWB",  "hold_ms": 3000, "boundary": "explicit" },
    { "track": "discrimination", "letters": "BWVW", "hold_ms": 2500, "boundary": "direct" },
    { "track": "word",           "letters": "WISE", "hold_ms": 3000, "boundary": "explicit" }
  ],
  "estimated_seconds": 312
}
```
 
### 13.3 검증 (승인 전 서버 자동)
 
| 항목 | 실패 시 |
|---|---|
| 모든 글자가 `poses`에 존재 | 재생성 (최대 2회) → 규칙 폴백 |
| `word` 문항이 `words.json`에 존재 | 해당 문항 제거 |
| `hold_ms` 범위 | clamp |
| `length` 3~7 | 제거 |
| 같은 글자 연속 (판별 트랙) | 제거 |
| 깊이쌍 미해금 상태에서 깊이쌍 포함 | 제거 + 경고 |
| D 포함 비율 > 상한 | 경고 |
| 약점 상위 쌍 포함 | 경고 (차단 아님) |
| 최근 20문항 중복 | 제거 |
| `estimated_seconds` ±20% | 경고 |
 
**검증 통과 후에도 실행되지 않는다.** HITL 승인 필요.
 
### 13.4 폴백
 
LLM 실패·검증 2회 실패 → `items.generate_items()`, `source="rule"`. 발표 당일 네트워크 장애 대비.
 
---
 
## 14. 화면
 
### 14.1 학습자 뷰 `/practice.html`
 
```
┌────────────────────────────┐
│  홍길동      3 / 12 · 단어 │   ← 트랙만 표시. 길이는 판별 트랙에서만
│      ● 표현 중...          │
│   ┌────────────────────┐   │   ← done 후 활성
│   └────────────────────┘   │
│         [ 제출 ]           │
│      [ 다시 보기 ]         │
└────────────────────────────┘
```
 
원칙 v1과 동일: 시선은 로봇손에. 애니메이션·색 강조 최소. 정답은 제출 후 공개.
 
### 14.2 진단 뷰 `/teacher.html` *(선택)* — **MCP 로그 패널은 필수**
 
혼동 행렬(트랙 토글) / 자연어 입력·계획 생성 / 계획 미리보기·승인·거부 / **MCP 호출 로그 (실시간)**. 로그 패널이 시연 ⑥의 핵심이므로 진단 뷰 나머지가 빠져도 이 패널은 만든다.
 
---
 
## 15. 시연 대응 사항
 
기획서 11장 시나리오를 구현 관점에서 보완한다.
 
### 15.1 ① 훅 폴백
 
`WISE`를 기본으로 하되, 판별 테스트(10.1)에서 E가 탈락하면 `WILD` 또는 `BODY`로 바꾼다. 둘 다 `words.json`에 있고 E를 쓰지 않는다. **훅 단어는 설정값이며 코드에 박지 않는다.**
 
### 15.2 ⑥ 차단 사례 재구성
 
v1 11.1의 "LLM 각도 직접 지정 시도"와 "flex 1.4 입력"은 **실제로 발생할 경로가 없다.** 도구 스키마에 각도·flex 파라미터가 없고, 서버 안쪽에서 flex를 만드는 건 `poses.json`뿐이다. 발생하지 않는 사건의 차단 로그를 띄우면 연출이 된다.
 
| 구분 | 사례 | 시연 방식 |
|---|---|---|
| **라이브 차단** | 지원하지 않는 글자 `C` | Claude Desktop에 "CAT 보여줘" → 구조화 실패 + 대안 목록 |
| **라이브 차단** | 세션 없는 구동 호출 | `start_session` 없이 `present_sequence` → `lock_not_held` |
| **라이브 차단** | 미해금 깊이쌍 채점 시도 | 신규 학습자에게 `E-I-E` → `curriculum_violation` |
| **라이브 정지** | HITL 승인 대기 | 계획 생성 후 실행 안 됨 |
| **설명 슬라이드** | LLM 각도 지정 | 도구 스키마 화면 캡처: 파라미터 자체가 없음 |
| **설명 슬라이드** | 값 오류·clamp 이중화 | 단위 테스트 결과 화면 |
 
---
 
## 16. 개발 순서
 
### Phase 0′ — 마이그레이션 (1주차, 9/3~9/9) · 하드웨어 병행
 
- [ ] `poses.json` schema v2 (11자, 중립·안전 분리, 15쌍+힌트, curriculum), 검증 강화
- [ ] `words.json` + 필터 로딩
- [ ] `DeviceAdapter.move(flex)` 로 인터페이스 변경. Mock 전이 시간 고정
- [ ] `tools/gen_calibration.py` + 해시
- [ ] DB 마이그레이션 (`track`, `result`, `ready_at`, `transition_ms`)
- [ ] 판정: 애너그램 `order`. 적응: v2 상수·①축 바닥 후 ②③ 전환
- [ ] 문항 생성: 2트랙, 깊이쌍 게이팅, D 상한
- [ ] 펌웨어 착수: 프로토콜 파서, `seq` 에코, 상태 머신
- [ ] **제3자 판별 파일럿 5명** (전기팀과 함께. 정식 테스트는 3주차)
**완료 기준:** Mock으로 판별 8 + 단어 4 세션이 끝까지 돌고, 요약이 트랙별로 나온다.
 
### Phase 2 — 펌웨어·실기 (1~2주차, ~9/16)
 
- [ ] 20ms 틱 동기 보간, RDY/DON, 안전 자세 부팅·타임아웃
- [ ] `toAngle` clamp, `ERANGE`
- [ ] `SerialDevice` + 해시 핸드셰이크
- [ ] `DEVICE_MODE=serial`로 Phase 0′ 완료 기준 재현
- [ ] 유지 시간 하한 성격 회신 반영 (`observe_from`)
**완료 기준:** 실기에서 `WISE`가 표현되고 마지막 DON까지 타이밍이 계산값과 ±100ms 내.
 
### Phase 3 — MCP (3주차, 9/17~9/23)
 
- [ ] 3층 도구, `get_capabilities` 런타임 선언
- [ ] 구조화된 실패 3종
- [ ] `/ws/mcp-log`
- [ ] Claude Desktop 연동 확인 (Windows 경로·stdio)
- [ ] 판별 테스트 결과 반영 → 세트 축소 시 `poses.json`·`words.json`·훅 단어 갱신
**완료 기준:** Claude Desktop에서 "V와 W 차이 보여줘"로 구동, "CAT 보여줘"로 구조화 실패.
 
### Phase 4 — 에이전트 + 실데이터 (4주차, 9/24~9/30)
 
- [ ] LLM 호출, 스키마 검증 10항목, HITL, 폴백
- [ ] 실데이터 수집: 판별 테스트 참가자를 이어서 섭외. 10명 × 2세션 목표, 최소 6명
- [ ] 응답 시간 실측 → 기획서 3.6 표 갱신
### Phase 5 — 마무리 (5주차, 10/1~10/5)
 
- [ ] 진단 뷰 (선택. MCP 로그 패널은 Phase 3에서 완료)
- [ ] 시연 리허설 6구간 전체, 전 구간 녹화
- [ ] Mock 시각화 폴백 모드 리허설
- [ ] 여분 부품·포트·전원 점검 (전기팀)
**삭제 순서 (일정 압박 시):** 단어 난이도 자동 분류 → 에이전트 회귀 테스트 → 진단 뷰 나머지.
 
---
 
## 17. 테스트
 
| 대상 | 방식 | 우선순위 |
|---|---|---|
| `flex → angle` | 경계값 0.0/1.0/범위 밖, 역방향 손가락 | **필수** |
| clamp 이중화 | 범위 밖 입력이 항상 안전 범위로 | **필수** |
| 캘리브레이션 해시 | `poses.json` 변경 시 불일치 검출 | **필수** |
| 세션 락 | 동시 요청 이중 점유 불가, 고아 락 정리 | **필수** |
| 판정 | 길이 불일치·애너그램·대소문자·공백 | **필수** |
| `poses.json` 검증 | 중립이 모든 글자와 2손가락 이상 차이 | **필수** |
| 트랙 분리 | 단어 오답이 판별 통계에 섞이지 않음 | **필수** |
| 깊이쌍 게이팅 | 미해금 시 생성·에이전트·게이트 모두 차단 | 권장 |
| 적응 규칙 | ①축 바닥 후 ②③ 전환 순서 | 권장 |
| 프로토콜 | Mock 왕복, `seq` 대응, 타임아웃 | 권장 |
| 에이전트 출력 | 픽스처 30개 | 선택 |
 
**최소 방어선:** clamp, 락, 해시. 이 셋이 깨지면 하드웨어가 부서지거나 시연이 무너진다.
 
---
 
## 18. 미해결 항목
 
| ID | 내용 | 상태 | 현재 대응 |
|---|---|---|---|
| TBD-1 | 11자 flex | **확정** | `poses.json` 반영 |
| TBD-2 | 유지 시간 범위 | **확정 / 하한 성격 미확인** | 2000~3000. `observe_from=arrival`로 개발 |
| TBD-3 | 전이 시간 | **확정** | 900 고정 |
| TBD-4 | 재캘리브레이션 주기 | 미확정 | 3~4주차 내구 테스트 |
| TBD-5 | 세션 소요 시간 | 확정 (응답 6초 추정) | 4주차 실측으로 갱신 |
| TBD-6 | D·E·F·O | 확정 (등급 폐지로 소멸) | **제3자 판별 테스트 결과에 따라 세트 축소 가능** |
| — | 30분 연속 구동 발열 | 미회신 | 세션 5분 기준으로 개발. 회신 시 `idle_timeout_ms` 조정 |
| — | 시리얼 포트 번호 | 미회신 | `SERIAL_PORT` 환경변수 |
| — | 여분 부품 | 미회신 | 폴백 모드로 대비 |
 
**모든 하드웨어 종속 값은 `poses.json` 한 파일에 격리되어 있다. 값이 확정되면 파일만 교체하며 코드는 수정하지 않는다. 펌웨어 캘리브레이션 헤더는 생성 산출물이므로 재생성·재업로드 후 해시로 일치를 확인한다.**