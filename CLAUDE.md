# spellhand

<!-- 사용법: 먼저 `/init` 실행 → 생성된 초안의 "스택" 부분만 남기고, 아래 섹션을 붙인다. -->

## 스택

Python 3.12 (.venv) / FastAPI (REST+WS) / SQLite+SQLAlchemy 2.x / pyserial(115200) /
mcp Python SDK(stdio) / Anthropic API / vanilla JS / Arduino C++ (Servo.h) / pytest

python / pytest / pip 는 항상 `.venv\Scripts\` 아래 것을 직접 호출한다.
전역 python은 3.10.11이며 의존성이 없다.

## 완료 판정

작업 후 반드시 실행:

- `powershell -ExecutionPolicy Bypass -File scripts\verify.ps1`

실패하면 스스로 고치고 재실행한다. **2회 실패 시 멈추고 보고한다.**
추측으로 통과시키지 않는다. 테스트를 고쳐서 통과시키지 않는다.

## 승인 필요 (실행 전 물어볼 것)

- DB 마이그레이션, `station.db` 조작·삭제
- `config/poses.json` · `config/words.json` 의 **값** 변경 (스키마 변경도 포함)
- `tools/gen_calibration.py` 실행 및 펌웨어 업로드
- `DEVICE_MODE=serial` 실기 구동 (전기팀 입회 없이 금지)
- `git push`, 외부 발송, Anthropic API 대량 호출

> 여기 적는 것으로 끝내지 말고 `.claude/settings.json`의 `ask`/`deny`로 실제로 막는다.

## 불변 규칙

R1. 하드웨어 종속 값은 `config/poses.json`에만 존재한다. 코드에 각도·flex 리터럴을 쓰지 않는다.
R2. `DeviceAdapter`는 Pose 객체(flex 5개)를 받는다. 글자 이름을 넘기지 않는다. 펌웨어는 글자를 모른다.
R3. 중립 자세와 안전 자세는 별개다. 서로 대체하지 않는다.
R4. clamp · 세션 락 · 캘리브레이션 해시가 최소 방어선이다. 이 셋의 테스트를 약화시키지 않는다.
R5. 추측으로 빈칸을 채우지 않는다. TBD는 TBD로 남긴다. 규칙을 바꾸면 `DECISIONS.md`에 이유를 남긴다.

## 건드리지 말 것

- `firmware/hand_controller/calibration.h` — 생성 산출물. 수기 편집 금지, `tools/gen_calibration.py`로만
- `config/poses.json`의 수치 — 전기팀 확정값
- `station.db` — 직접 조작 금지. 마이그레이션 스크립트로만
- `tests/` 중 clamp·락·해시 3종 — 구현을 통과시키려고 테스트를 완화하지 않는다

## 이 코드베이스의 관습

- 새 스타일을 도입하지 말고 주변 파일의 방식을 따른다
- 트랙(`discrimination` / `word`) 통계는 어떤 경로로도 섞이지 않는다
- 실패는 구조화된 형태로 반환한다. 에러 문자열을 즉흥 생성하지 않는다
- 요청한 것만 한다. 지나가다 본 코드를 리팩터링하지 않는다

## 기준 문서

`docs/` 아래에 있다. 작업 전 관련 절을 직접 읽는다.

- `docs/개발_PRD_v2.1.md` — 구현 사양. 절 번호로 참조한다. **이쪽이 기준이다**
- `docs/개발_PRD_v2.md` — 구본. 이력용으로만 남겨 둔다
- `docs/지문자_학습_로봇손_기획서_v3.md` — 왜 이렇게 만드는지. 설계 의도
- `docs/하드웨어_측정_요청서.md` — 전기팀 회신값의 출처

**문서보다 코드가 우선인 경우가 있다.** 문서 작성 후 대화에서 바뀐 결정이 있고,
그 근거는 `DECISIONS.md`에 있다. 문서와 코드가 어긋나면 임의로 문서 쪽에 맞추지 말고
`DECISIONS.md`를 확인한 뒤, 그래도 불분명하면 멈추고 물어본다.

알려진 불일치:
- **PRD 보드 표기.** PRD 8행과 18장 표는 `Arduino Uno`로 적혀 있으나 실제
  업로드·구동은 **UNO R4 WiFi(COM3)** 다. 코드가 맞다 — 핀은 `poses.json`에서
  오고 펌웨어는 보드 모델을 모른다
- **`station.html` heartbeat 주석.** 학습자 스크립트 끝의 heartbeat 주석은 "15초 주기"라고 적혀 있으나
  실제 `setInterval`은 10초다. **코드가 맞다** — PRD 10장의 15초는 만료(60초)
  전에 넘겨야 하는 *상한*이고, 10초는 여유를 둔 것이다. 고칠 것은 주석이다
- **PRD 14장 화면 주소.** PRD 는 `/practice.html` · `/teacher.html` 두 페이지로 적고
  있으나 지금은 `station.html` 한 화면의 두 모드다 (DECISIONS §52). 예전 주소는
  리다이렉트로 살아 있다
- v2.1이 구본 v2의 불일치 3건(`servo_range_deg` 누락, Python 버전,
  마이그레이션 전제)은 모두 해소했다. 새로 어긋나는 것을 발견하면 여기 적는다

## 현재 상태

- 목표: 신규 개발. 기준 문서는 기획서 v3 + PRD v2.1.
  **1차 완료 기준(Mock에서 판별 8 + 단어 4 세션 완주 + 트랙별 요약)은 달성했다.**

- 완료 (611테스트):
  - `config` — poses.json schema v2(핀 포함) + words.json 필터
  - `calibration` — flex ↔ 각도, 이중 clamp, CAL_HASH
  - `device` — Pose/DeviceAdapter(동기), MockDevice, SerialDevice, 팩토리
  - `db` `judge` `stats` `items` `safety` `session` — 세션 루프와 안전 게이트
  - `routes` — REST + WS, `station.html`, `mockhand.html`
  - `firmware/hand_controller` — P/H/Z/N 프로토콜. **UNO R4 WiFi(COM3)에 업로드·구동 확인**
  - `tools/gen_calibration.py` — calibration.h 생성 + `--check`
  - `mcp_server/server.py` — 3층 도구 9개, 구조화된 실패, `/ws/mcp-log`.
    스테이션의 REST 클라이언트다 (DECISIONS §46)
  - `server/agent.py` — 계획 생성 + 검증 10항목 + HITL + 규칙 폴백.
    **LLM은 계획 생성까지만이고 문항 내 적응은 규칙이다** (DECISIONS §47)
  - `web/station.html` — 학습자/교사 두 모드 한 화면 (DECISIONS §52). 학습자 모드는
    훈련 + "내 진행"(난이도·깊이쌍 해금 진척도·세션별 정확도, 혼동 데이터 없음).
    교사 모드는 진단 뷰 (PRD 14.2). MCP 로그 패널, 계획 생성·검증
    결과, HITL 승인·거부, 트랙별 통계. 승인 거부는 코드를 그대로 보이고
    다음 동작을 한 줄 덧붙인다. 락이 물린 경우에만 손 놓기를 내놓는다
    (DECISIONS §45 · §48)

- 진행 중: 없음

- 막힌 것: 없음

- 미해결:
  - **제3자 판별 파일럿 미실시.** 11자 세트가 지각적으로 검증되지 않았다 (3주차)
  - **①축이 ②·③축을 세션 안에서 밀어낸다** (DECISIONS §39 첫 항목). 감쇠
    계수·단 전환 조건 조정은 4주차 실측 오답률로 판단한다
  - **E 약지 — 경유(approach)로 임시 우회, 실기 확인 전** (DECISIONS §55).
    펴진 상태에서 오면 약지가 덜 접힌다(텐던 히스테리시스). E 는 S 를 거쳐
    간다. 경유 시간 260/200ms 는 **추정값이고 실기 확인 전까지 미확정**이다.
    전기팀 입회 실기 확인이 남았다. E 수치 자체는 바꾸지 않았다. 하드웨어가
    고쳐지면 `approach` 를 지운다. 경유 여부는 `items` 에 없고 `letters` + 당시
    `poses.json` 으로 복원한다
  - **mockhand 주먹 자세에서 손가락이 겹쳐 그려진다.** 미수정. 발표 폴백 화면이라
    고쳐야 한다. 시각화만의 문제이고 flex 값·실기 동작과는 무관하다
  - **승인과 참여 사이 60초 창 (DECISIONS §50.5) — 고치지 않기로 결정했다.**
    미수정이 아니다. 승인 뒤 60초 안에 참여하지 않으면 락이 만료되어 승인된
    세션이 사라지는데, 이것은 락 만료가 설계대로 동작한 것이다. "학습자가 화면
    앞에 있을 때 승인한다" 는 운영 절차로 처리한다. **락 만료·heartbeat 로 이 창을
    메우려 하지 않는다** (R4). 다시 볼 조건은 4주차 실측에서 이것으로 날아간
    세션이 나올 때다
  - 위 "알려진 불일치" 3건(PRD 보드 표기, heartbeat 주석, PRD 14장 화면 주소)

- 다음: 제3자 판별 파일럿(위 미해결 첫 항목) → 실데이터 수집(10명 × 2세션,
  최소 6명, **승인 후 바로 참여** — DECISIONS §50.5) → 시연 리허설 6구간
  전체. **셋 다 사람을 모아야 하는 일이고, 코드에서 막혀 있는 것은 없다.**
