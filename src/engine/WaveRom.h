// SQ-80 wave ROM and wavesample lookup (original units wavrom, docWaveParams).
#pragma once

#include <cstdint>

namespace sq8l {

namespace data {
extern const uint8_t kWaveRom[0x40000];
extern const uint8_t kWaveKeyMap[1200];
extern const uint8_t kWaveSampleRecords[604];
}  // namespace data

constexpr int kNumWaves = 75;
constexpr uint32_t kWaveRomSize = 0x40000;

// Wavesample record for a wave at a MIDI key (FUN_00455f84): page = ROM address
// bits 15..8, waveReg = DOC 5503 wavetable register (bank, table size, resolution).
// Waves >= 75 yield {0, 0}.
struct WaveSample {
    uint8_t page;
    uint8_t waveReg;
};
WaveSample waveSample(uint32_t wave, int32_t key);

// Wavetable bank (0..3) encoded in a waveReg: bit 6 -> bank bit 1, bit 7 -> bank bit 0.
inline uint32_t waveBank(uint8_t waveReg) { return ((waveReg & 0x40u) >> 5) | (waveReg >> 7); }

// log2 of the wavetable length (8..15 -> 256..32768 bytes).
inline int32_t waveSizeLog2(uint8_t waveReg) { return ((waveReg >> 3) & 7) + 8; }

// Start of a wavesample in the ROM, as used by the LFOs (callback LAB_0045c4f8).
struct WaveLocation {
    const uint8_t* data;
    int32_t sizeLog2;
};
WaveLocation lfoWaveLocation(int32_t key, uint32_t wave);

}  // namespace sq8l
