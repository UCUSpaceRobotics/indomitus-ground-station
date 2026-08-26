#include <Arduino.h>
#include <Wire.h>

// ---------- I2C bus pins (ESP32-S3-DevKitC-1 default I2C pins) ----------
static const int I2C_SDA_PIN = 8;
static const int I2C_SCL_PIN = 9;

// ---------- PCF8575 address ----------
// 7-bit address = 0b0100 (A2 A1 A0) -> 0x20 | (A2<<2) | (A1<<1) | A0
// A0=0, A1=0, A2=1  ->  0x20 | 0b100 = 0x24
static const uint8_t PCF_ADDR = 0x24;

// ---------- Switch pins in use (silkscreen labels P0-P7, P10-P17) ----------
static const uint8_t SWITCH_PINS[] = {0, 1, 2, 3, 4, 5, 7, 10, 11};
static const uint8_t NUM_SWITCHES = sizeof(SWITCH_PINS) / sizeof(SWITCH_PINS[0]);

// Silkscreen label -> bit index in the 16-bit word (P00-P07 = bits 0-7,
// P10-P17 = bits 8-15).
uint8_t pinToBitIndex(uint8_t pinLabel) {
  return pinLabel < 10 ? pinLabel : (pinLabel - 10 + 8);
}

// ---------- Joystick potentiometers (3 joysticks x X/Y) ----------
// Order: J0X, J0Y, J1X, J1Y, J2X, J2Y
static const uint8_t JOY_PINS[] = {1, 2, 3, 4, 5, 6};
static const uint8_t NUM_JOY_AXES = sizeof(JOY_PINS) / sizeof(JOY_PINS[0]);
static const int ADC_MAX = 4095; // 12-bit ADC

// Exponential moving average: smaller = smoother but slower to react.
//
// Alpha is tied to the sample period and must be rescaled whenever
// POLL_INTERVAL_MS changes, or the filtering silently changes with it. The
// time constant is tau = -dt / ln(1 - alpha); the original 20 ms / 0.4 pair
// gives tau = 39 ms, and 5 ms / 0.12 reproduces it. Leaving alpha at 0.4 while
// sampling 4x faster would cut tau to 10 ms and let ADC noise straight through
// — visible as stick creep just outside the deadzone.
static const float EMA_ALPHA = 0.12f;
static float joySmoothed[NUM_JOY_AXES];

// ---------- UART ----------
// 115200 could not carry this: a 35-byte line costs 3.0 ms on the wire there,
// so 200 Hz would sit at 60% utilisation and any hiccup would block
// Serial.print (and therefore the sample loop). At 921600 the same line costs
// 0.38 ms — under 8% loaded. The CH343 bridge on this DevKitC handles it.
//
// This MUST match `baudrate` in joy.launch.py. Flash one without the other and
// nothing parses.
static const uint32_t UART_BAUD = 921600;
static const uint32_t POLL_INTERVAL_MS = 5;

// ---------- Switch debounce ----------
// Same 4-consecutive-reads filter the button board uses, for the same reason: a
// mechanical toggle bounces for 1-10 ms, and at a 5 ms poll that lands several
// alternating samples inside one flick. Undebounced they reach Joy.buttons, and
// since switch 0 picks rover-vs-arm, every bounce is a mode handover — one flick
// spams the drive and servo nodes with stop/resume.
//
// Nothing downstream filters this, so it has to be done here. Note the poll rate
// is what exposed it: at the old 20 ms period a bounce fit between samples and
// the missing debounce never showed. Raising the rate again shortens the 20 ms
// window this buys, so keep the two in step.
//
// Unlike the button board, which sends only on a debounced change, a frame goes
// out here every poll because the axes need it. So the filter feeds
// `stableSwitches` and every frame carries the last stable word, rather than
// gating the send.
static const uint8_t DEBOUNCE_STABLE_READS = 4; // 4 x 5 ms = 20 ms stable
static uint16_t stableSwitches = 0;
static uint16_t candidateSwitches = 0;
static uint8_t switchMatchCount = 0;

