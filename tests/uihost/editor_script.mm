// Scripted editor session (VST2 in a real NSWindow): actions run from a run-loop timer, so
// they also reach the editor while one of its menus or dialogs runs a nested event loop
// (the drawn ones: SQ8L_DRAWN_UI=1). After each action the frame the plugin last showed
// (SQ8L_UI_FRAME) is copied to <out dir>/<n>.ppm.
//
//   editor_script plugin.vst/Contents/MacOS/x <out dir> "c 100,12; m 120,40; k esc; c 300,200"
//   c x,y = left click, r x,y = right click, m x,y = move, k esc|ret|down|up|<char> = key
#import <Cocoa/Cocoa.h>
#include <dlfcn.h>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <vector>

struct AEffect;
typedef intptr_t (*Dispatcher)(AEffect*, int32_t, int32_t, intptr_t, void*, float);
struct AEffect {
    int32_t magic; Dispatcher dispatcher; void* process; void* setParameter; void* getParameter;
    int32_t numPrograms, numParams, numInputs, numOutputs, flags;
    intptr_t resvd1, resvd2; int32_t initialDelay, realQualities, offQualities; float ioRatio;
    void* object; void* user; int32_t uniqueID, version; void* processReplacing; void* processDouble; char future[56];
};
static size_t next = 0;  // script state (globals: shared by the timer block and the lambdas)
static int shots = 0;
static bool finished = false;

static intptr_t hostCb(AEffect*, int32_t op, int32_t, intptr_t, void*, float) {
    if (op == 1) return 2400;
    if (op == 16) return 44100;
    return 0;
}

