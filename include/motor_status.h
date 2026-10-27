#pragma once
#include <stdint.h>

// One six-byte status-v2 range block. Compatible with a 32-byte slave TX buffer
// when appended three times to the existing 14-byte motor prefix.
inline void writeRangeStatusV2(uint8_t *block, uint8_t sensor, uint8_t state,
                              uint8_t sequence, uint16_t mm, bool seen,
                              uint32_t measuredAt, uint32_t now) {
  block[0] = 0xD1 + sensor;
  block[1] = ((sequence & 63) << 2) | (state & 3);
  block[2] = mm & 0xFF;
  block[3] = mm >> 8;
  const uint32_t age = (now - measuredAt) / 10;
  block[4] = !seen || age >= 254 ? 255 : age + 1;
  block[5] = block[0] ^ block[1] ^ block[2] ^ block[3] ^ block[4];
}
