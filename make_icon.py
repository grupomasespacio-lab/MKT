# Generates icon.icns-ready PNGs (run on macOS: python make_icon.py out_dir)
import sys, os
from PIL import Image, ImageDraw
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((60, 60, S - 60, S - 60), 220, fill=(24, 29, 37, 255))
d.rounded_rectangle((200, 300, S - 200, S - 300), 70, outline=(232, 145, 58, 255), width=34)
d.rounded_rectangle((270, 370, 520, S - 370), 28, fill=(232, 145, 58, 255))
for sz in (16, 32, 64, 128, 256, 512, 1024):
    im.resize((sz, sz), Image.LANCZOS).save(os.path.join(out, f"icon_{sz}.png"))
