#include <Arduino.h>
#include <Wire.h>

// ---------- I2C bus pins (ESP32-S3-DevKitC-1 default I2C pins) ----------
static const int I2C_SDA_PIN = 8;
static const int I2C_SCL_PIN = 9;

// ---------- PCF8575 addresses ----------
// 7-bit address = 0b0100 (A2 A1 A0)  ->  0x20 | (A2<<2) | (A1<<1) | A0
static const uint8_t PCF_ADDR_A = 0x20; // A2=0 A1=0 A0=0  ("000")
static const uint8_t PCF_ADDR_B = 0x22; // A2=0 A1=1 A0=0  ("010", read as A0,A1,A2)

// ---------- Ignored pins ----------
// Pins not actually wired to a button. Excluded entirely from the output
// string (not just zeroed) and never counted for debounce.
// bit0=P00 .. bit7=P07, bit8=P10 .. bit15=P17
static const uint16_t IGNORE_MASK_A = 0xE303; // P00,P01,P10,P11,P15,P16,P17
static const uint16_t IGNORE_MASK_B = 0x6000; // P15,P16
static const uint32_t IGNORE_MASK = (uint32_t)IGNORE_MASK_A | ((uint32_t)IGNORE_MASK_B << 16);

// ---------- UART ----------
// DevKitC-1's "UART" USB-C port is wired to the onboard CP2102 bridge,
// which sits on UART0 (GPIO43/44). Serial already maps there by default,
// so the button bitmask and debug logs both go out over that same cable.
static const uint32_t UART_BAUD = 115200;

// ---------- Debounce ----------
static const uint8_t DEBOUNCE_STABLE_READS = 4; // consecutive matching polls needed
static const uint32_t POLL_INTERVAL_MS = 5;

uint32_t stableMask = 0;     // last debounced "pressed = 1" mask actually sent
uint32_t candidateMask = 0;
uint8_t matchCount = 0;

bool readPCF8575(uint8_t addr, uint16_t &value) {
  // PCF8575 is quasi-bidirectional: a plain I2C read returns the current
  // pin states, no register pointer needed. Pins must be latched HIGH
  // (input mode) beforehand, see setPCF8575InputMode().
  uint8_t bytesRead = Wire.requestFrom((int)addr, 2);
  if (bytesRead != 2) return false;
  uint8_t low  = Wire.read();  // P00..P07
  uint8_t high = Wire.read();  // P10..P17
  value = (uint16_t)low | ((uint16_t)high << 8);
  return true;
}

void setPCF8575InputMode(uint8_t addr) {
  // Writing 1s puts every pin in input mode with a weak internal pull-up
  // (quasi-bidirectional I/O). Do this once at boot.
  Wire.beginTransmission(addr);
  Wire.write(0xFF);
  Wire.write(0xFF);
  Wire.endTransmission();
}

void sendMask(uint32_t mask) {
  // String of '0'/'1', one char per non-ignored pin, 0 = inactive, 1 = active.
  // Left-to-right: expander A P00..P17, then expander B P00..P17, skipping
  // any pin set in IGNORE_MASK.
  char buf[34];
  int len = 0;
  for (int i = 0; i < 32; i++) {
    if (IGNORE_MASK & (1UL << i)) continue;
    buf[len++] = (mask & (1UL << i)) ? '1' : '0';
  }
  buf[len++] = '\n';
  buf[len] = '\0';
  Serial.print(buf);
}

void setup() {
  Serial.begin(UART_BAUD); // UART0, out the "UART" USB-C port

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(400000);

  setPCF8575InputMode(PCF_ADDR_A);
  setPCF8575InputMode(PCF_ADDR_B);

  Serial.println("PCF8575 button reader started");
}

void loop() {
  static uint32_t lastPoll = 0;
  uint32_t now = millis();
  if (now - lastPoll < POLL_INTERVAL_MS) return;
  lastPoll = now;

  uint16_t rawA = 0xFFFF, rawB = 0xFFFF;
  bool okA = readPCF8575(PCF_ADDR_A, rawA);
  bool okB = readPCF8575(PCF_ADDR_B, rawB);

  if (!okA) Serial.println("WARN: expander A (0x20) not responding");
  if (!okB) Serial.println("WARN: expander B (0x22) not responding");

  // Expander B buttons pull the pin LOW when pressed -> invert so 1 = pressed.
  // Expander A's buttons are wired the other way (pin reads HIGH when pressed),
  // so it's used as-is, no invert.
  uint16_t pressedA = rawA & (uint16_t)~IGNORE_MASK_A;
  uint16_t pressedB = (uint16_t)(~rawB) & (uint16_t)~IGNORE_MASK_B;

  // bits 0-15 = expander A (0x20), bits 16-31 = expander B (0x22)
  uint32_t rawMask = (uint32_t)pressedA | ((uint32_t)pressedB << 16);

  if (rawMask == candidateMask) {
    if (matchCount < DEBOUNCE_STABLE_READS) matchCount++;
  } else {
    candidateMask = rawMask;
    matchCount = 1;
  }

  if (matchCount == DEBOUNCE_STABLE_READS && candidateMask != stableMask) {
    stableMask = candidateMask;
    sendMask(stableMask);
  }
}
