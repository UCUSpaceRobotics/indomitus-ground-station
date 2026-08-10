#include <Arduino.h>
#include "LoRa_E32.h"

// --- Wiring (see README.md for the full table) ---
#define E32_RX_PIN 26   // ESP32 pin <- E32 TXD
#define E32_TX_PIN 27   // ESP32 pin -> E32 RXD
#define E32_AUX_PIN 32
#define E32_M0_PIN 25
#define E32_M1_PIN 33

#ifndef NODE_NAME
#define NODE_NAME "NODE"
#endif

LoRa_E32 e32ttl100(&Serial2, E32_AUX_PIN, E32_M0_PIN, E32_M1_PIN, UART_BPS_RATE_9600);

String inputLine;

void setup() {
  Serial.begin(115200);
  delay(300);

  Serial2.begin(9600, SERIAL_8N1, E32_RX_PIN, E32_TX_PIN);
  e32ttl100.begin();

  Serial.println();
  Serial.print("[");
  Serial.print(NODE_NAME);
  Serial.println("] E32 LoRa chat ready. Type a message and press Enter to send it to the other node.");
}

void loop() {
  // Anything typed into the Serial monitor gets sent out over LoRa on Enter.
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      inputLine.trim();
      if (inputLine.length() > 0) {
        ResponseStatus rs = e32ttl100.sendMessage(inputLine);
        Serial.print("[");
        Serial.print(NODE_NAME);
        Serial.print(" -> sent] ");
        Serial.print(inputLine);
        Serial.print("  (");
        Serial.print(rs.getResponseDescription());
        Serial.println(")");
      }
      inputLine = "";
    } else if (c != '\r') {
      inputLine += c;
    }
  }

  // Anything arriving over LoRa gets printed to the Serial monitor.
  if (e32ttl100.available() > 0) {
    ResponseContainer rc = e32ttl100.receiveMessage();
    Serial.print("[");
    Serial.print(NODE_NAME);
    Serial.print(" <- recv] ");
    Serial.println(rc.data);
  }
}
