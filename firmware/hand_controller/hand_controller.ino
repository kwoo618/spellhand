// spellhand 로봇손 펌웨어 (PRD 7장).
//
// **이 펌웨어는 글자를 모른다** (R2). 자세 테이블도, 글자 이름도 없다.
// 시리얼로 받은 ‰(0~1000) 다섯 개를 각도로 바꿔 움직일 뿐이다. 무엇을
// 표현하고 있는지는 서버만 안다.
//
// 각도와 핀은 전부 calibration.h 에서 온다. 이 소스에 각도·핀 리터럴은 없다
// (R1). calibration.h 는 poses.json 에서 생성되며 손으로 고치지 않는다.
//
// 프로토콜 (PRD 6.5)
//   서버 -> 펌웨어
//     P <seq> <t> <i> <m> <r> <p> <trans_ms> <hold_ms>   이동·유지
//     H                                                   해시 조회
//     Z                                                   즉시 안전 자세
//     N                                                   중립 자세
//   펌웨어 -> 서버
//     ACK <seq>            명령 수신
//     RDY <seq>            목표 도달. 서버의 관측 시작 시점
//     DON <seq>            유지까지 완료
//     ERR <seq> <code>     거부. 자세는 바뀌지 않는다
//     HSH <hash>           H 응답
//
// seq 가 -1 인 응답은 P 명령에 딸리지 않은 것이다 (Z, N, 유휴 복귀).

#include <Servo.h>
#include "calibration.h"

// ---------------------------------------------------------------------------
// 거부 코드. 원인이 다르면 코드도 다르다 — 서버가 다르게 대응해야 하기 때문이다.
// ---------------------------------------------------------------------------
static const char ERR_PARSE[] = "EPARSE";  // 형식이 아니다
static const char ERR_RANGE[] = "ERANGE";  // ‰ 가 0~1000 밖
static const char ERR_TIME[]  = "ETIME";   // trans/hold 가 범위 밖
static const char ERR_BUSY[]  = "EBUSY";   // 아직 움직이는 중
static const char ERR_SEQ[]   = "ESEQ";    // seq 중복 또는 역행

static const long NO_SEQ = -1;

enum State { IDLE, MOVING, HOLDING };

Servo servos[FINGER_COUNT];

static int startAngle[FINGER_COUNT];
static int targetAngle[FINGER_COUNT];
static int liveAngle[FINGER_COUNT];

static State state = IDLE;
static long activeSeq = NO_SEQ;
static long lastSeq = NO_SEQ;

static unsigned long moveStartedAt = 0;
static unsigned long holdStartedAt = 0;
static unsigned long transitionMs = 0;
static unsigned long holdMs = 0;
static unsigned long lastTickAt = 0;
static unsigned long lastCommandAt = 0;
static bool idleParked = true;

static char line[96];
static size_t lineLength = 0;

// ---------------------------------------------------------------------------
// 각도 변환 — 서버와 같은 식이다 (PRD 5장). 이중 clamp 는 제거하지 않는다 (R4).
// ---------------------------------------------------------------------------

