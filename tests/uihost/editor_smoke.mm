// Smoke test: open the plugin editor (VST2) in a real NSWindow, idle it, close it.
// Optionally saves a screenshot of the window (PNG) when a path is given.
#import <Cocoa/Cocoa.h>
#include <dlfcn.h>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <string>

struct AEffect;
typedef intptr_t (*Dispatcher)(AEffect*, int32_t, int32_t, intptr_t, void*, float);
struct AEffect {
    int32_t magic; Dispatcher dispatcher; void* process; void* setParameter; void* getParameter;
    int32_t numPrograms, numParams, numInputs, numOutputs, flags;
    intptr_t resvd1, resvd2; int32_t initialDelay, realQualities, offQualities; float ioRatio;
    void* object; void* user; int32_t uniqueID, version; void* processReplacing; void* processDouble; char future[56];
};
static intptr_t hostCb(AEffect*, int32_t op, int32_t, intptr_t, void*, float) {
    if (op == 1) return 2400;
    if (op == 16) return 44100;
    return 0;
}

int main(int argc, char** argv) {
    @autoreleasepool {
        if (argc < 2) { fprintf(stderr, "usage: editor_smoke plugin.vst/Contents/MacOS/x [shot.png]\n"); return 2; }
        void* h = dlopen(argv[1], RTLD_NOW);
        if (!h) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 1; }
        auto entry = (AEffect* (*)(void*))dlsym(h, "VSTPluginMain");
        AEffect* e = entry((void*)hostCb);
        e->dispatcher(e, 0, 0, 0, nullptr, 0);          // open
        e->dispatcher(e, 10, 0, 0, nullptr, 44100.f);   // sample rate
        e->dispatcher(e, 12, 0, 1, nullptr, 0);         // resume
        printf("flags %#x (hasEditor %d)\n", e->flags, e->flags & 1);
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        int16_t* rect = nullptr;
        e->dispatcher(e, 13, 0, 0, &rect, 0);
        int w = rect ? rect[3] - rect[1] : 626, hgt = rect ? rect[2] - rect[0] : 430;
        printf("editor rect %dx%d\n", w, hgt);
        NSWindow* win = [[NSWindow alloc] initWithContentRect:NSMakeRect(100, 100, w, hgt)
                                                    styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
        [win makeKeyAndOrderFront:nil];
        intptr_t r = e->dispatcher(e, 14, 0, 0, (__bridge void*)[win contentView], 0);   // editOpen
        printf("editOpen -> %ld\n", (long)r);
        auto idle = [&](double secs) {
            NSDate* until = [NSDate dateWithTimeIntervalSinceNow:secs];
            while ([until timeIntervalSinceNow] > 0) {
                e->dispatcher(e, 19, 0, 0, nullptr, 0);   // editIdle
                [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.02]];
            }
        };
        idle(1.5);
        // SQ8L_SMOKE_CLICKS="x,y;x,y" : left clicks at form coordinates (626x430 space)
        if (const char* clicks = getenv("SQ8L_SMOKE_CLICKS")) {
            NSView* content = [win contentView];
            NSView* target = content.subviews.count ? content.subviews.firstObject : content;
            const double sx = target.bounds.size.width / 626.0;
            std::string all = clicks;
            size_t pos = 0;
            while (pos < all.size()) {
                size_t end = all.find(';', pos);
                std::string one = all.substr(pos, end == std::string::npos ? std::string::npos : end - pos);
                int fx = 0, fy = 0;
                sscanf(one.c_str(), "%d,%d", &fx, &fy);
                NSPoint inView = NSMakePoint(fx * sx, target.isFlipped ? fy * sx : target.bounds.size.height - fy * sx);
                NSPoint inWin = [target convertPoint:inView toView:nil];
                NSView* hit = [content hitTest:[content convertPoint:inWin fromView:nil]] ?: target;
                for (NSEventType t : {NSEventTypeLeftMouseDown, NSEventTypeLeftMouseUp}) {
                    NSEvent* ev = [NSEvent mouseEventWithType:t location:inWin modifierFlags:0
                                                    timestamp:[[NSProcessInfo processInfo] systemUptime]
                                                 windowNumber:win.windowNumber context:nil eventNumber:0
                                                   clickCount:1 pressure:t == NSEventTypeLeftMouseDown ? 1.0 : 0.0];
                    if (t == NSEventTypeLeftMouseDown) [hit mouseDown:ev]; else [hit mouseUp:ev];
                }
                printf("clicked %d,%d\n", fx, fy);
                idle(0.6);
                if (end == std::string::npos) break;
                pos = end + 1;
            }
        }
        idle(0.5);
        e->dispatcher(e, 15, 0, 0, nullptr, 0);   // editClose
        [win close];
        e->dispatcher(e, 12, 0, 0, nullptr, 0);
        e->dispatcher(e, 1, 0, 0, nullptr, 0);    // close
        printf("OK\n");
    }
    return 0;
}
