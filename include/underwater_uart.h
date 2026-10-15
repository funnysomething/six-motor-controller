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

// One UART and one pending measurement per sensor. No blocking read or delay.
class UnderwaterUartSensor {
 public:
  uint8_t state = 0; // Waiting, valid, no echo/range, or UART timeout.
  uint16_t distance = 0;

  template <typename Port>
  bool poll(Port &port, uint32_t now) {
    bool updated = false;
    if (waiting && now - requestedAt >= 60) {
      waiting = false;
      decoder.reset();
      state = 3;
      distance = 0;
      updated = true;
    }
    for (uint8_t budget = 64; budget && port.available(); --budget) {
      const uint8_t value = port.read();
      uint16_t mm;
      if (waiting && decoder.feed(value, mm)) {
        waiting = false;
        const bool valid = UnderwaterUartDecoder::inRange(mm);
        state = valid ? 1 : 2;
        distance = valid ? mm : 0;
        updated = true;
      }
    }
    return updated;
  }

  template <typename Port>
  void request(Port &port, uint32_t now) {
    // Do not accept queued/late bytes as a reply to a fresh request.
    if (waiting || port.available()) return;
    decoder.reset();
    port.write(static_cast<uint8_t>(0x55));
    requestedAt = now;
    waiting = true;
  }

 private:
  UnderwaterUartDecoder decoder;
  uint32_t requestedAt = 0;
  bool waiting = false;
};
