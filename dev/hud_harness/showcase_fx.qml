import QtQuick
import QtQuick.Window
import qs

// Section 25: the showcase's choreography offscreen: ui/ShowcaseFx.qml (scene titles, window frames, the "your day"
// card, the pill's trail, the beats, the finale) next to ui/CoreField.qml and the real pill trip (ui/PillTravel.qml),
// driven by a condensed script of the daemon's showcase.* events (the shapes jarvis/showcase/runner.py sends).
//   qml -I WORK showcase_fx.qml -- MODE OUT.png W H [WALLPAPER]      (dev/hud_harness/render_showcase_fx.sh)
// MODE: frames  grabs at the key moments: OUT-<name>.png
//       stop    a takeover in the middle of the finale: everything gone at once, `finished` right away
//       pacing  every frame's interval with the real render-thread Animators
// Prints "FX OK" or what failed.
Window {
    id: w

    function arg(i, d) {
        const a = Qt.application.arguments;
        const j = a.indexOf("--");
        return j >= 0 && a.length > j + i && a[j + i] !== "" ? a[j + i] : d;
    }
    readonly property string mode: arg(1, "frames")
    readonly property string out: arg(2, "fx.png")
    readonly property string wallpaper: arg(5, "")
    width: Number(arg(3, 2560))
    height: Number(arg(4, 1440))
    visible: true
    color: Theme.bg

    Image {
        anchors.fill: parent
        visible: w.wallpaper !== ""
        source: w.wallpaper !== "" ? "file://" + w.wallpaper : ""
        fillMode: Image.PreserveAspectCrop
        asynchronous: false
    }
    // A demo window stand-in (what the frame is drawn round): the terminal with fastfetch, Neovim with the live code
    // and the program running in its split below, a web page. Stand-in colours only (this is not the UI).
    Rectangle {
        id: demoWin
        property string kind: "term"
        property real typed: 0              // the code scene: how much of the program is on screen (0..1)
        property bool running: false        // the code scene: the program runs in the split below
        visible: false
        x: 0.0055 * w.width
        y: 0.0417 * w.height
        width: 0.989 * w.width
        height: 0.949 * w.height
        radius: 10 * Theme.round
        color: demoWin.kind === "web" ? "#f6f6f6" : "#16181d"
        clip: true
        // the terminal: a prompt, fastfetch's logo and lines
        Item {
            visible: demoWin.kind === "term"
            anchors.fill: parent
            Text { x: 40; y: 36; text: "daniel@arch:~$ fastfetch"; color: "#9ece6a"; font.family: "JetBrainsMono Nerd Font"; font.pixelSize: 26 }
            Text {
                x: 60; y: 110; color: "#7aa2f7"; font.family: "JetBrainsMono Nerd Font"; font.pixelSize: 22; lineHeight: 0.95
                text: "                  -`\n                 .o+`\n                `ooo/\n               `+oooo:\n              `+oooooo:\n              -+oooooo+:\n            `/:-:++oooo+:\n           `/++++/+++++++:\n          `/++++++++++++++:\n         `/+++ooooooooooooo/`\n        ./ooosssso++osssssso+`\n       .oossssso-````/ossssss+`\n      -osssssso.      :ssssssso.\n     :osssssss/        osssso+++.\n    /ossssssss/        +ssssooo/-\n  `/ossssso+/:-        -:/+osssso+-\n `+sso+:-`                 `.-/+oso:\n`++:.                           `-/+/"
            }
            Column {
                x: 640; y: 120; spacing: 10
                Repeater {
                    model: ["daniel@arch", "OS: Arch Linux x86_64", "Kernel: 6.18.54-1-lts", "WM: Hyprland (Wayland)", "Shell: fish", "Terminal: ghostty",
                            "CPU: AMD Ryzen 7 (16) @ 4.9 GHz", "GPU: NVIDIA GeForce RTX 3060 12 GB", "Memory: 21.3 GiB / 31.2 GiB", "Disk (/): 540 GiB / 953 GiB"]
                    delegate: Text { required property var modelData; text: modelData; color: "#c0caf5"; font.family: "JetBrainsMono Nerd Font"; font.pixelSize: 24 }
                }
            }
        }
        // Neovim: the program appears line by line; below it the terminal split with the program's output
        Item {
            visible: demoWin.kind === "code"
            anchors.fill: parent
            readonly property var code: [
                "#!/usr/bin/env python3", "\"\"\"An arc reactor in the terminal, written live by JARVIS.\"\"\"", "",
                "import math, shutil, sys, time", "", "", "def frame(t, cols, rows):", "    out = []",
                "    for y in range(rows):", "        line = []", "        for x in range(cols):",
                "            dx, dy = (x - cols / 2) / cols, (y - rows / 2) / rows * 2",
                "            r = math.hypot(dx, dy)", "            glow = math.sin(r * 40 - t * 6) * 0.5 + 0.5",
                "            v = int(255 * glow * max(0.0, 1 - r * 1.6))",
                "            line.append(f\"\\033[48;2;{v // 4};{v // 2};{v}m \")", "        out.append(\"\".join(line))",
                "    return \"\\033[0m\\n\".join(out)", "", "", "def main():", "    cols, rows = shutil.get_terminal_size()",
                "    start = time.time()", "    while time.time() - start < 6:", "        sys.stdout.write(\"\\033[H\" + frame(time.time(), cols, rows - 2))",
                "        sys.stdout.flush()", "    print(\"\\033[0m\\nWritten live by JARVIS.\")", "", "", "main()"]
            Column {
                x: 30; y: 24; spacing: 3
                Repeater {
                    model: parent.parent.code.slice(0, Math.round(parent.parent.code.length * demoWin.typed))
                    delegate: Row {
                        required property var modelData
                        required property int index
                        spacing: 22
                        Text { width: 40; horizontalAlignment: Text.AlignRight; text: index + 1; color: "#3b4261"; font.family: "JetBrainsMono Nerd Font"; font.pixelSize: 22 }
                        Text {
                            text: modelData
                            color: /^\s*(def|import|for|while|return)\b/.test(modelData) ? "#bb9af7" : /^\s*#|"""/.test(modelData) ? "#565f89" : "#c0caf5"
                            font.family: "JetBrainsMono Nerd Font"; font.pixelSize: 22
                        }
                    }
                }
            }
            Rectangle {   // the terminal split with the program's output
                visible: demoWin.running
                y: parent.height * 0.62
                width: parent.width
                height: parent.height * 0.38
                color: "#0b0c10"
                Rectangle { width: parent.width; height: 2; color: "#2a2e3a" }
                Repeater {
                    model: 9
                    delegate: Rectangle {
                        required property int index
                        anchors.centerIn: parent
                        width: (index + 1) * parent.height * 0.22
                        height: width * 0.5
                        radius: height / 2
                        color: "transparent"
                        border.width: parent.height * 0.05
                        border.color: Qt.rgba(0.25 + index * 0.06, 0.55 + index * 0.04, 1, 0.9 - index * 0.09)
                    }
                }
            }
        }
        Item {
            visible: demoWin.kind === "web"
            anchors.fill: parent
            Rectangle { width: parent.width; height: 74; color: "#e8e8ec" }
            Text { x: parent.width * 0.18; y: 150; text: "J.A.R.V.I.S."; color: "#202122"; font.family: "serif"; font.pixelSize: 64 }
            Rectangle { x: parent.width * 0.18; y: 240; width: parent.width * 0.64; height: 1; color: "#a2a9b1" }
            Column {
                x: parent.width * 0.18; y: 280; spacing: 20
                Repeater { model: 12; delegate: Rectangle { required property int index; width: w.width * (0.6 - (index % 4) * 0.05); height: 14; color: "#c8ccd1" } }
            }
        }
    }

    QtObject {
        id: ipc
        signal event(var msg)
        property bool connected: true
        property bool hudOpen: false
        property real level: 0
    }

    readonly property real surfaceW: Theme.pillMaxWidth
    readonly property real pillW: 196
    PillTravel {
        id: travel
        ipc: ipc
        homeX: Theme.pillLeft
        homeY: Theme.barTop
        areaW: w.width - w.surfaceW
        areaH: w.height - Theme.pillHeight
    }
    ShowcaseFx {
        id: fx
        anchors.fill: parent
        ipc: ipc
        travel: travel
        pillOffsetX: 0
        pillWidth: w.pillW
        onFinished: if (w.fxDoneAt < 0) w.fxDoneAt = w.now()
    }
    CoreField {
        id: field
        anchors.fill: parent
        ipc: ipc
        travel: travel
        pillOffsetX: 0
        pillWidth: w.pillW
        seed: 11
        onFinished: if (w.coresDoneAt < 0) w.coresDoneAt = w.now()   // (live, the layer is gone after the first)
    }
    Rectangle {  // the pill stand-in (above everything, like the real Overlay layer)
        x: travel.posX
        y: travel.posY
        width: w.pillW
        height: Theme.pillHeight
        radius: Theme.pillRadius
        color: Theme.surfaceRaised
        border.width: 1
        border.color: Theme.alpha(Theme.primary, 0.55)
        z: 5
        Text {
            anchors.centerIn: parent
            text: "JARVIS  ·  SHOWCASE"
            color: Theme.text
            font.family: Theme.fontMono
            font.pixelSize: 11
            font.letterSpacing: 2.0
        }
    }

    property double t0: Date.now()
    // The script's clock: frames mode runs it on the animation driver (offscreen, a slow grab holds the animations
    // back with it, so a grab shows the effects at the time it was asked for); else the wall clock.
    property real aclock: 0
    NumberAnimation {
        target: w
        property: "aclock"
        from: 0
        to: 200000
        duration: 200000
        running: w.mode === "frames"
    }
    function now() {
        return w.mode === "frames" ? w.aclock : Date.now() - w.t0;
    }
    property double coresDoneAt: -1
    property double fxDoneAt: -1
    property double stopAt: -1
    property var problems: []
    function progressBeats(app, from, to, n) {
        const out = [];
        for (let i = 1; i <= n; i++)
            out.push({ at: from + (to - from) * i / n, msg: { ev: "showcase.beat", app: app, kind: "progress", v: i / n }, typed: i / n });
        return out;
    }
    readonly property var frameMsg: ({ x: 0.0055, y: 0.0417, w: 0.989, h: 0.949 })
    property var script: [
        { at: 0, msg: { ev: "showcase.start", lang: "en", wordmark: "JARVIS", name: "JARVIS", chapters: 5 } },
        { at: 100, msg: { ev: "showcase.step", index: 1, id: "about", action: "hud_close", title: "All local", chapter: 1, chapters: 5, path: [[0.2, 0.3], [0.52, 0.18], [0.78, 0.26]] } },
        { at: 100, msg: { ev: "showcase.move", x: 0.2, y: 0.3, ms: 1400 } },
        { at: 1500, msg: { ev: "showcase.move", x: 0.52, y: 0.18, ms: 1700 } },
        { at: 3200, msg: { ev: "showcase.move", x: 0.78, y: 0.26, ms: 1700 } },
        // 02 this machine: the terminal, fastfetch typed, done
        { at: 5000, msg: { ev: "showcase.step", index: 2, id: "machine", action: "terminal", title: "This machine", chapter: 2, chapters: 5, path: [[0.88, 0.04]] } },
        { at: 5000, msg: { ev: "showcase.move", x: 0.88, y: 0.04, ms: 1500 } },
        { at: 6400, msg: Object.assign({ ev: "showcase.frame", app: "terminal" }, w.frameMsg), show: "term" },
        { at: 10400, msg: { ev: "showcase.beat", app: "terminal", kind: "done" } },
        { at: 12800, msg: { ev: "showcase.frame", clear: true }, hide: true },
        // 03 live coding: the code typed, the line count, the program runs (sparks), done
        { at: 12900, msg: { ev: "showcase.step", index: 3, id: "coding", action: "code", title: "Live coding", chapter: 3, chapters: 5, path: [[0.86, 0.03]] } },
        { at: 12900, msg: { ev: "showcase.move", x: 0.86, y: 0.03, ms: 1500 } },
        { at: 14300, msg: Object.assign({ ev: "showcase.frame", app: "code" }, w.frameMsg), show: "code" },
        { at: 21300, msg: { ev: "showcase.beat", app: "code", kind: "total", value: 30, label: "lines of Python" } },
        { at: 22600, msg: { ev: "showcase.beat", app: "code", kind: "run" }, run: true },
        { at: 26200, msg: { ev: "showcase.beat", app: "code", kind: "done" } },
        { at: 28400, msg: { ev: "showcase.frame", clear: true }, hide: true },
        // 04 your day: the question, the answer from the real widgets
        { at: 28500, msg: { ev: "showcase.step", index: 4, id: "day", action: "ask", title: "Your day", chapter: 4, chapters: 5, path: [[0.5, 0.06]] } },
        { at: 28500, msg: { ev: "showcase.move", x: 0.5, y: 0.06, ms: 1500 } },
        { at: 28600, msg: { ev: "showcase.card", kind: "question", label: "Your day", question: "What does my day look like?" } },
        { at: 30900, msg: { ev: "showcase.card", kind: "answer", label: "Your day", question: "What does my day look like?", text: "It's 23 degrees and clear, sir, with rain likely from eleven tonight. You have three reminders today, the next one at 20:16: stretch and refill water.", sources: ["Open-Meteo", "Reminders"] } },
        { at: 34800, msg: { ev: "showcase.card", kind: "clear" } },
        // 05 on the web: the second tab's scan
        { at: 34900, msg: { ev: "showcase.step", index: 5, id: "web", action: "browser", title: "On the web", chapter: 5, chapters: 5, path: [[0.88, 0.03]] } },
        { at: 34900, msg: { ev: "showcase.move", x: 0.88, y: 0.03, ms: 1500 } },
        { at: 36200, msg: Object.assign({ ev: "showcase.frame", app: "browser" }, w.frameMsg), show: "web" },
        { at: 38400, msg: { ev: "showcase.beat", app: "browser", kind: "tab" } },
        { at: 40400, msg: { ev: "showcase.frame", clear: true }, hide: true },
        // the finale
        { at: 40500, msg: { ev: "showcase.step", index: 6, id: "outro", action: "finish", title: "", chapter: 0, chapters: 5, path: [[0.5, 0.07]] } },
        { at: 40500, msg: { ev: "showcase.move", x: 0.5, y: 0.07, ms: 1800 } },
        { at: 40500, msg: { ev: "showcase.finale", phase: "recap", items: ["Code", "Desktop", "Web", "Your day"], wordmark: "JARVIS" } },
        { at: 47000, msg: { ev: "showcase.finale", phase: "reveal" } },
        { at: 49500, msg: { ev: "showcase.finale", phase: "name", line: "At your service, sir. Just say my name.", wordmark: "JARVIS" } },
        { at: 54000, msg: { ev: "showcase.end", status: "done", reason: "", home_ms: 1300 } }
    ].concat(w.progressBeats("code", 14800, 21000, 10)).sort((a, b) => a.at - b.at)
    readonly property var grabs: [
        { at: 2600, name: "title-corner" }, { at: 5250, name: "sweep-a" }, { at: 5600, name: "sweep-b" },
        { at: 5950, name: "title-center" }, { at: 6900, name: "frame" }, { at: 10750, name: "done-sheen" },
        { at: 11200, name: "done-badge" }, { at: 13300, name: "sweep-back" }, { at: 17800, name: "code-progress" },
        { at: 21600, name: "code-lines" }, { at: 23300, name: "code-run-sparks" }, { at: 26700, name: "code-done" },
        { at: 33000, name: "card-answer" }, { at: 38850, name: "tab-scan" }, { at: 45000, name: "recap" },
        { at: 47850, name: "converge" }, { at: 48200, name: "sparks" }, { at: 48700, name: "sparks2" },
        { at: 49300, name: "wordmark" }, { at: 52000, name: "orbit" }, { at: 53300, name: "orbit2" },
        { at: 54700, name: "fading" }
    ]
    Component.onCompleted: if (w.mode === "stop") w.script = w.script.filter(e => e.at <= 47000).concat([
        { at: 48300, msg: { ev: "showcase.stopping", reason: "key" } },
        { at: 49100, msg: { ev: "showcase.end", status: "stopped", reason: "key", home_ms: 1300 } }])
    property int next: 0
    property int grabbed: 0
    property int saved: 0

    function grab(name) {
        const asked = w.now();
        w.contentItem.grabToImage(r => {
            r.saveToFile(name);
            w.saved++;
            if (w.now() - asked > 250)
                console.log("grab " + name.replace(/.*-/, "") + " asked at " + asked + " ms, taken at " + w.now() + " ms");
        });
    }
    Timer {
        interval: 4
        repeat: true
        running: true
        onTriggered: {
            const now = w.now();
            while (w.next < w.script.length && now >= w.script[w.next].at) {
                const e = w.script[w.next];
                if (e.show) {
                    demoWin.kind = e.show;
                    demoWin.typed = 0;
                    demoWin.running = false;
                    demoWin.visible = true;
                }
                if (e.hide)
                    demoWin.visible = false;
                if (e.typed !== undefined)
                    demoWin.typed = e.typed;
                if (e.run)
                    demoWin.running = true;
                if (e.msg.ev === "showcase.stopping")
                    w.stopAt = w.now();
                ipc.event(e.msg);
                w.next++;
            }
            if (w.mode === "frames") {
                while (w.grabbed < w.grabs.length && now >= w.grabs[w.grabbed].at) {
                    w.grab(w.out.replace(/\.png$/, "-" + w.grabs[w.grabbed].name + ".png"));
                    w.grabbed++;
                }
                if (now >= 57000 && w.saved >= w.grabbed) {
                    running = false;
                    w.finish();
                }
            } else if (w.mode === "pacing") {
                if (now >= 57000) {
                    running = false;
                    w.finish();
                }
            } else if (now >= 50200) {
                running = false;
                const ok = w.stopAt > 0 && w.fxDoneAt >= w.stopAt && w.fxDoneAt < w.stopAt + 60
                    && w.coresDoneAt >= w.stopAt && w.coresDoneAt < w.stopAt + 60 && field.live === 0;
                console.log((ok ? "FX OK" : "FX FAIL") + " stop: fx finished " + w.fxDoneAt + " ms, cores "
                            + w.coresDoneAt + " ms (the stop sent at " + w.stopAt + "), " + field.live + " live after");
                Qt.quit();
            }
        }
    }
    // ── pacing: every presented frame's interval (the real render-thread Animators) ──
    property var dts: []
    property double lastSwap: 0
    Connections {
        target: w
        enabled: w.mode === "pacing"
        function onFrameSwapped() {
            const now = Date.now();
            if (w.lastSwap > 0)
                w.dts.push({ t: now - w.t0, dt: now - w.lastSwap });
            w.lastSwap = now;
        }
    }
    function pacing() {
        if (!w.dts.length)
            return;
        const d = w.dts.map(e => e.dt).sort((a, b) => a - b);
        const pct = q => d[Math.min(d.length - 1, Math.floor(q * d.length))];
        const mean = d.reduce((a, b) => a + b, 0) / d.length;
        console.log("PACING frames " + d.length + "  mean " + mean.toFixed(1) + " ms  p50 " + pct(0.5) + "  p95 "
                    + pct(0.95) + "  p99 " + pct(0.99) + "  max " + d[d.length - 1]);
        // offscreen has no vsync: a long interval is mostly idle (nothing changed, nothing drawn); a real hitch is
        // a 25-400 ms frame in the middle of running animations
        const hitches = w.dts.filter(e => e.dt > 25 && e.dt < 400).map(e => e.dt.toFixed(0) + "ms@" + (e.t / 1000).toFixed(2) + "s");
        console.log("PACING hitches (25-400 ms) " + hitches.length + ": " + hitches.join(" "));
    }
    function finish() {
        const p = w.problems.slice();
        if (fx.counts().sweeps < 3)
            p.push("only " + fx.counts().sweeps + " sweeps");
        if (fx.counts().beats < 12)
            p.push("only " + fx.counts().beats + " beats shown");
        if (fx.counts().cheers < 3)
            p.push("only " + fx.counts().cheers + " cheers");
        if (!fx.counts().sparked)
            p.push("the finale threw no sparks");
        if (field.dives < 1)
            p.push("no core dived into a window");
        if (w.fxDoneAt < 54000)
            p.push("fx finished " + w.fxDoneAt + " (before the end?)");
        if (w.coresDoneAt < 0)
            p.push("cores never finished");
        if (field.live > 0)
            p.push(field.live + " cores live at the end");
        console.log((p.length ? "FX FAIL: " + p.join("; ") : "FX OK") + " (fx done " + (w.fxDoneAt / 1000).toFixed(2)
                    + " s, cores done " + (w.coresDoneAt / 1000).toFixed(2) + " s, spawned " + field.spawned
                    + ", dived " + field.dives + ", " + JSON.stringify(fx.counts()) + ")");
        w.pacing();
        Qt.quit();
    }
}
