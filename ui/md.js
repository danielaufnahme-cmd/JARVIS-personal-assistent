.pragma library

// Markdown → Qt rich text for the deep answer (section 10: the corner reading panel and the HUD's panel ⑦).
//
// Why not Text.MarkdownText: Qt sets code in the platform's fixed font at its own point size (on this machine a
// large proportional face, even with qt6ct), and it loads images, so an `![](http://…)` in an answer that quotes a
// web page would be fetched from the network. This renderer escapes every raw HTML tag the model writes, never
// emits an image (only its alt text), styles code with the UI's monospace, and keeps headings compact.
// It is line based and tolerant of half-written input, because the answer is rendered while it streams.
//
// style: { text, muted, accent, codeText, codeBg, rule, mono, px, codePx, h1Px, h3Px }  (colours as "#rrggbb")

function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function inline(src, st) {
    const saved = [];
    const keep = html => {
        saved.push(html);
        return "\u0000" + (saved.length - 1) + "\u0000";
    };
    // Code spans first: nothing inside them is markdown.
    let s = String(src).replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (m, ticks, code) =>
        keep('<span style="font-family:\'' + st.mono + '\'; font-size:' + st.codePx + 'px; color:' + st.codeText
            + '; background-color:' + st.codeBg + '">&nbsp;' + esc(code.trim()) + '&nbsp;</span>'));
    s = esc(s);
    s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");                       // images: alt text only
    s = s.replace(/\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)/g,                  // links: shown, never opened
        (m, t) => keep('<span style="color:' + st.accent + '; text-decoration:underline">' + t + '</span>'));
    s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<b>$1</b>");
    s = s.replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<b>$2</b>");
    s = s.replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])\*(?!\*)/g, "$1<i>$2</i>");
    s = s.replace(/(^|[^\w])_(?=[^\s_])([^_]*?[^\s_])_(?!\w)/g, "$1<i>$2</i>");
    s = s.replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<s>$1</s>");
    return s.replace(/\u0000(\d+)\u0000/g, (m, i) => saved[Number(i)]);
}

