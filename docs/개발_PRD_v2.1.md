# 지문자 수용 훈련 로봇손 — 개발 PRD

> **버전:** 2.1
> **작성일:** 2026-09-03 (2.1 개정 2026-09-08)
> **상위 문서:** 지문자_학습_로봇손_기획서_v3.md (기획 확정본)
> **개발자:** 최강우 (1인)
> **기간:** 5주 (2026-09-03 ~ 2026-10-05)
> **하드웨어 상태:** 제작 완료. 자세·타이밍 실측 완료. 11자 육안 판별 확인. 보드 Uno, 핀 배치 확정.
> **소프트웨어 상태:** v1 코드는 폐기하고 **신규 개발**로 진행했다. Mock 기반 전 계층(설정·변환·디바이스·DB·판정·통계·출제·안전·세션·REST/WS·학습자 화면)과 펌웨어·헤더 생성기가 완료됐다. 남은 것은 시리얼 어댑터, 실기 결합, MCP·에이전트.
>
> **2.1 개정 사유:** 구현 과정에서 확정된 사항을 문서에 반영한다. 아래 항목은 코드가 기준이며 문서를 코드에 맞췄다. 각 변경의 근거는 `DECISIONS.md`에 있다.
>
> | 절 | 변경 | 근거 |
> |---|---|---|
> | 2 | Python 3.11+ → 3.12 | 실제 환경 |
> | 4.1 | `calibration` 구조 개편, `pins`·`validation` 블록 추가 | DECISIONS §2 |
> | 4.2 | `hook_word`·`hook_fallbacks` 필드 명시 | §10 |
> | 5.1 | 해시 대상을 하드웨어 관련 블록으로 한정 | §29 |
> | 6.1 | 어댑터를 동기(sync)로 | §13 |
> | 6.5 | `H`/`HSH`·`N` 명령, `ETIME`·`ESEQ` 코드 추가 | §21, §31, §33 |
> | 7.1 | 중립·안전 자세는 생성 헤더로 내려간다 | §31 |
> | 7.4 | ‰ 범위 초과는 clamp가 아니라 거부 | §32 |
> | 8 | `unlisted` 컬럼 미생성 (집계 시점 계산) | §18 |
> | 9.4 | ①축 양방향, 사다리 데드엔드 수정 | §23 |
> | 11 | `/demo` 추가, `judged` 이벤트에서 오답 1회째 `confusions` 제외 | §26~§27 |
> | 15.2 | 차단 사례에 `LOCK_UNAVAILABLE`·`SESSION_QUOTA` 반영 | §21 |
> | 16 | 개발 순서를 실제 진행에 맞춰 재작성 | — |

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
| 유지 시간 | 400~3000ms | **800~3000ms**. 하한 성격 확인 완료(TBD-2), 하한 자체가 불필요했음 | 3.2, 9.1 |
| 중립 자세 | 전 손가락 0.0 | **중지만 0.70** | 3.5 |
| 안전 자세 | 중립과 동일 | **분리.** 전 손가락 0.0. 부팅·타임아웃·비상정지 전용 | 3.5 |
| 펌웨어 자세 테이블 | `PROGMEM` 복제 | **복제 금지.** 서버가 flex 값을 전송 | 7.3 |
| 시리얼 프로토콜 | `P <letter> ...` | **`P <seq> <flex×5> ...`** 글자 개념이 펌웨어에서 사라짐 | 7.3 |
| 캘리브레이션 일치 검증 | 불가능 | **해시 핸드셰이크.** 펌웨어 헤더를 `poses.json`에서 생성하고 접속 시 해시 대조 | 7.3 (신규 해법) |
| 안전 게이트 ③ | 등급 검사 | **트랙·깊이쌍 게이팅 검사**로 대체 | 2.3, 3.3 |
| 판정 | 길이 불일치만 예외 | + **애너그램은 순서 오류**로 분리 기록 | 통계 오염 방지 |
| 시연 ⑥ 차단 사례 | LLM 각도 지정·flex 1.4 | **실제 발생 가능한 사례로 재구성.** 스키마 차단은 설명 슬라이드 | 11.1 재검토 |
| 시연 ① 훅 | `V-W-B` | `WISE`. **E 탈락 대비 폴백 단어 지정** | 11 |
| 일정 | 6주, Phase 0부터 | **5주.** 제3자 판별 파일럿 1주차로 전진 | 13 |

> v1 코드는 폐기하고 신규 개발로 진행했다. 이 표는 v1 **사양** 대비 변경점을 정리한 것이며, 코드 마이그레이션 이력이 아니다.

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

