#include <Arduino.h>

static const uint32_t BAUD = 115200;

// --- LED configuration (ESP32) ---
// If LED_BUILTIN exists, use it; otherwise default to GPIO 2 (common on ESP32 DevKit).
#ifndef LED_BUILTIN
  #define LED_BUILTIN 2
#endif

static const int LED_PIN = LED_BUILTIN;

// Set to 1 if your onboard LED turns ON with LOW (active-low).
// If your LED behavior is inverted, change this to 1.
static const bool LED_ACTIVE_LOW = false;

// ---------------- Executor state ----------------
enum Mode : uint8_t { IDLE, BLINKING, HOLDING, PATTERNING };
static Mode mode = IDLE;

static bool ledState = false;     // logical state: true=ON, false=OFF
static uint32_t nextChangeMs = 0;

// BLINK params
static uint16_t blinkCyclesRemaining = 0;
static uint32_t blinkOnMs = 200;
static uint32_t blinkOffMs = 200;

// HOLD params
static uint32_t holdEndMs = 0;

// PATTERN params
static const uint8_t MAX_SEQ = 50;
static uint32_t seqMs[MAX_SEQ];
static uint8_t seqLen = 0;
static uint8_t seqIndex = 0;
static uint8_t patternRepeat = 1;
static uint8_t patternRepeatDone = 0;

// Serial line buffer
static const uint16_t BUF_LEN = 128;
static char buf[BUF_LEN];
static uint16_t bufIdx = 0;

static void setLed(bool on) {
  ledState = on;
  if (!LED_ACTIVE_LOW) {
    digitalWrite(LED_PIN, on ? HIGH : LOW);
  } else {
    digitalWrite(LED_PIN, on ? LOW : HIGH);
  }
}

static void stopAll() {
  mode = IDLE;
  blinkCyclesRemaining = 0;
  seqLen = 0;
  seqIndex = 0;
  patternRepeatDone = 0;
  nextChangeMs = 0;
  holdEndMs = 0;
  setLed(false);
}

static bool parseUInt(const char* s, uint32_t &out) {
  if (!s || !*s) return false;
  char* endp = nullptr;
  unsigned long v = strtoul(s, &endp, 10);
  if (endp == s || *endp != '\0') return false;
  out = (uint32_t)v;
  return true;
}

static void replyOK(const char* msg) {
  Serial.print("OK ");
  Serial.println(msg);
}

static void replyERR(const char* msg) {
  Serial.print("ERR ");
  Serial.println(msg);
}

