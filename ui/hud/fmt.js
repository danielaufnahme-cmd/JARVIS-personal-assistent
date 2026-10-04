.pragma library

// Formatting helpers for the HUD. Widget data comes from the daemon (§6); every reader here is lenient, so a
// missing or renamed field renders as "—" or an empty state instead of throwing.

function num(v) {
    if (v === null || v === undefined || v === "")
        return null;
    const n = Number(v);
    return isNaN(n) ? null : n;
}

function str(v) {
    return v === null || v === undefined ? "" : String(v);
}

function list(v) {
    return Array.isArray(v) ? v : [];
}

// Seconds, milliseconds or an ISO string → milliseconds.
function toMs(v) {
    if (v === null || v === undefined || v === "")
        return null;
    const n = Number(v);
    if (!isNaN(n))
        return n > 1e12 ? n : n * 1000;
    const d = Date.parse(v);
    return isNaN(d) ? null : d;
}

function pad2(n) {
    return (n < 10 ? "0" : "") + n;
}

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December"];

function hhmm(ms) {
    const d = new Date(ms);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

function sameDay(a, b) {
    const x = new Date(a), y = new Date(b);
    return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
}

// Mail-client style: "now", "12m", "09:14" (today), "Yesterday", "Mon", "12 Sep".
function when(ms, now) {
    if (ms === null)
        return "";
    const s = (now - ms) / 1000;
    if (s < 60)
        return "now";
    if (s < 3600)
        return Math.floor(s / 60) + "m";
    if (sameDay(ms, now))
        return hhmm(ms);
    if (sameDay(ms, now - 86400000))
        return "Yesterday";
    const d = new Date(ms);
    if (s < 6 * 86400)
        return DAYS[d.getDay()].slice(0, 3);
    return d.getDate() + " " + MONTHS[d.getMonth()].slice(0, 3);
}

// Headline age: "just now", "14 min", "2 h", "yesterday", "3 days".
function age(ms, now) {
    if (ms === null)
        return "";
    const s = Math.max(0, (now - ms) / 1000);
    if (s < 60)
        return "just now";
    if (s < 3600)
        return Math.floor(s / 60) + " min";
    if (s < 86400)
        return Math.floor(s / 3600) + " h";
    if (s < 2 * 86400)
        return "yesterday";
    return Math.floor(s / 86400) + " days";
}

// "in 18 min", "in 3 h 5 min", "now".
function until(ms, now) {
    const s = Math.round((ms - now) / 1000);
    if (s <= 30)
        return s < -60 ? "overdue" : "now";
    const m = Math.round(s / 60);
    if (m < 60)
        return "in " + m + " min";
    const h = Math.floor(m / 60), r = m % 60;
    if (h < 24)
        return "in " + h + " h" + (r ? " " + r + " min" : "");
    const d = Math.round(h / 24);
    return "in " + d + (d === 1 ? " day" : " days");
}

// Timer display: "8:12", "1:02:03".
function countdown(sec) {
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h > 0 ? h + ":" + pad2(m) + ":" + pad2(s) : m + ":" + pad2(s);
}

function dateLine(ms) {
    const d = new Date(ms);
    return DAYS[d.getDay()] + " · " + d.getDate() + " " + MONTHS[d.getMonth()];
}

function temp(v) {
    const n = num(v);
    return n === null ? "—" : Math.round(n) + "°";
}

function gb(mb, digits) {
    const n = num(mb);
    return n === null ? "—" : (n / 1024).toFixed(digits === undefined ? 1 : digits);
}

// Section 8's weather icon names → Nerd Font weather glyphs.
function weatherGlyph(icon, isDay) {
    switch (icon) {
    case "clear": return isDay === false ? "" : "";
    case "partly-cloudy": return isDay === false ? "" : "";
    case "cloudy": return "";
    case "fog": return "";
    case "drizzle": return "";
    case "rain": return "";
    case "showers": return "";
    case "snow": return "";
    case "thunder": return "";
    default: return "";
    }
}

function initials(name) {
    const parts = str(name).trim().split(/\s+/).filter(p => p.length > 0);
    if (parts.length === 0)
        return "?";
    const a = parts[0][0], b = parts.length > 1 ? parts[parts.length - 1][0] : "";
    return (a + b).toUpperCase();
}

// One line for the HUD: collapse whitespace, so untrusted text can't stretch a row.
function oneLine(s) {
    return str(s).replace(/\s+/g, " ").trim();
}

function isHttp(url) {
    return /^https?:\/\/[^\s]+$/i.test(str(url));
}
