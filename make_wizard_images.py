"""make_wizard_images.py - run once:  python make_wizard_images.py
Creates wizard_side.bmp (164x314) and wizard_small.bmp (55x55) next to this file."""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
BG, ACCENT, WHITE = (24, 28, 36), (217, 142, 4), (255, 255, 255)


def font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def side():
    img = Image.new("RGB", (164, 314), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 6, 314), fill=ACCENT)                      # accent stripe
    d.ellipse((42, 50, 122, 130), outline=ACCENT, width=6)        # simple logo ring
    d.ellipse((66, 74, 98, 106), fill=ACCENT)
    d.text((82, 175), "CESIUM", font=font(22), fill=WHITE, anchor="mm")
    d.text((82, 200), "AI Race Engineer", font=font(11), fill=(190, 195, 205), anchor="mm")
    d.text((82, 295), "Cesium HQ", font=font(9), fill=(120, 126, 138), anchor="mm")
    img.save(os.path.join(HERE, "wizard_side.bmp"), "BMP")


def small():
    img = Image.new("RGB", (55, 55), BG)
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 47, 47), outline=ACCENT, width=4)
    d.ellipse((21, 21, 34, 34), fill=ACCENT)
    img.save(os.path.join(HERE, "wizard_small.bmp"), "BMP")


if __name__ == "__main__":
    side()
    small()
    print("Created wizard_side.bmp and wizard_small.bmp in", HERE)
