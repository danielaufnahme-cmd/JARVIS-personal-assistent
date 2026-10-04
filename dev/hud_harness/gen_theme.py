#!/usr/bin/env python3
"""ui/Theme.qml -> a plain-Qt singleton for the offscreen harness: no Quickshell, palette read via XHR."""
import re, sys, os
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
s = re.sub(r"^import Quickshell.*\n", "", s, flags=re.M)
# drop FileView { ... } blocks (brace matched)
out, i = [], 0
while True:
    j = s.find("FileView {", i)
    if j < 0:
        out.append(s[i:]); break
    out.append(s[i:j])
    depth, k = 0, j
    while True:
        if s[k] == "{": depth += 1
        elif s[k] == "}":
            depth -= 1
            if depth == 0: break
        k += 1
    i = k + 1
s = "".join(out)
s = s.replace("Singleton {", "QtObject {", 1)
home = os.environ["HOME"]
loader = '''
    function _read(path) {
        const x = new XMLHttpRequest();
        x.open("GET", "file://" + path, false);
        x.send();
        return x.responseText || "";
    }
    Component.onCompleted: {
        try { const d = JSON.parse(_read("%s/.config/opencode/themes/matugen.json")).defs; if (d && d.primary) theme.m3 = d; } catch (e) {}
        const out = {}; const re = /@define-color\\s+([a-z_]+)\\s+(#[0-9a-fA-F]{6})\\s*;/g; const t = _read("%s/.config/gtk-4.0/noctalia.css"); let m;
        while ((m = re.exec(t)) !== null) out[m[1]] = m[2];
        theme.gtk = out;
    }
''' % (home, home)
k = s.rstrip().rfind("}")
s = s[:k] + loader + "}\n"
open(dst, "w").write(s)
