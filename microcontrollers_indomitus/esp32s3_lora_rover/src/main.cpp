// Rover-side endpoint of the mast <-> rover LoRa link, standing in for the
// Jetson until the real rover side exists.
//
// The mast (mast/lora_bridge.py) polls; this node only ever answers. It parses
// teleop frames, holds the latest command, replies with a status frame, and
// zeroes the command if the polls stop. Nothing here drives a motor - the
// command is printed to the USB console, which is what "the rover would have
// moved" looks like on a bench.
//
// Wiring, module configuration and bring-up steps: see README.md.

#include <Arduino.h>
#include <string.h>

#include "link_frame.h"

// --- wiring ---------------------------------------------------------------
// GPIO17/18 are the S3's default Serial1 pins, and none of these five collide
// with the panel firmwares in this repo (1-6 ADC, 8/9 I2C, 43/44 USB UART) or
// with the strapping (0, 3, 45, 46), USB (19, 20) or flash/PSRAM (26-37) pins.
static const int E32_M0_PIN = 15;
static const int E32_M1_PIN = 16;
static const int E32_TX_PIN = 17;   // S3 TX1 -> E32 RXD
static const int E32_RX_PIN = 18;   // S3 RX1 <- E32 TXD
static const int E32_AUX_PIN = 21;

static const uint32_t CONSOLE_BAUD = 115200;
// Must match the UART baud encoded in CFG_SPED below. The module's config
// commands always run at 9600 8N1 regardless, which cfgTransact() handles.
static const uint32_t E32_UART_BAUD = 9600;
static const uint32_t E32_CONFIG_BAUD = 9600;

// --- module configuration -------------------------------------------------
// Written by the CFG! console command; see README.md for the bit breakdown.
// Factory default is C0 00 00 1A 17 44.
static const uint8_t CFG_HEAD_SAVE = 0xC0;      // C2 = same, but not kept over power-down
static const uint8_t CFG_ADDH = 0x00;
static const uint8_t CFG_ADDL = 0x00;
static const uint8_t CFG_SPED = 0x1B;           // 8N1, UART 9600, air 4.8 kbps
static const uint8_t CFG_CHAN = 0x17;           // 410 + 23 = 433 MHz
static const uint8_t CFG_OPTION = 0x44;         // transparent, push-pull, FEC on, 30 dBm
static const uint8_t CFG_OPTION_LOW = 0x47;     // as above but 21 dBm (CFG!LOW)

static const uint8_t E32_MODE_NORMAL = 0;
static const uint8_t E32_MODE_SLEEP = 3;

// --- timing ---------------------------------------------------------------
// Two to three missed polls at the mast's 5 Hz. Long enough to ride out a
// single dropped frame, short enough that a dead link stops the rover before
// it travels anywhere.
static const uint32_t FAILSAFE_TIMEOUT_MS = 500;
static const uint32_t STATUS_PRINT_MS = 1000;
static const uint32_t AUX_TIMEOUT_MS = 1000;

// --- state ----------------------------------------------------------------
static LinkParser parser;
static TeleopPayload command = {0, 0, 0, 0};
static uint32_t last_frame_ms = 0;
static uint32_t last_print_ms = 0;
static uint32_t tx_frames = 0;
static uint32_t tx_drops = 0;
static uint8_t tx_seq = 0;
// Boot into failsafe: a rover that has never heard from the mast is in exactly
// the state a rover that stopped hearing from it is in.
static bool failsafe = true;

static char console_line[32];
static uint8_t console_len = 0;

// --- E32 control ----------------------------------------------------------

// AUX low means the module is busy (self-check, unsent TX, or draining RX to
// the UART). Datasheet 6.1: a mode change only takes effect once AUX has been
// high for 2 ms, so anything that switches modes waits here first.
static bool waitAux(uint32_t timeout_ms)
{
    const uint32_t start = millis();
    while (digitalRead(E32_AUX_PIN) == LOW) {
        if (millis() - start > timeout_ms) {
            return false;
        }
        delay(1);
    }
    delay(3);
    return true;
}

static bool setMode(uint8_t mode)
{
    const bool settled = waitAux(AUX_TIMEOUT_MS);
    digitalWrite(E32_M0_PIN, mode & 0x01);
    digitalWrite(E32_M1_PIN, (mode >> 1) & 0x01);
    delay(5);
    return waitAux(AUX_TIMEOUT_MS) && settled;
}

