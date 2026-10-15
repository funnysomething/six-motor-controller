#include <assert.h>
#include <deque>
#include <vector>
#include "underwater_uart.h"

struct FakePort {
  std::deque<uint8_t> input;
  std::vector<uint8_t> output;
  unsigned reads = 0;
  bool available() { return !input.empty(); }
  uint8_t read() {
    const uint8_t value = input.front();
    input.pop_front();
    ++reads;
    return value;
  }
  void write(uint8_t value) { output.push_back(value); }
};

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

  // Each physical UART owns its decoder and timeout. A disconnected second
  // sensor must not prevent fresh readings from the first.
  UnderwaterUartSensor first, second;
  FakePort firstPort, secondPort;
  first.request(firstPort, 500);
  assert(firstPort.output == std::vector<uint8_t>({0x55}));
  firstPort.input = {0xFF, 0x07};
  assert(!first.poll(firstPort, 510));
  assert(first.state == 0);
  firstPort.input = {0xA1, 0xA7};
  assert(first.poll(firstPort, 514));
  assert(first.state == 1 && first.distance == 1953);
  second.request(secondPort, 600);
  assert(!second.poll(secondPort, 659));
  assert(second.poll(secondPort, 660));
  assert(second.state == 3 && second.distance == 0);
  assert(first.state == 1 && first.distance == 1953);

  // Late replies are ignored; a later request can recover a timed-out sensor.
  secondPort.input = {0xFF, 0x07, 0xA1, 0xA7};
  assert(!second.poll(secondPort, 670));
  assert(second.state == 3);
  second.request(secondPort, 800);
  secondPort.input = {0xFF, 0x0B, 0xB8, 0xC2};
  assert(second.poll(secondPort, 814));
  assert(second.state == 1 && second.distance == 3000);
  assert(first.distance == 1953);

  // A zero reply means no underwater echo, not missing UART communication.
  first.request(firstPort, 900);
  firstPort.input = {0xFF, 0x00, 0x00, 0xFF};
  assert(first.poll(firstPort, 914));
  assert(first.state == 2 && first.distance == 0);
  // Invalid checksum times out and clears the previous measured value.
  second.request(secondPort, 1000);
  secondPort.input = {0xFF, 0x07, 0xA1, 0xA6};
  assert(!second.poll(secondPort, 1014));
  assert(second.poll(secondPort, 1060));
  assert(second.state == 3 && second.distance == 0);

  // Continuous noise cannot starve motor watchdog work.
  firstPort.input.assign(1000, 0x12);
  const unsigned reads = firstPort.reads;
  assert(!first.poll(firstPort, 1100));
  assert(firstPort.reads - reads == 64);
  const auto writes = firstPort.output.size();
  first.request(firstPort, 1100);
  assert(firstPort.output.size() == writes);

  // Timeout subtraction still works across the millis() wraparound.
  UnderwaterUartSensor wrap;
  FakePort wrapPort;
  wrap.request(wrapPort, 0xFFFFFFF0U);
  assert(!wrap.poll(wrapPort, 0x20U));
  assert(wrap.poll(wrapPort, 0x2CU));
  assert(wrap.state == 3);

  // The third UART has its own partial packet and timeout state as well.
  UnderwaterUartSensor third;
  FakePort thirdPort;
  third.request(thirdPort, 1200);
  thirdPort.input = {0xFF, 0x04};
  assert(!third.poll(thirdPort, 1210));
  assert(!second.poll(secondPort, 1210));
  thirdPort.input = {0xD2, 0xD5}; // 1234 mm.
  assert(third.poll(thirdPort, 1214));
  assert(third.state == 1 && third.distance == 1234);
  assert(second.state == 3 && second.distance == 0);
  third.request(thirdPort, 1500);
  assert(third.poll(thirdPort, 1560));
  assert(third.state == 3 && third.distance == 0);
}
