#!/usr/bin/env python3
"""A made-up screen crop (an error dialog), turned into the chip's thumbnail by the daemon's own code
(jarvis.integrations.attachments.thumbnail_b64). Prints the base64 PNG. For dev/hud_harness/render_dock.sh."""
from PIL import Image, ImageDraw

from jarvis.integrations.attachments import thumbnail_b64

img = Image.new("RGB", (640, 380), (36, 38, 44))
d = ImageDraw.Draw(img)
d.rectangle((0, 0, 640, 44), fill=(52, 55, 64))
d.ellipse((18, 14, 34, 30), fill=(222, 88, 76))
d.text((50, 16), "Traceback (most recent call last)", fill=(235, 235, 235))
for i, line in enumerate(["File \"app.py\", line 12, in handler", "    user = session[\"user\"]",
                          "KeyError: 'user'"]):
    d.text((24, 80 + i * 34), line, fill=(222, 110, 96) if i == 2 else (200, 205, 210))
d.rectangle((470, 310, 616, 350), fill=(88, 120, 200))
d.text((520, 323), "OK", fill=(255, 255, 255))
print(thumbnail_b64(img))