void setPCF8575InputMode(uint8_t addr) {
  // Writing 1s puts every pin in input mode (weak pull-up, quasi-bidirectional).
  // Switches pull the pin to GND when pressed, so this just needs to avoid
  // ever writing a 0, which would fight a switch when it's open (HIGH).
  Wire.beginTransmission(addr);
  Wire.write(0xFF);
  Wire.write(0xFF);
  Wire.endTransmission();
}

bool readPCF8575(uint8_t addr, uint16_t &value) {
  uint8_t bytesRead = Wire.requestFrom((int)addr, 2);
  if (bytesRead != 2) return false;
  uint8_t low  = Wire.read();  // P00..P07
  uint8_t high = Wire.read();  // P10..P17
  value = (uint16_t)low | ((uint16_t)high << 8);
  return true;
}

void setup() {
  Serial.begin(UART_BAUD);

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(400000);

  setPCF8575InputMode(PCF_ADDR);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  for (uint8_t i = 0; i < NUM_JOY_AXES; i++) {
    joySmoothed[i] = analogRead(JOY_PINS[i]);
  }

  // Seed the debounce from a real read, so the first 20 ms of frames report the
  // panel's actual position instead of an all-zero placeholder. A spurious
  // "switch 0 off" at startup would otherwise read as a mode handover.
  uint16_t initial;
  if (readPCF8575(PCF_ADDR, initial)) {
    stableSwitches = candidateSwitches = initial;
    switchMatchCount = DEBOUNCE_STABLE_READS;
  }

  Serial.println("PCF8575 switch + joystick reader started @ 200 Hz");
}

void loop() {
  // millis() gating rather than delay(): delay() would add the ~0.8 ms of loop
  // work (6 ADC reads + the I2C transfer) on top of the interval, so the real
  // period would be 5.8 ms, not 5. This keeps the sample rate at the rate.
  static uint32_t lastPoll = 0;
  uint32_t now = millis();
  if (now - lastPoll < POLL_INTERVAL_MS) return;
  lastPoll = now;

  uint16_t raw;
  if (!readPCF8575(PCF_ADDR, raw)) {
    // Throttled: at 200 Hz an unresponsive expander would otherwise emit 200
    // warning lines a second and saturate the link that carries the axes.
    static uint32_t lastWarn = 0;
    if (now - lastWarn >= 1000) {
      lastWarn = now;
      Serial.println("WARN: PCF8575 (0x24) not responding");
    }
    return;
  }

  // A change has to survive DEBOUNCE_STABLE_READS identical polls before it is
  // believed. Anything shorter is bounce and never reaches the frame.
  if (raw == candidateSwitches) {
    if (switchMatchCount < DEBOUNCE_STABLE_READS) switchMatchCount++;
  } else {
    candidateSwitches = raw;
    switchMatchCount = 1;
  }
  if (switchMatchCount == DEBOUNCE_STABLE_READS) {
    stableSwitches = candidateSwitches;
  }

  // One line, assembled then written in a single call. Serial.print() blocks
  // once the TX buffer fills, which would stall sampling, so the frame is
  // dropped instead if the buffer cannot take it whole — a skipped frame at
  // 200 Hz is invisible, a stalled loop is not.
  char line[64];
  int len = 0;
  for (uint8_t i = 0; i < NUM_SWITCHES; i++) {
    bool pressed = (stableSwitches >> pinToBitIndex(SWITCH_PINS[i])) & 1;
    line[len++] = pressed ? '1' : '0';
  }
  for (uint8_t i = 0; i < NUM_JOY_AXES; i++) {
    joySmoothed[i] += EMA_ALPHA * (analogRead(JOY_PINS[i]) - joySmoothed[i]);
    int axisValue = map((int)joySmoothed[i], 0, ADC_MAX, 0, 1000);
    len += snprintf(line + len, sizeof(line) - len, "|%d", axisValue);
  }
  len += snprintf(line + len, sizeof(line) - len, "\r\n");

  if (Serial.availableForWrite() >= len) {
    Serial.write((const uint8_t *)line, len);
  }
}
