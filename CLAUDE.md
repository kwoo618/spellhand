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

- `docs/개발_PRD_v2.md` — 구현 사양. 절 번호로 참조한다
- `docs/지문자_학습_로봇손_기획서_v3.md` — 왜 이렇게 만드는지. 설계 의도
- `docs/하드웨어_측정_요청서.md` — 전기팀 회신값의 출처

**문서보다 코드가 우선인 경우가 있다.** 문서 작성 후 대화에서 바뀐 결정이 있고,
그 근거는 `DECISIONS.md`에 있다. 문서와 코드가 어긋나면 임의로 문서 쪽에 맞추지 말고
`DECISIONS.md`를 확인한 뒤, 그래도 불분명하면 멈추고 물어본다.

알려진 불일치:
- PRD 4.1의 poses.json 전문에는 `calibration.servo_range_deg` 가 없다.
  실제 파일에는 있다. 코드가 맞다 (DECISIONS.md §2)
- PRD 2장은 Python 3.11+ 라고 적혀 있으나 실제 환경은 3.12
- PRD 16장은 v1→v2 마이그레이션을 전제로 쓰였다. 실제로는 신규 개발이다

## 현재 상태

- 목표: 신규 개발. 기준 문서는 기획서 v3 + PRD v2.
  1차 완료 기준은 Mock에서 판별 8 + 단어 4 세션이 끝까지 돌고 요약이 트랙별로 나오는 것
- 완료: config 계층 (poses.json schema v2 + 검증 34테스트), words.json + 필터 (24테스트)
- 진행 중: server/calibration.py (flex → 각도 변환)
- 막힌 것: 유지 시간 2000ms 하한의 성격(정착 시간 vs 부하 한계) 미회신 → `observe_from=arrival`로 진행
- 미해결: 제3자 판별 파일럿 미실시. 11자 세트는 아직 지각적으로 검증되지 않음
- 다음: DeviceAdapter + MockDevice → DB 스키마 → judge → items → safety → session → REST/WS