| 계층 | 선택 |
|---|---|
| 언어 | Python 3.12 (프로젝트 `.venv`) |
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
    "servo_range_deg": { "min": 0, "max": 180 },
    "fingers": {
      "thumb":  { "open": 180, "closed": 80  },
      "index":  { "open": 0,   "closed": 120 },
      "middle": { "open": 0,   "closed": 125 },
      "ring":   { "open": 0,   "closed": 105 },
      "pinky":  { "open": 180, "closed": 80  }
    }
  },

  "pins": { "thumb": 2, "index": 3, "middle": 6, "ring": 8, "pinky": 10 },
  "board_pin_range": { "min": 0, "max": 53 },

  "timing": {
    "transition_ms": 900,
    "transition_slow_ms": 1800,
    "hold_min_ms": 800,
    "hold_max_ms": 3000,
    "hold_default_ms": 3000,
    "interpolation_tick_ms": 20,
    "observe_from": "arrival",
    "settle_ms": 0,
    "idle_timeout_ms": 30000,
    "_note": "transition/hold는 실측. hold_min 2000의 성격은 2026-09-08 확인 결과 정착 시간도 부하 한계도 아니었다 — 하한 자체가 불필요했고 800ms까지 안정 동작을 확인해 하한을 내렸다. 정착 시간 가설이 기각되었으므로 observe_from은 arrival, settle_ms는 0을 유지한다"
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

  "validation": {
    "neutral_min_distinct_fingers": 2,
    "min_finger_delta": 0.20,
    "_note": "두 자세에서 같은 손가락의 flex 차이가 min_finger_delta 이상이면 '다른 손가락'으로 센다"
  },

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
- `schema_version == 2`
- `poses`, `neutral`, `safe`의 모든 flex 값이 `0.0 ≤ x ≤ 1.0`, `fingers` 5개 키가 정확히 전부 존재
- `calibration.fingers`의 키가 `fingers` 배열과 일치, open/closed가 `servo_range_deg` 범위 내, `open ≠ closed`
- `servo_range_deg.min < servo_range_deg.max`
- `pins`의 키가 `fingers` 배열과 일치, 번호 중복 없음, `board_pin_range` 범위 내
- `confusion_pairs`의 모든 글자가 `poses`에 존재, 쌍 중복 없음(`["V","W"]`와 `["W","V"]`는 동일), 자기쌍 없음, `hint` 비어있지 않음
- `neutral`이 모든 글자와 `validation.neutral_min_distinct_fingers` 이상 차이 (기획서 3.5를 코드로 강제)
- `poses` 안에 동일한 두 글자 없음
- `safe`의 모든 값이 0.0 (텐던 최저 장력 자세)
- `transition_ms ≤ transition_slow_ms`, `hold_min_ms ≤ hold_default_ms ≤ hold_max_ms`
- `observe_from`이 `arrival` 또는 `settled`. `settled`이면 `settle_ms > 0`

**"손가락 2개 이상 차이"의 임계값.** flex는 연속값이므로 임계값 없이는 판정이 불가능하다. `validation.min_finger_delta = 0.20`을 기준으로 센다. 0.15~0.25 어느 값을 써도 결과가 같다 — 중립 대비 최소 마진은 **B의 2개(엄지·중지)** 이고 나머지 10자는 3개 이상이다. B가 경계값이라는 사실은 회귀 테스트로 고정되어 있어, 중립이나 B의 값을 건드리면 즉시 드러난다.

**혼동쌍 완전성은 수치로 검증하지 않는다.** `confusion_pairs`는 사람의 지각적 판단 결과이고 실측 flex와 기하학적으로 일치하지 않는다. 예를 들어 I/Y는 개념상 엄지 하나 차이지만 실측값으로는 검지·중지·약지도 0.17~0.24 벌어져 있다. 검증은 참조 무결성(존재·중복·자기쌍)까지만 한다.

### 4.2 `config/words.json`

```json
{
  "words": ["YES","OWL","OLD","ODE","ELF","WEB","LID","DIE","VIE","FIB",
            "LOVE","WISE","BODY","WOLF","IDLE","BOLD","SIDE","WIDE","LOSE","SILO","DOVE",
            "FIELD","SOLID","VIDEO","BLESS","DEVIL","SLIDE","BIBLE",
            "DIESEL","FOSSIL","BESIDE","WILDLIFE"],
  "hook_word": "WISE",
  "hook_fallbacks": ["WILD", "BODY"]
}
```

로딩 시 `poses`에 없는 글자를 포함한 단어는 **경고 로그 후 제외**한다. 기동 실패 조건이 아니다 — 판별 테스트에서 글자가 빠져도 서버는 떠야 한다. 이 점이 `poses.json` 검증(실패 시 기동 중단)과 다르다. 세트 축소는 예상된 시나리오지 설정 오류가 아니기 때문이다.

기동 실패 조건은 따로 있다: `words`가 비었거나, **필터링 후 남은 단어가 0개**이거나(통글자 트랙이 성립하지 않음), 알파벳 외 문자가 섞였거나, 중복 단어가 있거나, `hook_word`와 모든 폴백이 표현 불가한 경우다.

각 단어는 로딩 시점에 `has_repeat`(인접 중복 글자 유무)를 계산해 보관한다. 같은 글자가 연속되는 단어(`BLESS`·`FOSSIL`)는 연결 모드에서 손이 움직이지 않아 경계가 사라지므로 **또박또박 모드로만 출제**한다. 떨어져 있는 중복(`WILDLIFE`의 L)은 해당하지 않는다.

`hook_word`는 시연 ① 훅에서 표현할 단어다(15.1). 현재 문자 집합으로 불가하면 `hook_fallbacks`를 순서대로 시도한다. **훅 단어는 출제 풀이 아니므로 `words` 배열에 없어도 된다** — 없으면 정보 로그만 남기고 기동은 계속한다.

---

## 5. 캘리브레이션 계층

```
angle = OPEN + (CLOSED − OPEN) × clamp(flex, 0.0, 1.0)
angle = clamp(angle, min(OPEN, CLOSED), max(OPEN, CLOSED))
```

- 입력(flex)과 출력(angle) 양쪽 clamp. 서버·펌웨어 각각 구현. **제거 금지.**
- 안전 범위는 `poses.json`의 `calibration`에서만 온다. **확장 금지.**
- clamp가 실제로 값을 바꿨으면 경고 로그 + `/ws/mcp-log`에 `blocked` 이벤트.
- **각도는 float로 반환하고 반올림하지 않는다.** 반올림은 전송 직전 어댑터가 ‰로 바꿀 때 한다. 변환 계층에서 미리 깎으면 역변환 왕복 정확도와 보간 중간값 정밀도를 잃는다.
- 변환 공식이 방향을 가정하지 않는다. 엄지·소지는 `open > closed`(flex 증가 시 각도 감소), 검지·중지·약지는 반대다. 선형 보간이 방향을 자연히 처리하므로 `abs()`나 방향 분기를 넣지 않는다.
- 계산 오차로 경계를 미세하게 넘는 경우(역변환 결과 등)는 `1e-9` 허용폭 안에서 경고하지 않는다. 설정에서 직접 읽은 입력값에는 이 허용폭을 적용하지 않는다.

### 5.1 펌웨어 헤더 생성

```
python tools/gen_calibration.py  →  firmware/hand_controller/calibration.h
```

생성 헤더에는 캘리브레이션 테이블·핀 배치·중립·안전 자세와 그 **SHA-256 앞자리(`CAL_HASH`)** 가 들어간다. 서버는 기동 시 같은 방식으로 해시를 계산하고, 시리얼 접속 직후 `H`로 펌웨어 해시를 받아 대조한다. **불일치 시 서버는 연결 자체를 거부하고 `CAL_MISMATCH`를 띄운다.** `poses.json`을 고치고 펌웨어 재업로드를 잊는 사고를 이 핸드셰이크가 막는다. 검증을 건너뛰는 옵션은 만들지 않는다.

**해시 대상은 `poses.json` 전체가 아니라 펌웨어 동작에 영향을 주는 블록만이다** — `calibration`, `pins`, `neutral`, `safe`, `timing.interpolation_tick_ms`. `words`·`confusion_pairs`·`curriculum`을 고쳐도 펌웨어 동작은 같으므로 제외한다. 전체를 해싱하면 훈련 설정을 만질 때마다 재업로드를 요구하게 되고, 그러면 사람이 해시 검증을 꺼버린다. 방어선이 무력화되는 경로를 미리 막는다.

해싱 전 JSON을 정규화한다(키 정렬·공백 제거). 포매팅 차이로 해시가 바뀌면 안 된다. **해시 계산 로직은 서버 모듈에 한 곳만 두고 `tools/gen_calibration.py`가 이를 import한다.** 두 곳에 중복하면 언젠가 값이 갈라진다.

`--check` 플래그는 파일을 쓰지 않고 대조만 한다. 일치 `exit 0`, 불일치 또는 헤더 부재 `exit 1`. 완료 판정 스크립트가 이 플래그를 호출한다.

---

## 6. 디바이스 계층

### 6.1 인터페이스

펌웨어는 글자를 모른다. 어댑터도 글자가 아니라 **`Pose` 객체**(손가락 5개 flex, 불변)를 받는다. 글자 이름을 넘기는 경로는 존재하지 않는다.

```python
class DeviceAdapter(ABC):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    def move_to(self, pose: Pose, transition_ms: int, hold_ms: int) -> None:
        """자세 이동 후 유지. 완료까지 대기."""

    def neutral(self) -> None:
        """중립 자세. hold 없음 (글자 경계 신호)."""

    def safe(self) -> None:
        """안전 자세. hold_min_ms 만큼 유지."""

    def stop(self) -> None:
        """즉시 정지 후 안전 자세."""

    @property
    def is_busy(self) -> bool: ...
```

**동기(sync)로 만든다.** 서보 동작은 물리적으로 블로킹이고, 세션 루프도 순차 진행이라 동시성이 필요 없다. FastAPI 엔드포인트에서는 백그라운드 스레드로 돌린다. WebSocket 브로드캐스트는 상위 계층이 담당한다.

**중립과 안전은 별개 메서드다.** 하나가 다른 하나를 호출하지 않는다. 중립은 글자 경계 신호(중지만 0.70), 안전은 부팅·타임아웃·비상정지용(전 손가락 0.0)이다. 안전 자세는 도착 즉시 반환하면 상위 계층이 정착 전에 다음 동작을 시작할 수 있으므로 `hold_min_ms`만큼 유지한다.

글자 → `Pose` 변환과 중립 삽입은 `session.py`가 담당한다. 그 아래로는 글자가 내려가지 않는다.

**예외는 구조화된 타입으로 던진다.** `DeviceError`(베이스), `DeviceBusyError`, `DeviceNotConnectedError`, `DeviceTimeoutError`. 에러 문자열을 즉흥 생성하지 않는다.

**‰ 변환은 어댑터가 한다.** 변환 계층은 float 각도까지만 만들고, ‰(0~1000 정수)로 바꾸는 것은 전송 직전 어댑터의 일이다. 이 경계가 "펌웨어는 글자도 각도도 모른다"를 성립시킨다.

### 6.2 Mock 디바이스

현재 자세에서 목표 자세까지 `interpolation_tick_ms`(20ms) 간격으로 선형 보간하고, `transition_ms` 동안 이동한 뒤 `hold_ms` 동안 유지한다. 연결 직후 상태는 **안전 자세**다(중립이 아니다).

**Mock은 실제로 시간을 소비한다.** 즉시 반환하지 않는다. 타이밍 관련 버그(경쟁 상태, 락 heartbeat 누락)가 Mock 단계에서도 재현되어야 하기 때문이다. 대신 **시간 소스를 주입 가능하게** 만들어, 테스트에서는 즉시 반환하는 가짜 sleep을 넣는다.

`stop()`과 새 동작의 경쟁을 처리해야 한다. 단순 플래그로는 부족하다 — A가 이동 중에 `stop()`이 오고 곧바로 B가 시작하면, 뒤늦게 깨어난 A가 뒷정리를 하며 B의 busy 상태를 꺼버린다. 동작마다 세대 번호를 붙여 자기 세대가 지났으면 물러나게 한다.

현재 보간 중인 flex 값을 조회할 수 있게 한다(`mockhand.html` 시각화용).

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
| 중립 | `N\n` | 중립 자세. 안전 자세와 별개 |
| 해시 조회 | `H\n` | 접속 직후 해시 대조에 사용 |

**펌웨어 → 서버**

| 응답 | 형식 | 시점 |
|---|---|---|
| 수신 | `ACK <seq>\n` | 명령 수신 확인 |
| 도달 | `RDY <seq>\n` | 목표 도달(유지 시작). **관찰 시작 시점** |
| 완료 | `DON <seq>\n` | 유지 종료 |
| 해시 | `HSH <hash>\n` | `H` 응답 |
| 오류 | `ERR <seq> <code>\n` | 아래 표 |

| 코드 | 원인 |
|---|---|
| `EPARSE` | 필드 수 부족, 숫자 아님 |
| `ERANGE` | ‰가 0~1000 밖 |
| `EBUSY` | 이동 중 새 `P` 수신 |
| `ETIME` | `trans_ms`·`hold_ms`가 허용 범위 밖 |
| `ESEQ` | `seq` 중복 또는 역행 |

`ETIME`·`ESEQ`는 v2.0 표에 없던 코드다. 원인이 다르면 서버 대응도 달라야 하므로 기존 코드로 묶지 않았다.

`seq`는 서버가 붙이는 단조 증가 정수. 펌웨어는 의미를 모르고 그대로 되돌려준다. 이것으로 RDY/DON을 어느 글자에 대응시킬지 서버가 결정한다. **상위 계층은 `seq`를 모른다** — 어댑터가 관리한다.

**`RDY`가 `ready_at`의 기준이다.** Mock에서는 동기 어댑터 특성상 `DON`만 관측되므로 `hold_ms`를 빼서 역산한다. 시리얼에서는 `RDY` 수신 시각을 그대로 쓴다. 두 어댑터가 같은 인터페이스로 `ready_at`을 제공한다.

**타임아웃:** `P` 전송 후 `trans_ms + hold_ms + 여유` 내 `DON` 미수신 → `Z` 전송, `DEVICE_TIMEOUT`.

---

## 7. 펌웨어 사양

### 7.1 보유 데이터

`calibration.h`(생성 산출물)에 들어가는 것은 다섯 가지뿐이다 — 핀 배치, 손가락별 open/closed 각도, `servo_range_deg`, **중립·안전 자세**, `CAL_HASH`.

**11자 자세 테이블과 글자 이름은 펌웨어에 존재하지 않는다.** 회귀 테스트로 고정되어 있다.

> **v2.0에서 정정.** v2.0은 "중립 자세도 펌웨어에 없다"고 했으나, `N` 명령을 두려면 중립 값이 펌웨어에 있어야 한다. 없으면 서버가 `P`로 보내야 하고 그러면 `N` 자체가 불필요해진다. 중립·안전 두 자세만 생성 헤더로 내보내는 쪽을 택했다 — 생성물이라 `poses.json`과 갈라지지 않고, 해시 대상이라 재업로드 누락도 잡힌다. 단일 출처 원칙은 유지된다. (DECISIONS §31)

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
| 무명령 타임아웃 | `idle_timeout_ms` 경과 시 안전 자세. **`detach()` 하지 않는다** |
| flex 범위 초과 (‰ > 1000) | **거부.** `ERR <seq> ERANGE`, 자세 변경 없음 |
| 각도 범위 초과 | clamp (이중화) |
| `trans_ms`·`hold_ms` 범위 밖 | `ERR <seq> ETIME`, 자세 변경 없음 |
| 파싱 실패 | `ERR <seq> EPARSE`, 자세 변경 없음 |
| 이동 중 `P` 수신 | `ERR <seq> EBUSY`, 무시 |
| `seq` 중복·역행 | `ERR <seq> ESEQ`, 무시 |

> **v2.0에서 정정 — ‰ 범위 초과.** v2.0은 "clamp 후 진행"이었으나 **거부**로 바꿨다. 범위 밖 ‰가 도착한다는 것은 서버 버그이거나 시리얼 노이즈다. 조용히 잘라 움직이면 원인을 찾을 수 없고, 잘못된 자세가 정상처럼 표현된다. 각도 단계의 clamp는 이중 방어로 그대로 둔다 — 그쪽은 설정 오류를 잡는 최후 보루이므로 성격이 다르다. (DECISIONS §32)

> **`detach()` 하지 않는 이유.** 전기팀 테스트 코드는 시연 후 `detach()`로 서보 전원을 끊었으나, 세션 중 detach하면 다음 명령에 재부착 지연이 생긴다. 5분 세션 동안 유지해도 발열에 문제없음을 확인했다.

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

CREATE TABLE learner_difficulty (
    id           INTEGER PRIMARY KEY,
    learner_id   INTEGER NOT NULL REFERENCES learners(id),
    track        TEXT NOT NULL,           -- discrimination|word
    length       INTEGER NOT NULL,        -- ②축
    hold_ms      INTEGER NOT NULL,        -- ①축
    boundary     TEXT NOT NULL,           -- ③축. explicit|direct
    updated_at   TIMESTAMP NOT NULL,
    UNIQUE (learner_id, track)
);
```

**`learner_difficulty`는 마지막 상태만 담는다.** 학습자·트랙당 한 행이고 시계열이 아니다. 세션 간 난이도 이월에 필요한 것은 "마지막에 어디까지 갔는가"뿐이며, 추이가 필요해지면 그때 이력 테이블을 따로 만든다. 트랙별로 행이 나뉘는 것은 판별 난이도와 단어 난이도가 별개이기 때문이다 — `UNIQUE (learner_id, track)`이 두 트랙이 섞일 자리를 스키마에서 없앤다. (DECISIONS §40)

**`transition_ms`는 저장하지 않는다.** 난이도 축이 아니라 오답 재생에서만 늘리는 값이라(9.1), 이월하면 다음 세션이 느린 재생 상태로 시작한다. 복원 시 항상 `timing.transition_ms`로 되돌린다.

**`updated_at`은 지금 쓰지 않는다.** 오래 쉰 학습자의 난이도를 낮추는 감쇠는 근거가 없어 만들지 않았다. 나중에 실데이터로 판단할 수 있도록 시각만 남긴다.

**`unlisted` 컬럼은 만들지 않는다.** 9.3은 `confusion_pairs`에 없는 혼동을 `unlisted`로 표시한다고 했으나, 컬럼으로 굳히면 `confusion_pairs`가 바뀔 때 과거 기록만 낡은 판정으로 남는다. 3주차 판별 테스트로 세트가 바뀔 가능성이 실재하므로 **집계 시점에 계산**한다. (DECISIONS §18)

**신규 개발이므로 마이그레이션 경로는 없다.** v1 스키마는 폐기했고 DB는 처음부터 이 스키마로 생성한다.

---

## 9. 핵심 로직

### 9.1 난이도 3축

| 축 | 파라미터 | 범위 | 기본 | 상태 |
|---|---|---|---|---|
| ① 유지 시간 | `hold_ms` | 800 ~ 3000 | 3000 | **정상.** 하한 확정으로 되살아남 (TBD-2) |
| ② 시퀀스 길이 | `len(letters)` | 3 ~ 7 | 3 | 정상 |
| ③ 글자 경계 | `boundary` | explicit / direct | explicit | 정상 |

`transition_ms`는 난이도 축이 아니다. 기본 900, 오답 1회 느린 재생에서만 `transition_slow_ms`를 쓴다.

**①축 확정 (2026-09-08).** 하한 2000ms는 정착 시간도 부하 한계도 아니었다. **하한 자체가 불필요**했고 800ms까지 안정 동작을 확인해 `hold_min_ms`를 내렸다. 정착 시간 가설이 기각되었으므로 `observe_from`은 `arrival`, `settle_ms`는 0을 유지한다. 예상대로 **코드는 바뀌지 않았고 설정만 바뀌었다.**

조절 폭이 1.5배(2000~3000)에서 3.75배(800~3000)로 늘어 ①축이 난이도 축으로 성립한다. 감소 단계는 3단계에서 9단계가 되었다.

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
- 혼동 기록은 `confusion_pairs`에 있는 쌍만 통계에 쓴다. 나머지도 저장하되, 목록 포함 여부는 **집계 시점에 판정**한다(8장 참조)

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

- `correct`가 3000→2550→2167→1841→1564→1329→1129→959→815→800에서 바닥을 친다(9단계). 그 뒤 정답은 `length` 증가 또는 `direct` 전환으로 넘어간다 (②·③축 우선 순서: length 3→5 → direct → length 5→7)
- **부작용:** ①축이 길어져 **한 세션 안에서는** ②·③축이 거의 발동하지 않는다. 전부 정답인 학습자도 8문항 안에 1~2단까지만 오른다. 다만 난이도가 세션을 넘어 이어지므로(아래) 여러 세션에 걸쳐서는 사다리 끝까지 오른다. 감쇠 계수(0.85)나 단 전환 조건을 조정할지는 4주차 실데이터로 판단한다 (DECISIONS §39)
- **①축은 양방향이다.** 바닥을 친 뒤에도 오답이 나면 유지 시간이 회복된다. 단조 증가로 두면 축 ①이 바닥에 고정된 채 다른 축만 올라가, 후퇴한 학습자가 회복 수단을 잃는다. 난이도 축이 서로 독립이라는 전제가 깨진다. (DECISIONS §23)
- **난이도 사다리에 데드엔드가 없어야 한다.** 오답 2회로 길이가 5→4로 내려갔을 때 `(4, explicit)`이 사다리에 없으면 "다음 칸 없음"이 되어 후퇴한 학습자가 영영 올라오지 못한다. 후퇴 경로에서 도달 가능한 모든 조합이 사다리 위에 있어야 하며, 회귀 테스트로 고정한다.
- `order` 결과는 `wrong_first`와 같이 처리하되 `demo_pair()` 대상이 없으므로 2회째도 시범 없이 후퇴만 한다
- `demo_pair()`: 혼동 첫 위치의 (expected, given) 쌍을 `demo_letter(expected, HOLD_MAX) → neutral → demo_letter(given, HOLD_MAX)`로 시범. 힌트 문구는 `confusion_pairs[].hint`에서 조회. 시연 ④의 하이라이트
- **적응 로직은 교체 가능한 인터페이스로 분리한다.** 구현은 규칙 기반(`RuleAdaptation`)이다. **문항 내 적응은 LLM이 하지 않는다** — 13.1의 역할 분담(규칙은 판정·적응·변환·clamp·락·통계) 그대로다. LLM은 세션 계획 생성까지이고, 계획이 승인되면 그 안의 문항 진행·판정·난이도 이동은 전부 규칙이 한다. 인터페이스를 분리해 둔 것은 교체 가능성을 열어 둔 것이지 교체 계획이 아니다. (DECISIONS §47)

**난이도는 세션을 넘어 이어진다.** 세션 시작 시 `learner_difficulty`에서 트랙별로 복원하고, 문항에 답할 때마다 저장한다. 이력이 없는 신규 학습자만 초기값(`3글자 · explicit · hold_default`)에서 시작한다.

- **트랙별로 따로 간다.** 단어에서 틀렸다고 판별 문항이 쉬워지면 두 트랙을 섞는 것이다. `RuleAdaptation`은 그 문항의 트랙 난이도에만 적용된다
- **문항마다 저장한다.** 세션이 중간에 끊겨도 그때까지 오른 만큼은 남는다. 문항 기록(`items`)은 답할 때마다 남으므로, 난이도만 버리면 같은 사건을 두 규칙으로 다루는 셈이다
- 이월이 없으면 10세션을 해도 매번 3글자·또박또박·최대 유지에서 시작한다. "반복 세션으로 판별력을 기른다"는 훈련 모델의 경로가 막히고, 4주차 실데이터 수집(10명 × 2세션)에서 2세션째가 1세션째와 같은 난이도로 시작해 **반복 효과를 측정할 수 없다** (DECISIONS §40)

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
연결 시점 → ⑧ 캘리브레이션 해시            (불일치면 연결 거부 = 서버 기동 실패)
요청      → ① 세션 락 → ② 글자 존재 → ③ 커리큘럼 → ④ flex 조회·clamp
          → ⑤ 각도 변환·clamp → ⑥ rate limit → ⑦ 디바이스 idle → 전송
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
| ⑧ | serial 모드에서 펌웨어 해시 일치. **연결 시점에 한 번 확인한다** (요청 단위가 아니다) | `CAL_MISMATCH` — 연결 거부. 서버가 기동하지 못하므로 HTTP 응답은 나가지 않는다 |

**⑧은 요청 단위가 아니라 연결 시점이다.** 시리얼 접속 직후 핸드셰이크(`H`)로 대조하고, 불일치면 연결 자체를 거부한다 — 그래서 요청이 도착할 시점에는 ⑧이 이미 충족되어 있다. 단계에서 빼지 않는 이유는 여전히 구동의 전제 조건이기 때문이고, 요청 단위로 옮기지 않는 이유는 **게이트가 첫 구동 요청에서야 돌기 때문**이다: 기동 시와 유휴 복귀 시 나가는 안전 자세는 게이트를 지나지 않으므로, 요청 단위로 두면 해시가 틀린 펌웨어에 이미 잘못된 각도가 나간 뒤에 처음 검사하게 된다. 구현 위치는 `SerialDevice.connect()` 다. (DECISIONS §35, §48.2)

**rate limit은 구동 요청 단위다.** 글자 단위로 걸면 시퀀스가 스스로 차단된다. 시퀀스 내 글자 간격은 전이 시간이 물리적으로 보장한다.

| 추가 제한 | 값 | 초과 시 |
|---|---|---|
| 세션당 최대 구동 요청 | 200 | `429 SESSION_QUOTA` |
| 락 획득 실패 (타 세션 점유) | — | `409 LOCK_UNAVAILABLE` |
| 락 heartbeat | 15초 | |
| 락 만료 | 마지막 heartbeat + 60초 | |
| 서버 기동 시 | 고아 락 정리, 디바이스 안전 자세 | |

`LOCK_UNAVAILABLE`(다른 세션이 점유 중)과 `LOCK_NOT_HELD`(세션 없이 호출)는 원인이 다르므로 분리한다. `SESSION_QUOTA`도 `RATE_LIMIT`과 원인이 달라 묶지 않는다. 서버가 다르게 대응해야 하고, 로그에서 구분되어야 한다.

**락 heartbeat는 글자 단위로 찍는다.** 문항 단위로 찍으면 7글자 문항을 느린 재생(1800ms)·최대 유지(3000ms)로 낼 때 한 문항이 74초가 되어 만료 60초를 넘긴다. 난이도가 올라간 학습자가 오답을 내는 순간 — 즉 훈련이 가장 필요한 지점에서 — 세션이 자기 락을 잃고 죽는다. 글자 하나는 최대 4.8초라 주기 15초 안에 들어온다. (DECISIONS §25)

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
| `POST` | `/api/sessions/{id}/demo` | 시범 재요청. **시범이 이미 나온 뒤에만 허용** |
| `POST` | `/api/sessions/{id}/end` | 종료, 락 해제, 요약 (트랙별 정확도·순서 오류율·평균 응답 시간) |
| `POST` | `/api/sessions/{id}/heartbeat` | 락 유지 |

`/demo`는 v2.0에 없던 엔드포인트다. 학습자 화면의 "시범 다시 보기" 요구를 만족시킨다. **시범이 이미 제시된 뒤에만 허용한다** — 시범은 혼동한 두 글자를 나란히 보여주는 것이라 그 자체가 정답 공개이기 때문이다. (DECISIONS §26)

### 진단 / 에이전트 / 디바이스

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/stats/confusion?track=` | 혼동 행렬. `track` 필수 |
| `GET` | `/api/stats/learners/{id}` | 학습자 상세 (트랙 분리) |
| `POST` | `/api/agent/plan` | 자연어 → 계획 (실행 안 함) |
| `POST` | `/api/agent/plans/{id}/approve` | HITL 승인 → 세션 생성 |
| `POST` | `/api/agent/plans/{id}/reject` | 거부 |
| `GET` | `/api/device/status` | 상태, 모드, 캘리브레이션 해시 일치 여부 |
| `POST` | `/api/device/stop` | 비상 정지 (안전 자세). 세션은 유지 |
| `POST` | `/api/device/reset` | 강제 해제. 활성 세션을 `aborted` 로 닫고 락 반납 (DECISIONS §44) |

### WebSocket `/ws/session/{id}`

```
{ "type": "presenting", "index": 2, "total": 12, "track": "word", "length": 4 }
{ "type": "ready",      "index": 2, "letter_pos": 0 }    // 글자별 RDY
{ "type": "done",       "index": 2 }                     // 마지막 글자 DON. 입력 활성화
{ "type": "judged",     "result": "wrong" }              // 오답 1회째: confusions 없음
{ "type": "judged",     "result": "wrong", "confusions": [...] }   // 2회째
{ "type": "demo",       "letters": ["V","W"], "hint": "약지를 확인하세요. V는 굽힘, W는 폄" }
{ "type": "ended",      "summary": {...} }
{ "type": "error",      "code": "DEVICE_TIMEOUT" }
```

**오답 1회째에는 `expected`도 `confusions`도 응답·WS 어디에도 싣지 않는다.** 기획서 5.2가 "오답 1회 → 재생, 정답 미공개"이므로 재생 중에는 단서를 주지 않는다. v2.0의 예시는 `confusions`가 항상 포함되어 있었으나 정책과 어긋난다. (DECISIONS §27)

**정답은 클라이언트 표현과 타입 수준에서 분리한다.** 서버 내부용 정답 필드와 클라이언트로 나가는 문항 표현을 같은 객체에 두면, 직렬화 한 번으로 정답이 샌다. 클라이언트 표현을 직렬화한 결과에 정답 문자열이 등장하지 않음을 회귀 테스트로 고정한다.

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

> **승인은 13장(에이전트 계획) 경로에만 있다.** MCP 도구에는 사람 승인 단계가 없고, 스키마(각도·flex 파라미터 없음)와 안전 게이트가 막는다. 승인된 계획의 세션은 `present_sequence` · `demo_letter` 로 움직일 수 없다 — `PLAN_SESSION_RESTRICTED` (DECISIONS §62, 2026-09-29 추가).

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
    "hold_ms":  { "min": 800, "max": 3000, "note": "floor confirmed 2026-09-08" },
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

| 구분 | 사례 | 거부 코드 | 시연 방식 |
|---|---|---|---|
| **라이브 차단** | 지원하지 않는 글자 `C` | `UNSUPPORTED_LETTERS` | Claude Desktop에 "CAT 보여줘" → 구조화 실패 + 대안 목록 |
| **라이브 차단** | 세션 없는 구동 호출 | `LOCK_NOT_HELD` | `start_session` 없이 `present_sequence` |
| **라이브 차단** | 미해금 깊이쌍 채점 시도 | `CURRICULUM_VIOLATION` | 신규 학습자에게 `E-I-E` |
| **라이브 정지** | HITL 승인 대기 | — | 계획 생성 후 실행 안 됨 |
| **설명 슬라이드** | LLM 각도 지정 | — | 도구 스키마 화면 캡처: 파라미터 자체가 없음 |
| **설명 슬라이드** | 값 오류·clamp 이중화 | `ERANGE` | 단위 테스트 결과 화면 |
| **설명 슬라이드** | 캘리브레이션 불일치 | `CAL_MISMATCH` | 헤더 해시 한 글자 변경 → 연결 거부 |

전체 거부 코드는 아래와 같다. 시연에 쓰지 않는 것도 로그 패널에 나타날 수 있다. **`CAL_MISMATCH`는 예외다** — 해시 대조는 시리얼 연결 시점에 일어나고 불일치면 서버가 기동하지 못하므로, 로그 패널이 존재하는 시점에는 이 코드가 나올 수 없다. 매 요청마다 검사하는 게이트로 옮기지 않는다: 그렇게 하면 첫 검사 전에 이미 잘못된 각도가 손으로 나갈 수 있다. (DECISIONS §35, §48)

| 코드 | 계층 | 원인 |
|---|---|---|
| `LOCK_NOT_HELD` | 안전 게이트 ① | 세션 없이 구동 호출 |
| `LOCK_UNAVAILABLE` | 안전 게이트 ① | 다른 세션이 점유 중 |
| `UNSUPPORTED_LETTERS` | ② | `poses`에 없는 글자 |
| `CURRICULUM_VIOLATION` | ③ | 미해금 깊이쌍 |
| `RATE_LIMIT` | ⑥ | 구동 요청 간격 미달 |
| `SESSION_QUOTA` | ⑥ | 세션당 구동 요청 상한 초과 |
| `DEVICE_BUSY` | ⑦ | 디바이스 동작 중 |
| `CAL_MISMATCH` | 시리얼 연결 | 펌웨어 해시 불일치. 기동 시 연결 거부 |
| `DEVICE_TIMEOUT` | 어댑터 | `DON` 미수신 |
| `EPARSE` `ERANGE` `EBUSY` `ETIME` `ESEQ` | 펌웨어 | 6.5 참조 |

---

## 16. 개발 순서

### Phase 1 — Mock 기반 전 계층 (완료)

- [x] `poses.json` schema v2 (11자, 중립·안전 분리, 15쌍+힌트, curriculum, pins), 검증
- [x] `words.json` + 필터 로딩, 훅 단어 폴백
- [x] `calibration.py` — flex→angle, 이중 clamp, 방향 무가정
- [x] `DeviceAdapter`(동기) + `MockDevice`(시간 주입, 세대 번호)
- [x] DB 스키마, 판정(애너그램 `order`), 통계(트랙 필수 인자)
- [x] 문항 생성: 2트랙, 깊이쌍 게이팅, D 상한
- [x] 안전 게이트 ①~⑦ + 세션 락(글자 단위 heartbeat)
- [x] 세션 루프 + 교체 가능한 적응 인터페이스
- [x] REST + WebSocket + `practice.html`
- [x] `tools/gen_calibration.py` + 해시, 게이트 ⑧
- [x] 펌웨어: 프로토콜 파서, `seq` 에코, 상태 머신, 20ms 틱 보간, clamp

**완료 기준 (달성):** Mock으로 판별 8 + 단어 4 세션이 끝까지 돌고, 요약이 트랙별로 나온다.

### Phase 2 — 실기 결합 (진행 중)

- [ ] `SerialDevice` + 해시 핸드셰이크, `RDY`로 `ready_at` 대체
- [ ] 펌웨어 업로드 (전기팀 입회)
- [ ] `DEVICE_MODE=serial`로 Phase 1 완료 기준 재현
- [ ] 11자 각도 실기 확인 — **한 글자씩 확인 후 연속 구동**
- [x] 유지 시간 하한 성격 회신 반영 — 하한 불필요로 확정, 800ms로 인하 (`observe_from`은 `arrival` 유지)
- [ ] `mockhand.html` (폴백 시각화)

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
| TBD-2 | 유지 시간 범위 | **확정** | 800~3000. 하한 자체가 불필요했음. `observe_from=arrival` 유지 |
| TBD-3 | 전이 시간 | **확정** | 900 고정 |
| TBD-4 | 재캘리브레이션 주기 | 미확정 | 3~4주차 내구 테스트 |
| TBD-5 | 세션 소요 시간 | 확정 (응답 6초 추정) | 4주차 실측으로 갱신 |
| TBD-6 | 11자 육안 판별 | **확정** | 확인 완료 |
| — | 보드 종류 | **확정** | Arduino Uno |
| — | 핀 배치 | **확정** | `poses.json`의 `pins` 블록 |
| — | detach 없이 세션 유지 | **확정** | 발열 문제 없음. `detach()` 하지 않음 |
| — | 엄지 최대 굽힘 | **확정** | `closed: 80` 안전 |
| — | 30분 연속 구동 발열 | 미회신 | 세션 5분 기준으로 개발 |
| — | 시리얼 포트 번호 | 미회신 | `SERIAL_PORT` 환경변수 |
| — | 여분 부품 | 미회신 | 폴백 모드로 대비 |

**TBD-2는 2026-09-08 해소되었다.** 하한 2000ms는 정착 시간도 부하 한계도 아니었다 — 하한 자체가 불필요했고, 800ms까지 안정 동작이 확인되어 `hold_min_ms`를 내렸다. 정착 시간 가설이 기각되었으므로 `observe_from`은 `arrival`, `settle_ms`는 0을 유지한다. 예고한 대로 **코드는 바뀌지 않았고 설정 한 줄만 바뀌었다.** `hold_min_ms`는 `CAL_HASH` 대상이 아니므로 펌웨어 재업로드도 필요 없었다.

남은 과제는 ①축이 길어지면서 12문항 세션에서 ②·③축이 거의 발동하지 않는 점이다 (9.4, DECISIONS §39).

확인 방법: 아두이노에서 한 자세를 `delay(700)`로 반복 표현하고 (a) 손가락이 목표에 도달하지 못하고 흔들리는지 → 정착 시간, (b) 위치는 맞으나 발열·소음이 있는지 → 부하 한계, (c) 이상 없는지 → 하한 자체가 불필요.

**모든 하드웨어 종속 값은 `poses.json` 한 파일에 격리되어 있다. 값이 확정되면 파일만 교체하며 코드는 수정하지 않는다. 펌웨어 캘리브레이션 헤더는 생성 산출물이므로 재생성·재업로드 후 해시로 일치를 확인한다.**