const FENCE = /^\s{0,3}(`{3,}|~{3,})/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const ITEM = /^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function startsBlock(line, next) {
    return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || ITEM.test(line) || QUOTE.test(line)
        || (line.indexOf("|") >= 0 && next !== undefined && TABLE_SEP.test(next));
}

function cells(line) {
    let t = line.trim();
    if (t.startsWith("|"))
        t = t.slice(1);
    if (t.endsWith("|"))
        t = t.slice(0, -1);
    return t.split("|").map(c => c.trim());
}

function codeBlock(lines, st) {
    return '<table width="100%" cellspacing="0" cellpadding="8" bgcolor="' + st.codeBg
        + '" style="margin-top:2px; margin-bottom:10px"><tr><td><pre style="font-family:\'' + st.mono
        + '\'; font-size:' + st.codePx + 'px; color:' + st.codeText + '">' + esc(lines.join("\n")) + '</pre></td></tr></table>';
}

function tableBlock(rows, align, st) {
    let h = '<table cellspacing="0" cellpadding="5" border="1" style="border-collapse:collapse; border-color:'
        + st.rule + '; margin-top:2px; margin-bottom:10px">';
    rows.forEach((r, ri) => {
        h += "<tr>";
        r.forEach((c, ci) => {
            const tag = ri === 0 ? "th" : "td";
            const a = align[ci] || "left";
            h += "<" + tag + ' align="' + a + '" valign="top"' + (ri === 0 ? ' style="color:' + st.text + '"' : "")
                + ">" + inline(c, st) + "</" + tag + ">";
        });
        h += "</tr>";
    });
    return h + "</table>";
}

function listBlock(items, st, depth) {
    const bullets = ["•", "◦", "▪"];
    let h = '<table cellspacing="0" cellpadding="0" style="margin-top:0px; margin-bottom:' + (depth ? 0 : 8) + 'px">';
    items.forEach(it => {
        const marker = it.ordered ? esc(it.marker) : bullets[Math.min(depth, bullets.length - 1)];
        h += '<tr><td width="' + (it.ordered ? 22 : 16) + '" valign="top" style="color:' + (it.ordered ? st.muted : st.accent)
            + '">' + marker + '</td><td valign="top" style="padding-bottom:3px">' + inline(it.text, st)
            + (it.children.length ? listBlock(it.children, st, depth + 1) : "") + "</td></tr>";
    });
    return h + "</table>";
}

function blocks(lines, st) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        if (line.trim() === "") {
            i++;
            continue;
        }
        const f = line.match(FENCE);
        if (f) {
            const body = [];
            i++;
            // Until the closing fence, or the end of what has streamed so far.
            while (i < lines.length && !(lines[i].match(FENCE) && lines[i].trim()[0] === f[1][0]))
                body.push(lines[i++]);
            i++;
            out.push(codeBlock(body, st));
            continue;
        }
        const hd = line.match(HEADING);
        if (hd) {
            const big = hd[1].length <= 2;
            out.push('<p style="margin-top:' + (out.length ? 12 : 0) + 'px; margin-bottom:6px; font-size:'
                + (big ? st.h1Px : st.h3Px) + 'px; color:' + st.text + '"><b>' + inline(hd[2], st) + "</b></p>");
            i++;
            continue;
        }
        if (RULE.test(line)) {
            out.push('<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:4px; margin-bottom:10px">'
                + '<tr><td height="1" bgcolor="' + st.rule + '"></td></tr></table>');
            i++;
            continue;
        }
        if (line.indexOf("|") >= 0 && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
            const align = cells(lines[i + 1]).map(c => c.startsWith(":") && c.endsWith(":") ? "center"
                : c.endsWith(":") ? "right" : "left");
            const rows = [cells(line)];
            i += 2;
            while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim() !== "")
                rows.push(cells(lines[i++]));
            out.push(tableBlock(rows, align, st));
            continue;
        }
        if (QUOTE.test(line)) {
            const inner = [];
            while (i < lines.length && QUOTE.test(lines[i]))
                inner.push(lines[i++].match(QUOTE)[1]);
            out.push('<table cellspacing="0" cellpadding="0" style="margin-bottom:10px"><tr><td width="3" bgcolor="'
                + st.rule + '"></td><td width="10"></td><td style="color:' + st.muted + '"><i>'
                + blocks(inner, st).join("") + "</i></td></tr></table>");
            continue;
        }
        if (ITEM.test(line)) {
            const root = { indent: -1, children: [] };
            const stack = [root];
            let last = null;
            while (i < lines.length) {
                const l = lines[i];
                const m = l.match(ITEM);
                if (m) {
                    const indent = m[1].replace(/\t/g, "    ").length;
                    while (stack.length > 1 && indent <= stack[stack.length - 1].indent)
                        stack.pop();
                    if (last && indent > last.indent && stack[stack.length - 1] !== last)
                        stack.push(last);
                    last = { indent: indent, marker: m[2], ordered: /\d/.test(m[2]), text: m[3], children: [] };
                    stack[stack.length - 1].children.push(last);
                    i++;
                } else if (l.trim() === "") {
                    let j = i + 1;
                    while (j < lines.length && lines[j].trim() === "")
                        j++;
                    if (j < lines.length && ITEM.test(lines[j])) {
                        i = j;
                        continue;
                    }
                    break;
                } else if (!startsBlock(l, lines[i + 1])) {
                    last.text += " " + l.trim();   // a wrapped continuation line
                    i++;
                } else {
                    break;
                }
            }
            out.push(listBlock(root.children, st, 0));
            continue;
        }
        const para = [line.trim()];
        i++;
        while (i < lines.length && lines[i].trim() !== "" && !startsBlock(lines[i], lines[i + 1]))
            para.push(lines[i++].trim());
        out.push('<p style="margin-top:0px; margin-bottom:8px">' + inline(para.join(" "), st) + "</p>");
    }
    return out;
}

function toHtml(src, st) {
    const text = String(src || "").replace(/\r\n?/g, "\n");
    return blocks(text.split("\n"), st).join("\n");
}

function wordCount(src) {
    return String(src || "").split(/\s+/).filter(w => /[0-9A-Za-zÀ-ɏЀ-ӿ]/.test(w)).length;
}
