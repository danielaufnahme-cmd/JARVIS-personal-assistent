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
        // section 28: the snapshot's keys (a reconnect shows them again) and the live events
        t.line({ ev: "snapshot", state: { mode: "idle", session: true }, hud: { open: false },
                 attach: { items: [{ kind: "region", name: "Screen region", thumb: "iVBORw0KGgo=" }, { kind: "bogus", name: 7 }] },
                 meeting: { active: true, started_at: 1790000000, title: "Sync" },
                 notify: { count: 3, top: "Anna: lunch?" },
                 focus: { active: true, paused: false, label: "Invoices", started_at: 1790000000, ends_at: 1790002700 } });
        t.check(ipc.attachments.length === 2 && ipc.attachments[0].thumb === "iVBORw0KGgo=" && ipc.attachments[1].kind === "file"
                && ipc.attachments[1].name === "7", "snapshot attach " + JSON.stringify(ipc.attachments));
        t.check(ipc.meeting && ipc.meeting.startedAt === 1790000000000 && ipc.meeting.title === "Sync", "snapshot meeting");
        t.check(ipc.notifyCount === 3 && ipc.notifyTop === "Anna: lunch?", "snapshot notify");
        t.check(ipc.focusState && ipc.focusState.endsAt === 1790002700000 && !ipc.focusState.paused, "snapshot focus");
        t.line({ ev: "focus.state", active: true, paused: true, label: "Invoices", started_at: 1790000000, ends_at: 1790002700 });
        const left = ipc.focusState.pausedLeftMs;
        t.line({ ev: "focus.state", active: true, paused: true, label: "Invoices", started_at: 1790000000, ends_at: 1790002700 });
        t.check(ipc.focusState.paused && ipc.focusState.pausedLeftMs === left, "a paused focus doesn't run down");
        t.line({ ev: "focus.state", active: false, paused: false, label: "", started_at: null, ends_at: null });
        t.check(ipc.focusState === null, "focus.state inactive");
        t.line({ ev: "meeting.state", active: false });
        t.check(ipc.meeting === null, "meeting.state inactive");
        t.line({ ev: "notify.unseen", count: 0, top: "stale" });
        t.check(ipc.notifyCount === 0 && ipc.notifyTop === "", "notify.unseen 0");
        t.line({ ev: "attach.state", items: [] });
        t.check(ipc.attachments.length === 0, "attach.state empty");
        t.line({ ev: "search.results", query: "invoice", items: [1, 2, 3, 4, 5, 6, 7].map(i => ({ path: "/h/f" + i, name: "f" + i,
                 folder: "Documents", modified: 1790000000, snippet: "s" })) });
        t.check(ipc.searchResults.items.length === 5 && ipc.searchResults.items[0].modified === 1790000000000
                && ipc.searchResults.query === "invoice", "search.results");
        let saved = "";
        ipc.memorySaved.connect((kind, text) => saved = kind + ":" + text);
        t.line({ ev: "memory.saved", kind: "fact", text: "car on level 3" });
        t.check(saved === "fact:car on level 3", "memory.saved signal " + saved);
        // the daemon goes away: calm offline state, the showcase off
        t.line({ ev: "showcase.start", lang: "en" });
        t.line({ ev: "meeting.state", active: true, started_at: 1790000000, title: "" });
        ipc._onSocketState(ipc.sock, false);
        t.check(!ipc.showcaseActive && !ipc.hudOpen && ipc.mode === "idle", "offline fallback");
        t.check(ipc.meeting === null && ipc.attachments.length === 0, "section 28 state kept while offline");
        console.log(t.problems.length ? "IPC FAILED: " + t.problems.join("; ") : "IPC OK");
        Qt.quit();
    }
}
