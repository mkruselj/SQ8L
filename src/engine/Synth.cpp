#include "Synth.h"

#include <cstddef>
#include <cstring>

#include "Fpu.h"
#include "WaveRom.h"

namespace sq8l {

namespace {
constexpr float kControlRate = 83.592575f;  // 0x42a72f66, rate of LFOs/envelopes/followers
}

// The master's view of the Cdoc parameter block and of the LFO inputs alias the module
// storage (identical layouts, checked here).
static_assert(sizeof(MasterDocParams) == sizeof(DocVoiceParams), "Doc params layout");
static_assert(offsetof(Lfo, smoothIn) - offsetof(Lfo, freqIn) == 0x34, "Lfo input block layout");
static_assert(sizeof(LfoParams) == 0x38, "LfoParams layout");

SynthModules::SynthModules() : doc(kOriginalVoiceSlots, kMaxVoiceSlots) {
    RoundToNearest rn;
    // Master constructor order: Cdoc, then per voice the follower, 4 LFOs (with the
    // ROM wave callback), 4 envelopes, (filters,) the amp.
    for (int v = 0; v < kMaxVoiceSlots; v++) {
        foll[v].init(kControlRate);
        for (int i = 0; i < 4; i++) {
            lfo[v][i].construct(kControlRate);
            lfo[v][i].waveSource = &lfoWaveLocation;
        }
        for (int i = 0; i < 4; i++) env[v][i].init(kControlRate);
        amp[v].init();
    }
}

MasterDocParams& SynthModules::docVoiceParams(int v) {
    return *reinterpret_cast<MasterDocParams*>(doc.params(static_cast<uint32_t>(v)));
}

LfoParams& SynthModules::lfoParams(int v, int i) {
    return *reinterpret_cast<LfoParams*>(&lfo[v][i].freqIn);
}

Synth::Synth(float sampleRate, SoundLibrary* library, const Settings* settings, MasterNotify* notify) {
    if (library) {
        library_ = library;
    } else {
        ownLibrary_ = std::make_unique<SoundLibrary>();
        library_ = ownLibrary_.get();
    }
    if (settings) settings_ = *settings;
    modules_ = std::make_unique<SynthModules>();
    edit_ = std::make_unique<EditBuffer>(library_);
    {
        RoundToNearest rn;
        // CSynth ctor: master created with Round(sampleRate) of the AudioEffect (44100 default).
        master_ = std::make_unique<Master>(*modules_, fistp(sampleRate), settings_.synth, notify);
        // (port) OPTIONS -> Polyphony; the original's 8 + 8 are set by the constructor.
        if (settings_.polyphony() != kOriginalPlayableVoices) master_->setVoices(settings_.polyphony(), kFadeVoices);
    }
    edit_->listener = [this](EditBuffer::Event e, const Program* slot) {
        syncProgram();
        master_->editBufferEvent(static_cast<int32_t>(e), slot ? slot->bytes : nullptr);
    };
    syncProgram();
    // End of the master constructor: select program A000 (FUN_00460db8(editbuf, 0, -1)).
    edit_->selectProgram(0, -1);
    syncProgram();
}

void Synth::syncProgram() {
    master_->setCurrentProgram(edit_->current().bytes, static_cast<uint16_t>(edit_->programNumber()));
}

void Synth::setSampleRate(float sr) {
    RoundToNearest rn;
    master_->setSampleRate(sr);
}

void Synth::processEvents(const RawMidiEvent* ev, int32_t n) { master_->processEvents(ev, n); }

void Synth::process(float* outL, float* outR, int32_t n, bool replacing) {
    AudioFpuScope fpu;  // no FTZ/DAZ; the master sets round-toward-zero itself
    master_->process(outL, outR, n, replacing);
}

void Synth::setProgram(int32_t index) {
    if (chunkLoaded_) {
        chunkLoaded_ = false;
        return;
    }
    edit_->selectIndex(index);
    syncProgram();
}

int32_t Synth::program() const { return edit_->libraryIndex(-1, -1); }

std::string Synth::programName() const { return edit_->name(0); }

std::string Synth::programNameIndexed(int32_t index) const { return library_->programName(index); }

void Synth::setProgramName(const std::string& name) {
    edit_->setName(0, name);
    syncProgram();
}

std::vector<uint8_t> Synth::getChunk() { return edit_->getChunk(); }

int32_t Synth::setChunk(const uint8_t* data, size_t size) {
    const int n = edit_->setChunk(data, size);
    syncProgram();
    if (n > 0) {
        chunkLoaded_ = true;
        return 0;
    }
    return -1;
}

void Synth::setPolyphony(int voices) {
    if (voices < kOriginalPlayableVoices) voices = kOriginalPlayableVoices;
    if (voices > kMaxPlayableVoices) voices = kMaxPlayableVoices;
    if (voices == master_->playableVoices()) return;
    RoundToNearest rn;
    master_->setVoices(voices, kFadeVoices);
}

}  // namespace sq8l
