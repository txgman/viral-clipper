"""Draws the app icon (gradient rounded square with a play triangle) to packaging/icon.ico."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
grad = Image.new("RGBA", (S, S))
for x in range(S):
    for y in range(S):
        t = (x + y) / (2 * S)
        grad.putpixel((x, y), (255, int(61 + (154 - 61) * t), int(110 + (61 - 110) * t), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((8, 8, S - 8, S - 8), radius=58, fill=255)
img.paste(grad, (0, 0), mask)
ImageDraw.Draw(img).polygon([(96, 72), (96, 184), (188, 128)], fill=(255, 255, 255, 255))
out = Path(__file__).with_name("icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"wrote {out}")
