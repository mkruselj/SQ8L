#include "Sprite.h"

#include <utility>

#include "data/GuiData.h"

namespace sq8l::gui {

Sprite::Sprite(std::string name, int frameWidth, int frameHeight, int count, std::vector<Color> pixels)
    : name_(std::move(name)), fw_(frameWidth), fh_(frameHeight), count_(count), px_(std::move(pixels)) {}

namespace assets {
namespace {

struct Store {
    Bitmap background;
    Sprite sprites[data::kSpriteCount];

    Store() {
        background = Bitmap(data::kBackgroundWidth, data::kBackgroundHeight);
        auto& px = background.pixels();
        for (size_t i = 0; i < px.size(); i++) px[i] = data::kBackgroundPalette[data::kBackgroundIndex[i]];
        for (int s = 0; s < data::kSpriteCount; s++) {
            const data::SpriteData& d = data::kSprites[s];
            size_t n = static_cast<size_t>(d.frameWidth) * d.frameHeight * d.count;
            std::vector<Color> v(n);
            for (size_t i = 0; i < n; i++) v[i] = d.palette[d.index[i]];
            sprites[s] = Sprite(d.name, d.frameWidth, d.frameHeight, d.count, std::move(v));
        }
    }
};

const Store& store() {
    static const Store s;  // thread-safe initialization
    return s;
}

}  // namespace

const Bitmap& background() { return store().background; }
const Sprite& knobGif() { return store().sprites[0]; }
const Sprite& charGif() { return store().sprites[1]; }
const Sprite& numCharGif() { return store().sprites[2]; }
const Sprite& ledGif() { return store().sprites[3]; }
const Sprite& buttGif() { return store().sprites[4]; }
const Sprite& smallButtGif() { return store().sprites[5]; }
const Sprite& upDownGif() { return store().sprites[6]; }
const Sprite& scrButtGif() { return store().sprites[7]; }

const Sprite* byName(const std::string& name) {
    for (const Sprite& s : store().sprites)
        if (s.name() == name) return &s;
    return nullptr;
}

}  // namespace assets
}  // namespace sq8l::gui
