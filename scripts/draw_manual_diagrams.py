#!/usr/bin/env python3
"""Draw labelled owner-manual diagrams. Requires Pillow and a local TrueType font.

These are diagrams, not simulated application screenshots. No hardware is used.
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
BG, PANEL, TEXT, MUTED = "#111923", "#203044", "#f3f7fc", "#bbccde"
BLUE, ORANGE, GREEN = "#78c6ff", "#ffc478", "#86dfaf"


class Diagram:
    def __init__(self, height, title, font):
        self.image = Image.new("RGB", (1280, height), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.font = font
        self.text((48, 28), "SAT-IP RECEIVER · SETUP DIAGRAM", 17, BLUE)
        self.text((48, 65), title, 34)

    def text(self, position, text, size=24, color=TEXT):
        self.draw.text(position, text, fill=color, font=ImageFont.truetype(str(self.font), size))

    def box(self, bounds, title, lines, color=BLUE):
        x, y, right, bottom = bounds
        self.draw.rounded_rectangle(bounds, radius=14, fill=PANEL, outline=color, width=2)
        self.text((x + 20, y + 17), title, 25, color)
        for index, line in enumerate(lines):
            self.text((x + 20, y + 63 + index * 33), line, 21)

    def arrow(self, start, end, color=BLUE):
        self.draw.line((start, end), fill=color, width=4)
        x, y = end
        sx, sy = start
        if abs(x - sx) >= abs(y - sy):
            sign = 1 if x >= sx else -1
            self.draw.polygon([(x, y), (x - sign * 12, y - 7), (x - sign * 12, y + 7)], fill=color)
        else:
            sign = 1 if y >= sy else -1
            self.draw.polygon([(x, y), (x - 7, y - sign * 12), (x + 7, y - sign * 12)], fill=color)

    def save(self, output, name):
        self.image.save(output / ("manual-" + name + ".png"), optimize=True)


def render(output, font):
    output.mkdir(parents=True, exist_ok=True)
    d = Diagram(640, "Choose one television source", font)
    d.box((48, 145, 405, 325), "SAT-IP network tuner", ["Satellite cable to tuner", "Select a tuner on the Pi", "Scan Astra frequencies"], BLUE)
    d.box((48, 365, 405, 545), "Enigma2 TV box", ["Channels already tuned", "OpenWebif + raw streaming", "Import selected bouquets"], GREEN)
    d.box((495, 265, 790, 440), "Home router / LAN", ["Connect source and Pi", "to the same network"])
    d.arrow((405, 235), (495, 285), BLUE)
    d.arrow((405, 445), (495, 420), GREEN)
    d.box((875, 170, 1230, 325), "Raspberry Pi 4", ["Receives TV over LAN", "Sends picture + sound"])
    d.arrow((790, 315), (875, 260))
    d.box((875, 425, 1230, 545), "Your television", ["Select the HDMI input"])
    d.arrow((1050, 325), (1050, 425))
    d.text((1080, 360), "HDMI", 22, BLUE)
    d.text((48, 578), "The Pi has no tuner of its own. You need SAT-IP OR Enigma2, not both.", 24)
    d.save(output, "sources")

    d = Diagram(630, "1 · Flash the appliance image", font)
    d.box((48, 165, 395, 340), "Choose the image", ["Use custom", "Select sdcard.img", "Not Raspberry Pi OS"])
    d.box((465, 165, 810, 340), "Choose storage", ["Select the microSD card", "Check name + capacity", "All card data is erased"], ORANGE)
    d.box((880, 165, 1230, 340), "Write + verify", ["Write / Next + confirm", "Wait for verification", "Safely eject the card"], GREEN)
    d.arrow((395, 250), (465, 250))
    d.arrow((810, 250), (880, 250))
    d.text((48, 395), "Use Raspberry Pi Imager or another raw SD-image writer.", 27)
    d.text((48, 445), "Extract sdcard.img.zip first. Skip Raspberry Pi OS customisation.", 23)
    d.text((48, 495), "Set Wi-Fi and device preferences on the receiver after flashing.", 23)
    d.text((48, 560), "Checklist diagram — application button wording varies by version.", 19, MUTED)
    d.save(output, "flash")

    d = Diagram(740, "2 · Connect before switching on", font)
    d.box((480, 260, 815, 475), "Raspberry Pi 4", ["Flashed microSD card", "HDMI to television", "Then connect USB-C power"])
    d.box((48, 155, 395, 315), "A · Ethernet", ["Cable from home router", "Automatic network address"], BLUE)
    d.box((48, 385, 395, 545), "B · Wi-Fi", ["USB keyboard to the Pi", "Enter Wi-Fi details on TV"], GREEN)
    d.arrow((395, 265), (480, 300), BLUE)
    d.arrow((395, 430), (480, 420), GREEN)
    d.box((895, 270, 1230, 450), "Television", ["Choose connected HDMI", "Read startup messages", "Then QR code / address"])
    d.arrow((815, 350), (895, 350))
    d.text((840, 315), "HDMI", 17, BLUE)
    d.text((48, 598), "Choose Ethernet OR keyboard-assisted Wi-Fi for the first connection.", 25)
    d.text((48, 645), "The phone opens setup only AFTER the Pi is connected to your home network.", 22)
    d.save(output, "connections")

    d = Diagram(1010, "Three-pin infrared receiver · 3.3 V only", font)
    d.text((48, 125), "Disconnect all Pi power. Verify the receiver's own datasheet pinout.", 24, ORANGE)
    d.box((48, 185, 545, 545), "Pi 4 · 40-pin GPIO header", ["Physical pin 1  —  3.3 V", "", "Physical pin 6  —  Ground", "", "Physical pin 16 —  GPIO23"])
    d.box((835, 185, 1230, 545), "38 kHz IR receiver", ["VCC / VS  (power)", "", "GND  (ground)", "", "OUT / S  (signal)"], GREEN)
    d.arrow((545, 266), (835, 266), ORANGE)
    d.arrow((545, 332), (835, 332), MUTED)
    d.arrow((835, 398), (545, 398), GREEN)
    d.text((575, 227), "3.3 V supply", 20, ORANGE)
    d.text((575, 293), "Common ground", 20, MUTED)
    d.text((575, 427), "Signal to GPIO23", 20, GREEN)
    d.text((48, 580), "Header pin-number locator (first 8 rows; not a board-view drawing)", 23)
    d.draw.rounded_rectangle((58, 625, 255, 920), radius=8, outline=BLUE, width=2)
    for row in range(8):
        y = 650 + row * 33
        for col in range(2):
            pin = row * 2 + col + 1
            color = ORANGE if pin == 1 else GREEN if pin == 16 else TEXT if pin == 6 else MUTED
            d.text((82 + col * 90, y), str(pin), 21, color)
            if pin in (1, 6, 16):
                d.draw.rectangle((74 + col * 90, y - 3, 130 + col * 90, y + 27), outline=color, width=2)
    d.text((58, 936), "Rows continue to pins 39 / 40", 17, MUTED)
    d.text((310, 650), "Locate physical pin 1 on your board before counting.", 24)
    d.text((310, 700), "Physical pin 16 = GPIO23. It is NOT GPIO16.", 24, GREEN)
    d.text((310, 750), "Never use 5 V pins 2 or 4 for this example.", 24, ORANGE)
    d.text((310, 800), "Receiver terminal order varies — labels above are logical.", 22)
    d.text((310, 846), "Use any supply filtering specified by the part manufacturer.", 21)
    d.text((310, 890), "Do not connect another output alongside an Argon IR sensor.", 21)
    d.save(output, "ir-wiring")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "doc/images")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    candidates = [Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    font = args.font or next((path for path in candidates if path.is_file()), None)
    if font is None or not font.is_file():
        parser.error("Supply a local TrueType font with --font")
    render(args.output, font)


if __name__ == "__main__":
    main()
