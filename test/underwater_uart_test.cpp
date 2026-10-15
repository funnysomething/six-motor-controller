#include <assert.h>
#include "underwater_uart.h"

int main() {
  UnderwaterUartDecoder decoder;
  uint16_t mm = 9999;
  // Manufacturer's example: FF 07 A1 A7 = 1953 mm, sum wraps at 255.
  assert(!decoder.feed(0xFF, mm));
  assert(!decoder.feed(0x07, mm));
  assert(!decoder.feed(0xA1, mm));
  assert(mm == 9999); // A partial frame cannot publish a distance.
  assert(decoder.feed(0xA7, mm));
  assert(mm == 1953);

  // Back-to-back packets, including FF in the payload and zero/no echo.
  assert(!decoder.feed(0xFF, mm));
  assert(!decoder.feed(0x00, mm));
  assert(!decoder.feed(0xFF, mm));
  assert(decoder.feed(0xFE, mm));
  assert(mm == 255);
  assert(!decoder.feed(0xFF, mm));
  assert(!decoder.feed(0x00, mm));
  assert(!decoder.feed(0x00, mm));
  assert(decoder.feed(0xFF, mm));
  assert(mm == 0);
  assert(!UnderwaterUartDecoder::inRange(mm));

  // Recover a valid frame after noise and a bad checksum.
  const uint8_t noisy[] = {0x12, 0x34, 0xFF, 0x07, 0xA1, 0xA6,
                           0xFF, 0x07, 0xA1, 0xA7};
  for (unsigned i = 0; i < sizeof(noisy); ++i) {
    assert(decoder.feed(noisy[i], mm) == (i == sizeof(noisy) - 1));
  }
  assert(mm == 1953);

  // Recover when an incomplete frame is followed immediately by a new one.
  decoder.reset();
  const uint8_t truncated[] = {0xFF, 0x07, 0xFF, 0x07, 0xA1, 0xA7};
  for (unsigned i = 0; i < sizeof(truncated); ++i) {
    assert(decoder.feed(truncated[i], mm) == (i == sizeof(truncated) - 1));
  }
  assert(mm == 1953);

  // A timeout resets the partial packet before the next request.
  decoder.feed(0xFF, mm);
  decoder.feed(0x07, mm);
  decoder.reset();
  assert(!decoder.feed(0xFF, mm));
  assert(!decoder.feed(0x0B, mm));
  assert(!decoder.feed(0xB8, mm));
  assert(decoder.feed(0xC2, mm));
  assert(mm == 3000);
  assert(UnderwaterUartDecoder::inRange(20));
  assert(UnderwaterUartDecoder::inRange(3000));
  assert(!UnderwaterUartDecoder::inRange(19));
  assert(!UnderwaterUartDecoder::inRange(3001));
}
