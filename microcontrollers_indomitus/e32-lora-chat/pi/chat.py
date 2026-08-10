#!/usr/bin/env python3
"""E32 LoRa serial chat for the Raspberry Pi 5 node.

Counterpart to the PC's PlatformIO firmware (../src/main.cpp). Type a line,
press Enter, it goes out over the E32. Anything received gets printed.

Wiring (BCM numbering) - see README.md for the full table:
  E32 TXD -> Pi GPIO15 (RXD)
  E32 RXD -> Pi GPIO14 (TXD)
  E32 AUX -> Pi GPIO17
  E32 M0  -> Pi GPIO23
  E32 M1  -> Pi GPIO24
  E32 VCC -> separate 3.3V supply (NOT the Pi's 3V3 pin)
  E32 GND -> common ground
"""

import threading

import serial
from lora_e32 import LoRaE32

NODE_NAME = "PI"

AUX_PIN = 17
M0_PIN = 23
M1_PIN = 24
SERIAL_PORT = "/dev/ttyAMA0"  # Ubuntu doesn't create the /dev/serial0 alias Raspberry Pi OS does


def sender(lora: LoRaE32) -> None:
    while True:
        try:
            line = input().strip()
        except EOFError:
            break
        if not line:
            continue
        code = lora.send_transparent_message(line)
        print(f"[{NODE_NAME} -> sent] {line}  ({code})")


def main() -> None:
    lora_serial = serial.Serial(SERIAL_PORT, baudrate=9600, timeout=1)
    lora = LoRaE32("433T30D", lora_serial, aux_pin=AUX_PIN, m0_pin=M0_PIN, m1_pin=M1_PIN)
    code = lora.begin()
    print(f"[{NODE_NAME}] begin() -> {code}")

    threading.Thread(target=sender, args=(lora,), daemon=True).start()

    print(f"[{NODE_NAME}] E32 LoRa chat ready. Type a message and press Enter to send it to the other node.")
    while True:
        if lora.available() > 0:
            try:
                code, value = lora.receive_message()
                print(f"[{NODE_NAME} <- recv] {value}")
            except UnicodeDecodeError:
                print(f"[{NODE_NAME} <- recv] <garbled packet, dropped>")


if __name__ == "__main__":
    main()
