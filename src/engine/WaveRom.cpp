#include "WaveRom.h"

namespace sq8l {

WaveSample waveSample(uint32_t wave, int32_t key) {
    if (wave >= static_cast<uint32_t>(kNumWaves)) return {0, 0};
    const int32_t k = key < 0 ? 0 : (key > 127 ? 127 : key);
    const uint8_t rec = data::kWaveKeyMap[wave * 16 + (static_cast<uint32_t>(k) >> 3)];
    return {data::kWaveSampleRecords[rec * 4], data::kWaveSampleRecords[rec * 4 + 1]};
}

WaveLocation lfoWaveLocation(int32_t key, uint32_t wave) {
    const WaveSample ws = waveSample(wave, key);
    const uint32_t addr = ((waveBank(ws.waveReg) << 16) + (static_cast<uint32_t>(ws.page) << 8)) & 0x3FFFFu;
    return {data::kWaveRom + addr, waveSizeLog2(ws.waveReg)};
}

}  // namespace sq8l
