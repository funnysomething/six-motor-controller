#pragma once

#include <stdint.h>

// DFRobot SEN0598: 115200 8N1, request 0x55, reply FF/high/low/sum.
// Distances arrive in millimeters. No speed-of-sound conversion is needed.
class UnderwaterUartDecoder {
 public:
  void reset() { count = 0; }

  bool feed(uint8_t value, uint16_t &distance) {
    bytes[count++] = value;
    if (count < 4) return false;
    if (bytes[0] == 0xFF &&
        bytes[3] == static_cast<uint8_t>(bytes[0] + bytes[1] + bytes[2])) {
      distance = (static_cast<uint16_t>(bytes[1]) << 8) | bytes[2];
      reset();
      return true;
    }
    // Slide one byte so noise, partial frames and corrupt checksums do not
    // lose the next header. 0xFF is also allowed inside a distance payload.
    for (uint8_t i = 0; i < 3; ++i) bytes[i] = bytes[i + 1];
    count = 3;
    return false;
  }

  static bool inRange(uint16_t distance) {
    return distance >= 20 && distance <= 3000;
  }

 private:
  uint8_t bytes[4] = {};
  uint8_t count = 0;
};
