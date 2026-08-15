#include "link_frame.h"

#include <string.h>

uint16_t link_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
                                 : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

size_t link_encode(const LinkFrame &frame, uint8_t *out)
{
    out[0] = LINK_SYNC0;
    out[1] = LINK_SYNC1;
    out[2] = frame.type;
    out[3] = frame.seq;
    memcpy(out + 4, frame.payload, LINK_PAYLOAD_LEN);

    // The sync word is excluded: it is a framing marker, not data, and a
    // receiver that resynchronised mid-stream has not seen it.
    const uint16_t crc = link_crc16(out + 2, 2 + LINK_PAYLOAD_LEN);
    out[8] = (uint8_t)(crc & 0xFF);
    out[9] = (uint8_t)(crc >> 8);
    return LINK_FRAME_LEN;
}

void link_pack_teleop(const TeleopPayload &in, uint8_t *payload)
{
    payload[0] = (uint8_t)in.vx;
    payload[1] = (uint8_t)in.vy;
    payload[2] = (uint8_t)in.wz;
    payload[3] = in.flags;
}

void link_unpack_teleop(const uint8_t *payload, TeleopPayload &out)
{
    out.vx = (int8_t)payload[0];
    out.vy = (int8_t)payload[1];
    out.wz = (int8_t)payload[2];
    out.flags = payload[3];
}

void link_pack_status(const StatusPayload &in, uint8_t *payload)
{
    payload[0] = in.echo_seq;
    payload[1] = in.rx_ok;
    payload[2] = in.rx_bad;
    payload[3] = in.flags;
}

void link_unpack_status(const uint8_t *payload, StatusPayload &out)
{
    out.echo_seq = payload[0];
    out.rx_ok = payload[1];
    out.rx_bad = payload[2];
    out.flags = payload[3];
}

bool LinkParser::push(uint8_t byte, LinkFrame &out)
{
    switch (state_) {
    case WAIT_SYNC0:
        if (byte == LINK_SYNC0) {
            state_ = WAIT_SYNC1;
        }
        return false;

    case WAIT_SYNC1:
        if (byte == LINK_SYNC1) {
            state_ = BODY;
            idx_ = 0;
        } else if (byte != LINK_SYNC0) {
            // AA AA 55 is a legal start, so a repeated AA keeps us here rather
            // than throwing away the sync we already have.
            state_ = WAIT_SYNC0;
        }
        return false;

    case BODY:
        body_[idx_++] = byte;
        if (idx_ < sizeof(body_)) {
            return false;
        }
        state_ = WAIT_SYNC0;

        {
            const uint16_t want = (uint16_t)body_[6] | ((uint16_t)body_[7] << 8);
            if (link_crc16(body_, 2 + LINK_PAYLOAD_LEN) != want) {
                bad++;
                return false;
            }
            out.type = body_[0];
            out.seq = body_[1];
            memcpy(out.payload, body_ + 2, LINK_PAYLOAD_LEN);
            ok++;
            return true;
        }
    }
    return false;
}
