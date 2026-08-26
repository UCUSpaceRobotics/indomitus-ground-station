// Wire format for the mast <-> rover LoRa link.
//
//   AA 55 | type | seq | payload[4] | crc16[2]        = 10 bytes
//
// Ten bytes fits inside the E32's 58-byte single-packet limit, so one frame is
// always one air packet - write it with a single write() call and the module
// will not split it. Everything here is plain C++ with no Arduino dependency,
// so the same file compiles on a host for cross-checking against
// mast/lora_frame.py, which must stay byte-for-byte identical.
//
// The link is half-duplex on one channel: if both ends transmit at once the
// frames collide and both are lost. The mast polls, the rover only ever answers
// - see README.md.

#pragma once

#include <stddef.h>
#include <stdint.h>

static const uint8_t LINK_SYNC0 = 0xAA;
static const uint8_t LINK_SYNC1 = 0x55;

static const size_t LINK_PAYLOAD_LEN = 4;
static const size_t LINK_FRAME_LEN = 10;

// Frame types. Direction is fixed: TELEOP is mast->rover, STATUS is rover->mast.
static const uint8_t LINK_TYPE_TELEOP = 0x01;
static const uint8_t LINK_TYPE_STATUS = 0x02;

// TeleopPayload.flags
static const uint8_t LINK_FLAG_ESTOP = 0x01;
static const uint8_t LINK_FLAG_MODE = 0x02;

// StatusPayload.flags
static const uint8_t LINK_STATUS_FAILSAFE = 0x01;
static const uint8_t LINK_STATUS_ESTOP = 0x02;

struct LinkFrame {
    uint8_t type;
    uint8_t seq;
    uint8_t payload[LINK_PAYLOAD_LEN];
};

// Velocities are percentages of the rover's configured maximum, -100..100.
// A byte per axis is plenty: the joystick resolution that survives a 3 Hz link
// is nowhere near 8 bits, and it keeps the frame at one air packet.
struct TeleopPayload {
    int8_t vx;
    int8_t vy;
    int8_t wz;
    uint8_t flags;
};

// rx_ok / rx_bad are the low bytes of free-running counters. The mast diffs
// them modulo 256, which is unambiguous as long as fewer than 256 frames pass
// between polls - four seconds of headroom at 60 Hz, and the link runs at 5.
struct StatusPayload {
    uint8_t echo_seq;
    uint8_t rx_ok;
    uint8_t rx_bad;
    uint8_t flags;
};

// CRC-16/CCITT-FALSE: init 0xFFFF, poly 0x1021, no reflection, no final xor.
uint16_t link_crc16(const uint8_t *data, size_t len);

// Serialises into `out` (must hold LINK_FRAME_LEN bytes). Returns bytes written.
size_t link_encode(const LinkFrame &frame, uint8_t *out);

void link_pack_teleop(const TeleopPayload &in, uint8_t *payload);
void link_unpack_teleop(const uint8_t *payload, TeleopPayload &out);
void link_pack_status(const StatusPayload &in, uint8_t *payload);
void link_unpack_status(const uint8_t *payload, StatusPayload &out);

// Byte-at-a-time parser that resynchronises after corruption. Feed it
// everything the UART hands over; push() returns true on each complete frame
// whose CRC checks out.
class LinkParser {
public:
    bool push(uint8_t byte, LinkFrame &out);

    uint32_t ok = 0;   // frames accepted
    uint32_t bad = 0;  // frames reaching full length with a bad CRC

private:
    enum State : uint8_t { WAIT_SYNC0, WAIT_SYNC1, BODY };

    State state_ = WAIT_SYNC0;
    uint8_t body_[LINK_FRAME_LEN - 2];
    uint8_t idx_ = 0;
};
