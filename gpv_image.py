#!/usr/bin/env python3
"""Перетворює текстове повідомлення про ГПВ у картинку-таблицю.

Використання:
    python3 gpv_image.py message.txt out.png
    (або текст через stdin: python3 gpv_image.py - out.png)
"""
import re
import sys
from PIL import Image, ImageDraw, ImageFont

import os


def _pick(*paths):
    for p in paths:
        if os.path.exists(p):
            return p
    raise FileNotFoundError("Не знайдено шрифт: " + ", ".join(paths))


_L = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts") + "/"
_D = "/usr/share/fonts/truetype/dejavu/"
_W = "C:/Windows/Fonts/"
_M = "/System/Library/Fonts/Supplemental/"
FONT = _pick(_L + "DejaVuSans.ttf", _D + "DejaVuSans.ttf", _W + "arial.ttf", _M + "Arial.ttf")
FONT_B = _pick(_L + "DejaVuSans-Bold.ttf", _D + "DejaVuSans-Bold.ttf", _W + "arialbd.ttf", _M + "Arial Bold.ttf")
FONT_C = _pick(_L + "DejaVuSansCondensed.ttf", _D + "DejaVuSansCondensed.ttf", _W + "arialn.ttf", _W + "arial.ttf", _M + "Arial Narrow.ttf", _M + "Arial.ttf")

QUEUES = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]
BLUE = (142, 169, 219)
ORANGE = (240, 160, 90)
GRID = (150, 150, 150)
DARK = (60, 60, 60)
TEXT = (30, 30, 30)


MONTHS = {"січ": 1, "лют": 2, "берез": 3, "квіт": 4, "трав": 5, "черв": 6,
          "лип": 7, "серп": 8, "верес": 9, "жовт": 10, "листоп": 11, "груд": 12}


