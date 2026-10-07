import QtQuick
import qs

// ui/Ipc.qml without its socket (gen_ipc.py), fed the daemon's lines directly: checks the showcase state, the HUD's
// at-once open/close and the offline fallback. Prints "IPC OK" or what failed.
//   qml -I WORK ipc_check.qml        (dev/hud_harness/check_ipc.sh)
Item {
    id: t

    Ipc {
        id: ipc
    }
    property var sent: []
    property var problems: []
    function check(ok, what) {
        if (!ok)
            t.problems = t.problems.concat([what]);
    }
    function line(obj) {
        ipc._handle(JSON.stringify(obj));
    }

    Component.onCompleted: {
        ipc._connected = true;
        ipc.sock = { write: s => t.sent.push(JSON.parse(s)), flush: () => {}, destroy: () => {} };
        t.line({ ev: "snapshot", state: { mode: "idle", session: false }, hud: { open: false },
                 showcase: { active: true, lang: "en" }, computer: { active: false } });
        t.check(ipc.showcaseActive && ipc.showcaseLang === "en", "snapshot's showcase not applied");
        t.line({ ev: "showcase.end", status: "done", reason: "", home_ms: 1300 });
        t.check(!ipc.showcaseActive, "showcase.end didn't end it");
        t.line({ ev: "showcase.start", lang: "en", steps: [], total: 0 });
        t.check(ipc.showcaseActive, "showcase.start didn't start it");
        t.line({ ev: "showcase", active: false });
        t.check(!ipc.showcaseActive, "section 24's showcase event not applied");
        t.line({ ev: "bogus" });
        ipc._handle("not json");
        // the HUD: a toggle opens at once and goes out as an explicit open
        ipc.send({ cmd: "hud.toggle" });
        t.check(ipc.hudOpen, "hud.toggle didn't open the HUD at once");
        t.check(t.sent.length === 1 && t.sent[0].cmd === "hud.open", "sent " + JSON.stringify(t.sent));
        ipc.send({ cmd: "hud.toggle" });
        t.check(!ipc.hudOpen && t.sent[1].cmd === "hud.close", "the second toggle didn't close it");
        ipc.send({ cmd: "say", text: "hi" });
        t.check(t.sent[2].cmd === "say", "other commands changed");
        // the daemon goes away: calm offline state, the showcase off
        t.line({ ev: "showcase.start", lang: "en" });
        ipc._onSocketState(ipc.sock, false);
        t.check(!ipc.showcaseActive && !ipc.hudOpen && ipc.mode === "idle", "offline fallback");
        console.log(t.problems.length ? "IPC FAILED: " + t.problems.join("; ") : "IPC OK");
        Qt.quit();
    }
}
