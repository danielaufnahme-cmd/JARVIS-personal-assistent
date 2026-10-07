#!/usr/bin/env python3
"""ui/Ipc.qml -> a plain-Qt Item for ipc_check.qml: no Quickshell, no socket (the check feeds `_handle` itself)."""
import re
import sys

src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
s = re.sub(r"^import Quickshell.*\n", "", s, flags=re.M)
s = s.replace("Scope {", "Item {", 1)
s = re.sub(r'Quickshell\.env\("[A-Z_]+"\)', '""', s)
# the socket component and the connect attempts: nothing to connect to
i = s.index("    Component {\n        id: socketComponent")
depth, k = 0, s.index("{", i)
while True:
    if s[k] == "{":
        depth += 1
    elif s[k] == "}":
        depth -= 1
        if depth == 0:
            break
    k += 1
s = s[:i] + s[k + 1:]
s = s.replace("Component.onCompleted: _reconnect()", "")
s = s.replace("onTriggered: root._reconnect()", "onTriggered: {}")
open(dst, "w").write(s)