// Runs one sleep-mode command transaction and returns to normal mode. Reply
// length is fixed by the command (6 bytes for C0/C1), 0 to just write.
static bool cfgTransact(const uint8_t *cmd, size_t cmd_len,
                        uint8_t *reply, size_t reply_len)
{
    if (!setMode(E32_MODE_SLEEP)) {
        return false;
    }
    // Config is 9600 8N1 whatever the module's configured UART baud is.
    Serial1.begin(E32_CONFIG_BAUD, SERIAL_8N1, E32_RX_PIN, E32_TX_PIN);
    while (Serial1.available()) {
        Serial1.read();
    }

    Serial1.write(cmd, cmd_len);
    Serial1.flush();

    size_t got = 0;
    const uint32_t start = millis();
    while (got < reply_len && millis() - start < 1000) {
        if (Serial1.available()) {
            reply[got++] = (uint8_t)Serial1.read();
        }
    }

    Serial1.begin(E32_UART_BAUD, SERIAL_8N1, E32_RX_PIN, E32_TX_PIN);
    setMode(E32_MODE_NORMAL);
    return got == reply_len;
}

static void printHex(const char *label, const uint8_t *bytes, size_t len)
{
    Serial.print(label);
    for (size_t i = 0; i < len; i++) {
        if (bytes[i] < 0x10) {
            Serial.print('0');
        }
        Serial.print(bytes[i], HEX);
        if (i + 1 < len) {
            Serial.print(' ');
        }
    }
    Serial.println();
}

static void cfgRead()
{
    const uint8_t cmd[3] = {0xC1, 0xC1, 0xC1};
    uint8_t reply[6] = {0};
    if (!cfgTransact(cmd, sizeof(cmd), reply, sizeof(reply))) {
        Serial.println("CFG? failed - no reply. Check M0/M1/AUX wiring and the module's supply.");
        return;
    }
    printHex("CFG? ", reply, sizeof(reply));
}

static void cfgWrite(uint8_t option)
{
    const uint8_t cmd[6] = {CFG_HEAD_SAVE, CFG_ADDH, CFG_ADDL, CFG_SPED, CFG_CHAN, option};
    uint8_t echo[6] = {0};
    printHex("CFG! writing ", cmd, sizeof(cmd));

    // Some firmware revisions echo the saved parameters and some stay quiet, so
    // the echo is not the acceptance test - the read-back below is.
    cfgTransact(cmd, sizeof(cmd), echo, sizeof(echo));
    delay(100);
    cfgRead();
}

// --- link -----------------------------------------------------------------

static void sendStatus(uint8_t echo_seq)
{
    StatusPayload status;
    status.echo_seq = echo_seq;
    status.rx_ok = (uint8_t)parser.ok;
    status.rx_bad = (uint8_t)parser.bad;
    status.flags = (uint8_t)((failsafe ? LINK_STATUS_FAILSAFE : 0) |
                             ((command.flags & LINK_FLAG_ESTOP) ? LINK_STATUS_ESTOP : 0));

    LinkFrame frame;
    frame.type = LINK_TYPE_STATUS;
    frame.seq = tx_seq++;
    link_pack_status(status, frame.payload);

    uint8_t buf[LINK_FRAME_LEN];
    link_encode(frame, buf);

    if (!waitAux(AUX_TIMEOUT_MS)) {
        tx_drops++;
        return;
    }
    Serial1.write(buf, LINK_FRAME_LEN);
    tx_frames++;
}

static void pollRadio()
{
    while (Serial1.available()) {
        LinkFrame frame;
        if (!parser.push((uint8_t)Serial1.read(), frame)) {
            continue;
        }
        if (frame.type != LINK_TYPE_TELEOP) {
            continue;  // our own reply looped back, or a frame meant for someone else
        }

        link_unpack_teleop(frame.payload, command);
        if (command.flags & LINK_FLAG_ESTOP) {
            command.vx = command.vy = command.wz = 0;
        }
        last_frame_ms = millis();
        failsafe = false;

        // Reply immediately, inside the read loop: the mast will not transmit
        // again until it has our answer or timed out, which is what keeps this
        // half-duplex channel collision-free.
        sendStatus(frame.seq);
    }
}

static void applyFailsafe()
{
    if (failsafe || millis() - last_frame_ms <= FAILSAFE_TIMEOUT_MS) {
        return;
    }
    command.vx = command.vy = command.wz = 0;
    failsafe = true;
    Serial.println("FAILSAFE: no valid teleop frame for 500 ms - command zeroed");
}

// --- console --------------------------------------------------------------