def find_date(text, today=None):
    """Шукає дату в різних форматах. Повертає 'ДД.ММ.РРРР' або ''."""
    import datetime as dt
    if today is None:
        try:
            from zoneinfo import ZoneInfo
            today = dt.datetime.now(ZoneInfo("Europe/Kyiv")).date()
        except Exception:
            today = dt.date.today()
    t = text.replace("\u00a0", " ")

    def ok(d, m, y):
        y = int(y) if y else today.year
        if y < 100:
            y += 2000
        try:
            return dt.date(y, int(m), int(d)).strftime("%d.%m.%Y")
        except ValueError:
            return ""

    # 23.11.2026 / 23.11.26 / 23/11/2026 / 23-11-2026 / 23.11
    for m in re.finditer(r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?!\d)(?!\s*[-–:]\s*\d)", t):
        if re.search(r"черг\w*[\s\d.,;:і–-]*$", t[max(0, m.start() - 40):m.start()], re.I):
            continue  # це номер підчерги, а не дата
        r = ok(*m.groups())
        if r:
            return r
    # 23 листопада (2026)
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s+([А-Яа-яІіЇїЄєҐґ']+)(?:\s+(\d{4}))?", t):
        word = m.group(2).lower()
        for stem, num in MONTHS.items():
            if word.startswith(stem):
                r = ok(m.group(1), num, m.group(3))
                if r:
                    return r
    # «завтра» / «сьогодні»
    low = t.lower()
    if "завтра" in low:
        return (today + dt.timedelta(days=1)).strftime("%d.%m.%Y")
    if "сьогодні" in low:
        return today.strftime("%d.%m.%Y")
    return ""


def parse(text):
    date = find_date(text)
    sched = {q: [False] * 24 for q in QUEUES}
    for line in text.splitlines():
        m = re.match(r"\s*Підчерга\s+(\d\.\d)\s*[–—-]\s*(.*)", line)
        if not m:
            continue
        q, rest = m.group(1), m.group(2)
        for a1, a2, b1, b2 in re.findall(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})", rest):
            start = int(a1) + int(a2) / 60
            end = int(b1) + int(b2) / 60
            for h in range(24):
                # година зафарбовується, якщо відключення її перекриває
                if start < h + 1 and end > h:
                    sched[q][h] = True
    return date, sched


def render(date, sched, out, note="", added=None):
    f = lambda p, s: ImageFont.truetype(p, s)
    title_f, hdr_f, cell_f, big_f, num_f, small_f = (
        f(FONT_B, 26), f(FONT_C, 14), f(FONT_B, 15), f(FONT, 22), f(FONT, 17), f(FONT, 15))

    W = None
    x0, y0 = 28, 80
    qcol, subcol, hcol, durcol = 42, 42, 38.5, 130
    hdr_h, row_h = 130, 35
    table_w = qcol + subcol + 24 * hcol + durcol
    H = y0 + hdr_h + row_h * 12 + 120
    W = int(x0 * 2 + table_w)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    title = "Графік погодинних відключень на "
    d.text((38, 30), title, font=title_f, fill=TEXT)
    tw = d.textlength(title, font=title_f)
    d.text((38 + tw, 30), date, font=title_f, fill=TEXT)
    dw = d.textlength(date, font=title_f)
    d.line((38 + tw, 60, 38 + tw + dw, 60), fill=TEXT, width=2)
    if note:
        d.text((38 + tw + dw + 18, 47), note, font=f(FONT_B, 18), fill=(200, 80, 40), anchor="lm")

    hx = x0 + qcol + subcol
    ty = y0 + hdr_h

    # зафарбовані клітинки
    for i, q in enumerate(QUEUES):
        for h in range(24):
            if sched[q][h]:
                cx = hx + h * hcol
                cy = ty + i * row_h
                is_added = bool(added and added.get(q) and added[q][h])
                d.rectangle((cx, cy, cx + hcol, cy + row_h), fill=ORANGE if is_added else BLUE)

    # шапка
    d.multiline_text((x0 + (qcol + subcol) / 2, y0 + hdr_h / 2), "Черга/\nпідчерга",
                     font=cell_f, fill=TEXT, anchor="mm", align="center", spacing=4)
    for h in range(24):
        cx = hx + h * hcol + hcol / 2
        lines = [f"{h:02d}:", "00", "–", f"{h + 1:02d}:", "00"]
        for k, s in enumerate(lines):
            d.text((cx, y0 + 22 + k * 21), s, font=hdr_f, fill=TEXT, anchor="mm")
    dx = hx + 24 * hcol
    d.multiline_text((dx + durcol / 2, y0 + hdr_h / 2), "Прогнозована\nтривалість\nвідключень за\nдобу, год",
                     font=hdr_f, fill=TEXT, anchor="mm", align="center", spacing=4)

    # рядки
    for i, q in enumerate(QUEUES):
        cy = ty + i * row_h
        d.text((x0 + qcol + subcol / 2, cy + row_h / 2), q + ".", font=cell_f, fill=TEXT, anchor="mm")
        d.text((dx + durcol / 2, cy + row_h / 2), str(sum(sched[q])), font=num_f, fill=TEXT, anchor="mm")
        if i % 2 == 0:
            d.text((x0 + qcol / 2, cy + row_h), q[0], font=big_f, fill=TEXT, anchor="mm")

    # сітка
    right = x0 + table_w
    bottom = ty + 12 * row_h
    d.rectangle((x0, y0, right, bottom), outline=GRID)
    d.line((x0, ty, right, ty), fill=DARK, width=1)
    for i in range(1, 12):
        y = ty + i * row_h
        d.line((x0 + qcol if i % 2 else x0, y, right, y), fill=DARK if i % 2 == 0 else GRID)
    for xx in [x0 + qcol, hx] + [hx + h * hcol for h in range(1, 25)]:
        top = ty if xx == x0 + qcol else y0
        d.line((xx, top, xx, bottom), fill=GRID)

    # легенда
    ly = bottom + 25
    d.rectangle((x0, ly, x0 + 28, ly + 28), outline=GRID, fill="white")
    d.text((x0 + 42, ly + 14), "Електроенергія розподіляється", font=small_f, fill=TEXT, anchor="lm")
    lx = x0 + 340
    d.rectangle((lx, ly, lx + 28, ly + 28), fill=BLUE)
    d.text((lx + 42, ly + 14), "Електроенергія відсутня", font=small_f, fill=TEXT, anchor="lm")
    if added and any(any(v) for v in added.values()):
        ox = lx + 300
        d.rectangle((ox, ly, ox + 28, ly + 28), fill=ORANGE)
        d.text((ox + 42, ly + 14), "Додаткові відключення", font=small_f, fill=TEXT, anchor="lm")
    d.text((x0 + 10, ly + 65), "Прогноз — оператор системи передачі може змінити обсяг та час обмежень",
           font=f(FONT, 13), fill=(110, 110, 110), anchor="lm")

    img.save(out, format="PNG")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    out = sys.argv[2] if len(sys.argv) > 2 else "gpv.png"
    text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    date, sched = parse(text)
    render(date, sched, out)
    print("saved", out, date)
