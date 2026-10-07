"""An arc reactor in the terminal, written live by JARVIS."""
import math
import shutil
import sys
import time

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
cols, rows = shutil.get_terminal_size()
rows -= 2                                    # room for the last line
w, h = cols, rows * 2                        # two pixels per character: ▀
cx, cy = (w - 1) / 2, (h - 1) / 2
grid = [(math.hypot(x - cx, y - cy), math.atan2(y - cy, x - cx)) for y in range(h) for x in range(w)]


def colour(r, a, t):
    ring = max(0.0, 1 - abs(r - h * 0.34 - 1.5 * math.sin(6 * a + 3 * t)) / 3)
    coils = max(0.0, 1 - abs(r - h * 0.2) / 2.5) * (math.sin(10 * a - 4 * t) > 0.2)
    core = math.exp(-(r / (h * 0.09)) ** 2)
    glow = 0.25 * (0.5 + 0.5 * math.sin(0.45 * r - 5 * t)) * math.exp(-r / h)
    v = min(1.0, ring + 0.8 * coils + core + glow)
    return f"{int(30 * v + 200 * core)};{int(min(255, 190 * v + 60 * core))};{int(40 + 215 * v)}"


def frame(t):
    out = ["\x1b[H"]
    for row in range(rows):
        top, bottom = grid[2 * row * w:(2 * row + 1) * w], grid[(2 * row + 1) * w:(2 * row + 2) * w]
        out += [f"\x1b[38;2;{colour(*p, t)};48;2;{colour(*q, t)}m▀" for p, q in zip(top, bottom)]
        out.append("\x1b[0m\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


print("\x1b[?25l\x1b[2J", end="")
start = time.time()
try:
    while (t := time.time() - start) < seconds:
        frame(t)
        time.sleep(1 / 24)
finally:
    print("\x1b[0m\x1b[?25h", end="")
print("Written live by JARVIS.")