static void handleCommand(char* line) {
  const char* delim = " \t";
  char* cmd = strtok(line, delim);
  if (!cmd) return;

  // Deterministic: any new command cancels current program.
  stopAll();

  if (strcmp(cmd, "SET") == 0) {
    char* sState = strtok(nullptr, delim);
    if (!sState) return replyERR("SET missing state");
    if (strcmp(sState, "1") == 0) { setLed(true);  return replyOK("SET 1"); }
    if (strcmp(sState, "0") == 0) { setLed(false); return replyOK("SET 0"); }
    return replyERR("SET state must be 0 or 1");
  }

  if (strcmp(cmd, "BLINK") == 0) {
    char* sCount = strtok(nullptr, delim);
    char* sOn = strtok(nullptr, delim);
    char* sOff = strtok(nullptr, delim);
    uint32_t count, onms, offms;
    if (!parseUInt(sCount, count) || !parseUInt(sOn, onms) || !parseUInt(sOff, offms))
      return replyERR("BLINK expects: BLINK <count> <on_ms> <off_ms>");

    if (count < 1 || count > 100) return replyERR("BLINK count 1..100");
    if (onms < 10 || onms > 60000) return replyERR("BLINK on_ms 10..60000");
    if (offms < 10 || offms > 60000) return replyERR("BLINK off_ms 10..60000");

    blinkCyclesRemaining = (uint16_t)count;
    blinkOnMs = onms;
    blinkOffMs = offms;

    mode = BLINKING;
    setLed(true);
    nextChangeMs = millis() + blinkOnMs;
    replyOK("BLINK start");
    return;
  }

  if (strcmp(cmd, "HOLD") == 0) {
    char* sDur = strtok(nullptr, delim);
    uint32_t dur;
    if (!parseUInt(sDur, dur)) return replyERR("HOLD expects: HOLD <duration_ms>");
    if (dur < 10 || dur > 600000) return replyERR("HOLD duration_ms 10..600000");

    mode = HOLDING;
    setLed(true);
    holdEndMs = millis() + dur;
    replyOK("HOLD start");
    return;
  }

  if (strcmp(cmd, "PATTERN") == 0) {
    char* sRepeat = strtok(nullptr, delim);
    char* sN = strtok(nullptr, delim);
    uint32_t rep, n;
    if (!parseUInt(sRepeat, rep) || !parseUInt(sN, n))
      return replyERR("PATTERN expects: PATTERN <repeat> <n> <d1>.. <dn>");

    if (rep < 1 || rep > 50) return replyERR("PATTERN repeat 1..50");
    if (n < 1 || n > MAX_SEQ) return replyERR("PATTERN n 1..50");

    seqLen = (uint8_t)n;
    for (uint8_t i = 0; i < seqLen; i++) {
      char* sDi = strtok(nullptr, delim);
      uint32_t di;
      if (!parseUInt(sDi, di)) return replyERR("PATTERN missing duration");
      if (di < 10 || di > 60000) return replyERR("PATTERN duration 10..60000");
      seqMs[i] = di;
    }

    patternRepeat = (uint8_t)rep;
    patternRepeatDone = 0;
    seqIndex = 0;

    mode = PATTERNING;
    setLed(true); // starts ON
    nextChangeMs = millis() + seqMs[0];
    replyOK("PATTERN start");
    return;
  }

  if (strcmp(cmd, "STOP") == 0) {
    stopAll();
    replyOK("STOP");
    return;
  }

  replyERR("Unknown command");
}

static void tickExecutor() {
  uint32_t now = millis();

  if (mode == BLINKING) {
    if ((int32_t)(now - nextChangeMs) >= 0) {
      if (ledState) {
        // ON -> OFF
        setLed(false);
        nextChangeMs = now + blinkOffMs;

        if (blinkCyclesRemaining > 0) blinkCyclesRemaining--;
        if (blinkCyclesRemaining == 0) {
          mode = IDLE;
          replyOK("BLINK done");
        }
      } else {
        // OFF -> ON (next cycle)
        if (blinkCyclesRemaining > 0) {
          setLed(true);
          nextChangeMs = now + blinkOnMs;
        } else {
          mode = IDLE;
        }
      }
    }
    return;
  }

  if (mode == HOLDING) {
    if ((int32_t)(now - holdEndMs) >= 0) {
      stopAll();
      replyOK("HOLD done");
    }
    return;
  }

  if (mode == PATTERNING) {
    if ((int32_t)(now - nextChangeMs) >= 0) {
      seqIndex++;
      if (seqIndex >= seqLen) {
        seqIndex = 0;
        patternRepeatDone++;
        if (patternRepeatDone >= patternRepeat) {
          stopAll();
          replyOK("PATTERN done");
          return;
        }
      }
      setLed(!ledState);
      nextChangeMs = now + seqMs[seqIndex];
    }
    return;
  }
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  setLed(false);

  Serial.begin(BAUD);
  delay(200);
  Serial.println("READY");
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (bufIdx > 0) {
        buf[bufIdx] = '\0';
        handleCommand(buf);
        bufIdx = 0;
      }
    } else {
      if (bufIdx < BUF_LEN - 1) buf[bufIdx++] = c;
    }
  }

  tickExecutor();
}