int main(int argc, char** argv) {
    @autoreleasepool {
        if (argc < 4) { fprintf(stderr, "usage: editor_script plugin outdir script\n"); return 2; }
        const std::string out = argv[2];
        const std::string frame = out + "/frame.ppm";
        setenv("SQ8L_UI_FRAME", frame.c_str(), 1);
        void* h = dlopen(argv[1], RTLD_NOW);
        if (!h) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 1; }
        auto entry = (AEffect* (*)(void*))dlsym(h, "VSTPluginMain");
        AEffect* e = entry((void*)hostCb);
        e->dispatcher(e, 0, 0, 0, nullptr, 0);
        e->dispatcher(e, 10, 0, 0, nullptr, 44100.f);
        e->dispatcher(e, 12, 0, 1, nullptr, 0);
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        NSWindow* win = [[NSWindow alloc] initWithContentRect:NSMakeRect(100, 100, 626, 430)
                                                    styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
        [NSApp finishLaunching];
        [NSApp activateIgnoringOtherApps:YES];
        [win makeKeyAndOrderFront:nil];
        e->dispatcher(e, 14, 0, 0, (__bridge void*)[win contentView], 0);
        NSView* content = [win contentView];
        NSView* target = content.subviews.count ? content.subviews.firstObject : content;
        [win makeFirstResponder:target];

        std::vector<std::string> steps;
        {
            std::string all = argv[3];
            size_t pos = 0;
            while (pos < all.size()) {
                size_t end = all.find(';', pos);
                std::string one = all.substr(pos, end == std::string::npos ? std::string::npos : end - pos);
                while (!one.empty() && one[0] == ' ') one.erase(0, 1);
                if (!one.empty()) steps.push_back(one);
                if (end == std::string::npos) break;
                pos = end + 1;
            }
        }
        auto point = [&](int fx, int fy) {
            const double sx = target.bounds.size.width / 626.0;
            NSPoint inView = NSMakePoint(fx * sx, target.isFlipped ? fy * sx : target.bounds.size.height - fy * sx);
            return [target convertPoint:inView toView:nil];
        };
        auto mouse = [&](NSEventType t, NSPoint inWin) {
            NSEvent* ev = [NSEvent mouseEventWithType:t location:inWin modifierFlags:0
                                            timestamp:[[NSProcessInfo processInfo] systemUptime]
                                         windowNumber:win.windowNumber context:nil eventNumber:0
                                           clickCount:1 pressure:(t == NSEventTypeLeftMouseDown || t == NSEventTypeRightMouseDown) ? 1.0 : 0.0];
            NSView* hit = [content hitTest:[content convertPoint:inWin fromView:nil]] ?: target;
            switch (t) {
            case NSEventTypeLeftMouseDown: [hit mouseDown:ev]; break;
            case NSEventTypeLeftMouseUp: [hit mouseUp:ev]; break;
            case NSEventTypeRightMouseDown: [hit rightMouseDown:ev]; break;
            case NSEventTypeRightMouseUp: [hit rightMouseUp:ev]; break;
            default: [hit mouseMoved:ev]; break;
            }
        };
        auto key = [&](const std::string& k) {
            NSString* chars = k == "esc" ? @"\x1b" : k == "ret" ? @"\r" : k == "down" ? [NSString stringWithFormat:@"%C", (unichar)NSDownArrowFunctionKey]
                            : k == "up" ? [NSString stringWithFormat:@"%C", (unichar)NSUpArrowFunctionKey]
                            : k == "bs" ? @"\x7f" : [NSString stringWithUTF8String:k.c_str()];
            const unsigned short code = k == "esc" ? 53 : k == "ret" ? 36 : k == "down" ? 125 : k == "up" ? 126 : k == "bs" ? 51 : 0;
            for (NSEventType t : {NSEventTypeKeyDown, NSEventTypeKeyUp}) {
                NSEvent* ev = [NSEvent keyEventWithType:t location:NSZeroPoint modifierFlags:0
                                              timestamp:[[NSProcessInfo processInfo] systemUptime]
                                           windowNumber:win.windowNumber context:nil characters:chars
                                charactersIgnoringModifiers:chars isARepeat:NO keyCode:code];
                [NSApp postEvent:ev atStart:NO];  // through NSApp, so [NSApp currentEvent] is the key
            }
        };
        auto shot = [&] {
            const std::string to = out + "/" + std::to_string(++shots) + ".ppm";
            if (FILE* f = fopen(frame.c_str(), "rb")) {
                std::vector<char> buf(1 << 21);
                size_t n = fread(buf.data(), 1, buf.size(), f);
                fclose(f);
                if (FILE* g = fopen(to.c_str(), "wb")) { fwrite(buf.data(), 1, n, g); fclose(g); }
            }
        };
        NSTimer* timer = [NSTimer timerWithTimeInterval:0.35 repeats:YES block:^(NSTimer* t) {
            if (shots < (int)next) shot();  // the frame after the previous action
            if (next >= steps.size()) { finished = true; [t invalidate]; return; }
            const std::string s = steps[next++];
            printf("step %zu: %s\n", next, s.c_str());
            fflush(stdout);
            // Run the action from a one-shot timer: if it opens a modal loop, this repeating
            // timer (not inside its own callout) keeps firing in there with the next actions.
            [[NSRunLoop currentRunLoop] addTimer:[NSTimer timerWithTimeInterval:0 repeats:NO block:^(NSTimer*) {
            int x = 0, y = 0;
            if (s[0] == 'c' || s[0] == 'r') {
                sscanf(s.c_str() + 1, "%d,%d", &x, &y);
                const bool right = s[0] == 'r';
                mouse(NSEventTypeMouseMoved, point(x, y));
                mouse(right ? NSEventTypeRightMouseDown : NSEventTypeLeftMouseDown, point(x, y));
                mouse(right ? NSEventTypeRightMouseUp : NSEventTypeLeftMouseUp, point(x, y));
            } else if (s[0] == 'm') {
                sscanf(s.c_str() + 1, "%d,%d", &x, &y);
                mouse(NSEventTypeMouseMoved, point(x, y));
            } else if (s[0] == 'k') {
                key(s.substr(2));
            }
            }] forMode:NSDefaultRunLoopMode];
        }];
        [[NSRunLoop currentRunLoop] addTimer:timer forMode:NSDefaultRunLoopMode];
        NSDate* deadline = [NSDate dateWithTimeIntervalSinceNow:60];
        while (!finished && [deadline timeIntervalSinceNow] > 0) {
            e->dispatcher(e, 19, 0, 0, nullptr, 0);
            NSEvent* ev = [NSApp nextEventMatchingMask:NSEventMaskAny untilDate:[NSDate dateWithTimeIntervalSinceNow:0.02]
                                                inMode:NSDefaultRunLoopMode dequeue:YES];
            if (ev) [NSApp sendEvent:ev];
        }
        e->dispatcher(e, 15, 0, 0, nullptr, 0);
        [win close];
        e->dispatcher(e, 12, 0, 0, nullptr, 0);
        e->dispatcher(e, 1, 0, 0, nullptr, 0);
        printf("%s after %d steps\n", finished ? "OK" : "TIMEOUT", (int)next);
    }
    return 0;
}