static int clampInt(int value, int low, int high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

// ‰ -> 각도. open 과 closed 의 대소는 손가락마다 다르므로 방향을 가정하지 않는다.
static int toAngle(int finger, int permille) {
  const int open = SERVO_OPEN[finger];
  const int closed = SERVO_CLOSED[finger];

  const int bounded = clampInt(permille, 0, PERMILLE_MAX);
  const long span = (long)(closed - open);
  int angle = open + (int)((span * (long)bounded) / (long)PERMILLE_MAX);

  // 손가락별 양 끝점으로 자르고, 서보 물리 범위로 한 번 더 자른다.
  // 서버가 이미 잘라서 보내지만 시리얼 노이즈나 서버 버그로 범위 밖 값이
  // 와도 서보가 상하지 않도록 여기서도 자른다 (DECISIONS.md §32).
  const int low = open < closed ? open : closed;
  const int high = open < closed ? closed : open;
  angle = clampInt(angle, low, high);
  return clampInt(angle, SERVO_RANGE_MIN, SERVO_RANGE_MAX);
}

static void writeAngles(const int angles[FINGER_COUNT]) {
  for (int i = 0; i < FINGER_COUNT; i++) {
    liveAngle[i] = angles[i];
    servos[i].write(angles[i]);
  }
}

// ---------------------------------------------------------------------------
// 응답
// ---------------------------------------------------------------------------

static void reply(const char* tag, long seq) {
  Serial.print(tag);
  Serial.print(' ');
  Serial.println(seq);
}

static void replyError(long seq, const char* code) {
  Serial.print("ERR ");
  Serial.print(seq);
  Serial.print(' ');
  Serial.println(code);
}

// ---------------------------------------------------------------------------
// 동작
// ---------------------------------------------------------------------------

// 목표를 잡고 보간을 시작한다. transition 이 0 이면 즉시 도달한다.
static void beginMove(long seq, const int targets[FINGER_COUNT],
                      unsigned long trans, unsigned long hold) {
  for (int i = 0; i < FINGER_COUNT; i++) {
    startAngle[i] = liveAngle[i];
    targetAngle[i] = targets[i];
  }
  activeSeq = seq;
  transitionMs = trans;
  holdMs = hold;
  moveStartedAt = millis();
  lastTickAt = moveStartedAt;
  state = MOVING;
  idleParked = false;

  if (trans == 0) {
    writeAngles(targetAngle);
    reply("RDY", activeSeq);
    holdStartedAt = millis();
    state = HOLDING;
  }
}

static void anglesFromPermille(const int permille[FINGER_COUNT],
                               int out[FINGER_COUNT]) {
  for (int i = 0; i < FINGER_COUNT; i++) {
    out[i] = toAngle(i, permille[i]);
  }
}

// 안전 자세: 텐던 장력이 가장 낮은 상태. 부팅·비상정지·유휴 복귀에 쓴다.
static void moveToSafe(long seq, unsigned long trans) {
  int angles[FINGER_COUNT];
  anglesFromPermille(POSE_SAFE_PERMILLE, angles);
  beginMove(seq, angles, trans, 0);
}

// 중립 자세: 글자 사이 경계 신호. **안전 자세와 별개다** (R3).
static void moveToNeutral(long seq) {
  int angles[FINGER_COUNT];
  anglesFromPermille(POSE_NEUTRAL_PERMILLE, angles);
  beginMove(seq, angles, DEFAULT_TRANSITION_MS, 0);
}

// 이미 안전 자세에 있는가. 유휴 복귀가 헛돌지 않도록 확인한다.
static bool atSafePose() {
  for (int i = 0; i < FINGER_COUNT; i++) {
    if (liveAngle[i] != toAngle(i, POSE_SAFE_PERMILLE[i])) return false;
  }
  return true;
}

static void advanceMotion() {
  const unsigned long now = millis();

  if (state == MOVING) {
    if (now - lastTickAt < INTERPOLATION_TICK_MS) return;
    lastTickAt = now;

    const unsigned long elapsed = now - moveStartedAt;
    if (elapsed >= transitionMs) {
      // 마지막 스텝은 계산하지 않고 목표를 그대로 쓴다. 반올림 오차가 남아
      // "도착했는데 목표와 다른" 상태가 되는 것을 막는다.
      writeAngles(targetAngle);
      reply("RDY", activeSeq);
      holdStartedAt = now;
      state = HOLDING;
      return;
    }

    // 다섯 손가락이 같은 비율로 움직여 동시에 도달한다 (PRD 7.3).
    int stepped[FINGER_COUNT];
    for (int i = 0; i < FINGER_COUNT; i++) {
      const long span = (long)(targetAngle[i] - startAngle[i]);
      stepped[i] = startAngle[i] + (int)((span * (long)elapsed) / (long)transitionMs);
    }
    writeAngles(stepped);
    return;
  }

  if (state == HOLDING) {
    if (now - holdStartedAt < holdMs) return;
    reply("DON", activeSeq);
    state = IDLE;
    lastCommandAt = now;
    activeSeq = NO_SEQ;
    return;
  }

  // IDLE. 오래 아무 명령이 없으면 안전 자세로 돌아간다.
  // detach 하지 않는다 — 다음 명령에서 재부착 지연이 생긴다 (DECISIONS.md §33).
  if (!idleParked && now - lastCommandAt >= IDLE_TIMEOUT_MS) {
    idleParked = true;
    if (!atSafePose()) {
      moveToSafe(NO_SEQ, DEFAULT_TRANSITION_MS);
    }
  }
}

// ---------------------------------------------------------------------------
// 파싱
// ---------------------------------------------------------------------------

// 공백으로 끊어 다음 정수를 읽는다. 숫자가 아니거나 끝이면 false.
static bool nextLong(char** cursor, long* out) {
  char* p = *cursor;
  while (*p == ' ') p++;
  if (*p == '\0') return false;

  char* end = NULL;
  const long value = strtol(p, &end, 10);
  if (end == p) return false;      // 숫자로 시작하지 않았다
  if (*end != ' ' && *end != '\0') return false;  // 숫자 뒤에 쓰레기가 붙었다

  *out = value;
  *cursor = end;
  return true;
}

static void handleMove(char* cursor) {
  long seq = NO_SEQ;
  if (!nextLong(&cursor, &seq)) {
    replyError(NO_SEQ, ERR_PARSE);
    return;
  }

  long permille[FINGER_COUNT];
  for (int i = 0; i < FINGER_COUNT; i++) {
    if (!nextLong(&cursor, &permille[i])) {
      replyError(seq, ERR_PARSE);
      return;
    }
  }

  long trans = 0;
  long hold = 0;
  if (!nextLong(&cursor, &trans) || !nextLong(&cursor, &hold)) {
    replyError(seq, ERR_PARSE);
    return;
  }
  while (*cursor == ' ') cursor++;
  if (*cursor != '\0') {  // 필드가 남았다
    replyError(seq, ERR_PARSE);
    return;
  }

  // seq 는 서버가 붙이는 단조 증가 정수다. 되돌아오거나 겹치면 응답을 엉뚱한
  // 글자에 대응시키게 되므로 거부한다.
  if (seq < 0 || (lastSeq != NO_SEQ && seq <= lastSeq)) {
    replyError(seq, ERR_SEQ);
    return;
  }

  // 범위 밖 ‰ 는 자르지 않고 거부한다. 서버가 이미 자르므로, 범위 밖 값이
  // 왔다는 것 자체가 노이즈나 버그 신호다. 그 상태에서 추측해 움직이지 않는다
  // (DECISIONS.md §32).
  for (int i = 0; i < FINGER_COUNT; i++) {
    if (permille[i] < 0 || permille[i] > PERMILLE_MAX) {
      replyError(seq, ERR_RANGE);
      return;
    }
  }

  if (trans < 0 || (unsigned long)trans > MAX_TRANSITION_MS ||
      hold < 0 || (unsigned long)hold > MAX_HOLD_MS) {
    replyError(seq, ERR_TIME);
    return;
  }

  if (state != IDLE) {
    replyError(seq, ERR_BUSY);
    return;
  }

  lastSeq = seq;
  lastCommandAt = millis();
  reply("ACK", seq);

  int angles[FINGER_COUNT];
  for (int i = 0; i < FINGER_COUNT; i++) {
    angles[i] = toAngle(i, (int)permille[i]);
  }
  beginMove(seq, angles, (unsigned long)trans, (unsigned long)hold);
}

static void handleLine(char* text) {
  while (*text == ' ') text++;
  const char command = *text;
  char* cursor = text + 1;

  switch (command) {
    case 'P':
      handleMove(cursor);
      return;

    case 'H':
      Serial.print("HSH ");
      Serial.println(CAL_HASH);
      lastCommandAt = millis();
      return;

    case 'Z':
      // 비상 정지. 진행 중이던 동작을 버리고 즉시 안전 자세로 간다.
      reply("ACK", NO_SEQ);
      moveToSafe(NO_SEQ, 0);
      lastCommandAt = millis();
      return;

    case 'N':
      if (state != IDLE) {
        replyError(NO_SEQ, ERR_BUSY);
        return;
      }
      reply("ACK", NO_SEQ);
      moveToNeutral(NO_SEQ);
      lastCommandAt = millis();
      return;

    default:
      replyError(NO_SEQ, ERR_PARSE);
      return;
  }
}

static void readSerial() {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();

    if (c == '\r') continue;
    if (c == '\n') {
      line[lineLength] = '\0';
      if (lineLength > 0) handleLine(line);
      lineLength = 0;
      continue;
    }

    if (lineLength + 1 >= sizeof(line)) {
      // 줄이 너무 길다. 버리고 다음 줄부터 다시 본다.
      lineLength = 0;
      replyError(NO_SEQ, ERR_PARSE);
      continue;
    }
    line[lineLength++] = c;
  }
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);

  for (int i = 0; i < FINGER_COUNT; i++) {
    servos[i].attach(SERVO_PIN[i]);
    liveAngle[i] = toAngle(i, POSE_SAFE_PERMILLE[i]);
  }

  // 부팅 직후는 안전 자세다. 중립이 아니다 (R3).
  int angles[FINGER_COUNT];
  anglesFromPermille(POSE_SAFE_PERMILLE, angles);
  writeAngles(angles);

  state = IDLE;
  lastCommandAt = millis();
  idleParked = true;
}

void loop() {
  // loop() 에서 delay() 를 쓰지 않는다. millis() 기반 틱이다 (PRD 7.2).
  readSerial();
  advanceMotion();
}
