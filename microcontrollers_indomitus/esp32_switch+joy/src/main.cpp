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
static const float EMA_ALPHA = 0.4f;
static float joySmoothed[NUM_JOY_AXES];

// ---------- UART ----------
static const uint32_t UART_BAUD = 115200;
static const uint32_t POLL_INTERVAL_MS = 20;

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

  Serial.println("PCF8575 switch test started");
}

void loop() {
  uint16_t raw;
  if (!readPCF8575(PCF_ADDR, raw)) {
    Serial.println("WARN: PCF8575 (0x24) not responding");
    delay(POLL_INTERVAL_MS);
    return;
  }

  char mask[NUM_SWITCHES + 1];
  for (uint8_t i = 0; i < NUM_SWITCHES; i++) {
    bool pressed = (raw >> pinToBitIndex(SWITCH_PINS[i])) & 1;
    mask[i] = pressed ? '1' : '0';
  }
  mask[NUM_SWITCHES] = '\0';

  Serial.print(mask);
  for (uint8_t i = 0; i < NUM_JOY_AXES; i++) {
    joySmoothed[i] += EMA_ALPHA * (analogRead(JOY_PINS[i]) - joySmoothed[i]);
    int axisValue = map((int)joySmoothed[i], 0, ADC_MAX, 0, 1000);
    Serial.print('|');
    Serial.print(axisValue);
  }
  Serial.println();

  delay(POLL_INTERVAL_MS);
}