static void handleCommand(const char *line)
{
    if (strcmp(line, "CFG?") == 0) {
        cfgRead();
    } else if (strcmp(line, "CFG!") == 0) {
        cfgWrite(CFG_OPTION);
    } else if (strcmp(line, "CFG!LOW") == 0) {
        cfgWrite(CFG_OPTION_LOW);
    } else if (strcmp(line, "TEST") == 0 || strcmp(line, "TESTBAD") == 0) {
        // Loopback aid: with GPIO17 jumpered to GPIO18 and the E32 unplugged,
        // this frame comes straight back into pollRadio(), exercising the
        // parser with no radio in the picture. TESTBAD corrupts the CRC, which
        // must land in parser.bad and never in parser.ok.
        TeleopPayload probe = {10, -20, 30, 0};
        LinkFrame frame;
        frame.type = LINK_TYPE_TELEOP;
        frame.seq = tx_seq++;
        link_pack_teleop(probe, frame.payload);

        uint8_t buf[LINK_FRAME_LEN];
        link_encode(frame, buf);
        if (line[4] == 'B') {
            buf[LINK_FRAME_LEN - 1] ^= 0xFF;
        }
        printHex("TEST tx ", buf, sizeof(buf));
        Serial1.write(buf, sizeof(buf));

        // With a single radio on the bench nothing answers, so AUX is the only
        // evidence the module took the frame: it drops low while busy and rises
        // once the packet is in the RF chip.
        const uint32_t started = millis();
        uint32_t went_low = 0, came_back = 0;
        while (millis() - started < 500) {
            const int aux = digitalRead(E32_AUX_PIN);
            if (went_low == 0) {
                if (aux == LOW) {
                    went_low = millis();
                }
            } else if (aux == HIGH) {
                came_back = millis();
                break;
            }
            delayMicroseconds(200);
        }
        if (went_low == 0) {
            Serial.println("TEST: AUX never went low - module did not accept the frame");
        } else if (came_back == 0) {
            Serial.println("TEST: AUX stuck low - module still busy, or browning out");
        } else {
            Serial.printf("TEST: AUX low for %lu ms - frame transmitted\n",
                          (unsigned long)(came_back - went_low));
        }
    } else if (strcmp(line, "STAT") == 0) {
        Serial.printf("rx_ok=%lu rx_bad=%lu tx=%lu tx_drops=%lu failsafe=%d\n",
                      (unsigned long)parser.ok, (unsigned long)parser.bad,
                      (unsigned long)tx_frames, (unsigned long)tx_drops, failsafe);
    } else if (line[0] != '\0') {
        Serial.println("commands: CFG? CFG! CFG!LOW TEST TESTBAD STAT");
    }
}

static void pollConsole()
{
    while (Serial.available()) {
        const char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (console_len > 0) {
                console_line[console_len] = '\0';
                handleCommand(console_line);
                console_len = 0;
            }
        } else if (console_len < sizeof(console_line) - 1) {
            console_line[console_len++] = c;
        }
    }
}

static void printStatus()
{
    if (millis() - last_print_ms < STATUS_PRINT_MS) {
        return;
    }
    last_print_ms = millis();
    Serial.printf("vx=%4d vy=%4d wz=%4d flags=0x%02X | rx_ok=%lu rx_bad=%lu tx=%lu | %s (%lu ms since last frame)\n",
                  command.vx, command.vy, command.wz, command.flags,
                  (unsigned long)parser.ok, (unsigned long)parser.bad,
                  (unsigned long)tx_frames,
                  failsafe ? "FAILSAFE" : "LINKED",
                  (unsigned long)(millis() - last_frame_ms));
}

// --- entry points ---------------------------------------------------------

void setup()
{
    Serial.begin(CONSOLE_BAUD);
    delay(300);

    pinMode(E32_M0_PIN, OUTPUT);
    pinMode(E32_M1_PIN, OUTPUT);
    // Pulled up so a disconnected module reads "not busy" instead of floating,
    // which is what makes the TEST loopback usable with no E32 attached.
    pinMode(E32_AUX_PIN, INPUT_PULLUP);

    Serial1.begin(E32_UART_BAUD, SERIAL_8N1, E32_RX_PIN, E32_TX_PIN);
    setMode(E32_MODE_NORMAL);

    Serial.println();
    Serial.println("[ROVER] E32 LoRa endpoint ready (mode 0, 9600 8N1).");
    Serial.println("[ROVER] commands: CFG? CFG! CFG!LOW TEST TESTBAD STAT");
}

void loop()
{
    pollConsole();
    pollRadio();
    applyFailsafe();
    printStatus();
}
