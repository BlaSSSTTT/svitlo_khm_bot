#!/usr/bin/env python3
"""Бот (GitHub Actions запускає його кожні 10 хв, кожен запуск працює ~9 хв):
  1. обробляє команди, які люди написали боту (/start, /queue, /now, /stop);
  2. дивиться останній графік у каналі;
  3. якщо графік новий або змінений — малює картинку й розсилає підписникам.

Стан (підписники, що вже розіслано) зберігається зашифрованим у state.bin.

Змінні середовища (GitHub Secrets):
  BOT_TOKEN    — токен бота від @BotFather (обов'язково)
  CHANNEL      — канал: назва, @назва або t.me/назва (обов'язково)
  STATE_KEY    — будь-який довгий пароль для шифрування state.bin (обов'язково)
  ALLOWED_IDS  — необов'язково: Telegram ID через кому, кому можна підписатися
  TG_API_ID, TG_API_HASH, TG_SESSION — лише для ПРИВАТНОГО каналу (див. README)
"""
import base64
import hashlib
import html
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken

from gpv_image import parse, render

TOKEN = os.environ["BOT_TOKEN"].strip()
CHANNEL = re.sub(r"^(https?://)?(t\.me/)?(s/)?@?", "", os.environ["CHANNEL"].strip()).strip("/")
API = f"https://api.telegram.org/bot{TOKEN}/"
STATE_FILE = "state.bin"
ALLOWED = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}
VALID = {f"{q}.{s}" for q in range(1, 7) for s in (1, 2)}

KYIV = ZoneInfo("Europe/Kyiv")
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "540"))   # скільки працює один запуск
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "60"))    # як часто перевіряти канал, с
SETUP_VERSION = 2  # збільш, якщо зміниш опис/команди нижче

HELP = ("Я надсилаю графіки відключень, щойно вони з'являються.\n"
        "Канал перевіряю щохвилини. Зазвичай відповідаю одразу, але іноді — через кілька хвилин.\n\n"
        "/queue 3.1 — вказати свою підчергу (покажу твої години)\n"
        "/now — останній графік\n"
        "/status — чи працює бот\n"
        "/stop — відписатися")

DESCRIPTION = ("⚡ Надсилаю графіки погодинних відключень світла картинкою, "
               "щойно вони з'являються в каналі.\n\n"
               "Канал перевіряю щохвилини. Зазвичай відповідаю одразу, "
               "але інколи (коли перезапускаюсь) — через кілька хвилин. "
               "Це нормально, я не завис.\n\n"
               "Після підписки напиши /queue і свою підчергу (напр. /queue 3.1) — "
               "буду показувати твої години.")

COMMANDS = [
    {"command": "now", "description": "Останній графік"},
    {"command": "queue", "description": "Моя підчерга, напр. /queue 3.1"},
    {"command": "status", "description": "Чи працює бот"},
    {"command": "stop", "description": "Відписатися"},
]


def now_kyiv():
    return datetime.now(KYIV)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------- стан ----------

def _fernet():
    key = hashlib.sha256(os.environ["STATE_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load_state():
    st = {"offset": 0, "subs": {}, "sent": {}, "last": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "rb") as f:
                st.update(json.loads(_fernet().decrypt(f.read())))
        except InvalidToken:
            sys.exit("Не вдалося розшифрувати state.bin — STATE_KEY змінився? "
                     "Поверни старий пароль або видали state.bin з репозиторію.")
    return st


def save_state(st):
    st["sent"] = dict(sorted(st["sent"].items(), key=lambda kv: int(kv[0]))[-30:])
    data = _fernet().encrypt(json.dumps(st, ensure_ascii=False).encode())
    with open(STATE_FILE, "wb") as f:
        f.write(data)


# ---------- канал ----------

def html_to_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def fetch_public(channel):
    """Публічний канал: читаємо веб-версію t.me/s/<канал> — без входу в акаунт."""
    r = requests.get(f"https://t.me/s/{channel}", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    posts = []
    for chunk in r.text.split('data-post="')[1:]:
        m = re.match(r"[^/]+/(\d+)\"", chunk)
        t = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        if m and t:
            posts.append((int(m.group(1)), html_to_text(t.group(1))))
    if not posts and "tgme_channel_info" not in r.text:
        raise RuntimeError("Не бачу постів. Канал приватний? Тоді потрібні TG_SESSION і т.д. (README)")
    return posts


def fetch_private(channel):
    """Приватний канал: читаємо від твого акаунта через Telethon."""
    import asyncio
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    async def go():
        async with TelegramClient(StringSession(os.environ["TG_SESSION"]),
                                  int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as c:
            ent = int(channel) if channel.lstrip("-").isdigit() else channel
            return [(m.id, m.raw_text or "") async for m in c.iter_messages(ent, limit=20)]

    return asyncio.run(go())


SCHEDULE_LINE = re.compile(r"Підчерга\s+\d\.\d\s*[–—-]\s*\d{1,2}:\d{2}")


def is_schedule(text):
    """Графік — лише якщо є хоча б один рядок «Підчерга 1.1 – 10:00…».
    Усі інші пости каналу (новини, оголошення) ігноруються."""
    return bool(SCHEDULE_LINE.search(text or ""))


def schedule_posts():
    """Усі пости з графіками, від старих до нових."""
    posts = fetch_private(CHANNEL) if os.environ.get("TG_SESSION") else fetch_public(CHANNEL)
    return sorted(p for p in posts if is_schedule(p[1]))


# ---------- бот ----------

def api(method, **kw):
    r = requests.post(API + method, timeout=60, **kw)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('error_code')} {data.get('description')}")
    return data["result"]


def format_ranges(hours):
    out, start = [], None
    for i, h in enumerate(hours):
        if start is None:
            start = h
        if i == len(hours) - 1 or hours[i + 1] != h + 1:
            out.append(f"{start:02d}:00–{h + 1:02d}:00")
            start = None
    return ", ".join(out)


def parse_post(text):
    date, sched = parse(text)
    if not date:  # дати в пості немає — беремо сьогоднішню
        date = now_kyiv().strftime("%d.%m.%Y")
    return date, sched


def update_note(text):
    """'' для звичайного графіка; 'оновлено з 11:00' / 'оновлено 23:20' / 'оновлено' для оновленого."""
    low = text.lower()
    if "оновлен" not in low and "змінен" not in low and "додатков" not in low:
        return ""
    m = re.search(r"(?:оновлен|змінен)\w*\s*(з|о|об)?\s*(\d{1,2}[:.]\d{2})", text, re.I)
    if m:
        return "оновлено " + (m.group(1) + " " if m.group(1) else "") + m.group(2).replace(".", ":")
    return "оновлено"


def build_view(st, text, edited=False):
    """Що саме показувати: для «Додаткових годин» — об'єднує з попереднім графіком на цю дату."""
    date, s = parse_post(text)
    note = update_note(text)
    base = st.get("by_date", {}).get(date)
    v = {"date": date, "note": note, "prev": None, "added": None, "no_base": False}
    if "додатков" in text.lower():
        v["kind"] = "add"
        if base:
            v["sched"] = {q: [a or b for a, b in zip(base[q], s[q])] for q in s}
            v["added"] = {q: [b and not a for a, b in zip(base[q], s[q])] for q in s}
            v["prev"] = base
        else:
            v["sched"], v["added"], v["no_base"] = s, s, True
    else:
        v["kind"] = "upd" if (note or edited) else "new"
        if not note and edited:
            v["note"] = "оновлено"
        v["sched"] = s
        v["prev"] = base if v["kind"] == "upd" else None
    return v


def hours_of(sched, queue):
    return [h for h, off in enumerate((sched or {}).get(queue, [])) if off]


def caption(v, queue):
    date, sched, note = v["date"], v["sched"], v["note"]
    if v["kind"] == "add":
        c = f"➕ Додаткові відключення на {date}" + (f" ({note})" if note and note != "оновлено" else "")
    elif v["kind"] == "upd":
        short = note.replace("оновлено", "").strip()
        c = f"🔄 Оновлено графік на {date}" + (f" ({short})" if short else "")
    else:
        c = f"⚡ Графік відключень на {date}"

    if v["kind"] == "add":
        changed = sorted(q for q in sched if any(v["added"][q]))
        if queue:
            c += f"\nТвоя підчерга {queue}: " + (format_ranges(hours_of(sched, queue)) or "без відключень")
            add_h = hours_of(v["added"], queue)
            c += ("\n➕ Додано: " + format_ranges(add_h)) if add_h else "\n✅ Для твоєї підчерги без змін"
        elif changed:
            c += "\nДодано для підчерг: " + ", ".join(changed)
        if v["no_base"]:
            c += "\n⚠️ Основного графіка на цю дату не знайшов — показую лише додаткові години"
        return c

    if queue:
        now_h = hours_of(sched, queue)
        c += f"\nТвоя підчерга {queue}: " + (format_ranges(now_h) or "без відключень")
        if v["kind"] == "upd" and v["prev"] is not None:
            old_h = hours_of(v["prev"], queue)
            c += "\n✅ Для твоєї підчерги без змін" if old_h == now_h else \
                 "\n⚠️ Було: " + (format_ranges(old_h) or "без відключень")
    elif v["kind"] == "upd" and v["prev"] is not None:
        changed = [q for q in sorted(sched) if sched[q] != v["prev"].get(q)]
        if changed:
            c += "\nЗмінилося для підчерг: " + ", ".join(changed)
    return c


def make_png(v):
    buf = io.BytesIO()
    render(v["date"], v["sched"], buf, note=v["note"], added=v["added"])
    return buf.getvalue()


def send_view(st, uid, v, png=None):
    if png is None:
        png = make_png(v)
    try:
        api("sendPhoto", data={"chat_id": uid, "caption": caption(v, st["subs"].get(str(uid), ""))},
            files={"photo": (f"gpv_{v['date']}.png", png, "image/png")})
        return True
    except RuntimeError as e:
        log("Не вдалося надіслати", uid, e)
        if "403" in str(e) or "chat not found" in str(e):
            st["subs"].pop(str(uid), None)  # заблокував бота
        return False


def send_last(st, uid):
    if st.get("view"):
        return send_view(st, uid, st["view"])
    if st.get("last"):  # старий формат стану
        return send_view(st, uid, build_view(st, st["last"]))
    reply(uid, "Графіка поки немає.")
    return False


def reply(uid, text):
    try:
        api("sendMessage", data={"chat_id": uid, "text": text})
    except RuntimeError as e:
        log("Не вдалося відповісти", uid, e)


def process_updates(st, wait=0):
    # wait > 0 — «довге очікування»: Telegram тримає запит, поки не прийде повідомлення
    updates = api("getUpdates", data={"offset": st["offset"], "timeout": wait,
                                      "allowed_updates": json.dumps(["message"])})
    for u in updates:
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if msg.get("chat", {}).get("type") != "private":
            continue
        uid = msg["chat"]["id"]
        key = str(uid)
        text = (msg.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip().rstrip(".")
        log("Команда від", uid, cmd)

        if cmd == "/start":
            if ALLOWED and uid not in ALLOWED:
                reply(uid, "Вибач, цей бот приватний.")
                continue
            st["subs"].setdefault(key, "")
            reply(uid, "✅ Підписано!\n\n" + HELP)
            if st.get("view") or st.get("last"):
                send_last(st, uid)
        elif key not in st["subs"]:
            reply(uid, "Натисни /start, щоб підписатися.")
        elif cmd == "/stop":
            st["subs"].pop(key, None)
            reply(uid, "Відписано. Щоб повернутися — /start")
        elif cmd == "/queue":
            if arg in VALID:
                st["subs"][key] = arg
                reply(uid, f"Запам'ятав: підчерга {arg}")
            elif arg in ("", "0", "-"):
                st["subs"][key] = ""
                reply(uid, "Підчергу прибрано. Приклад: /queue 3.1")
            else:
                reply(uid, "Напиши так: /queue 3.1 (від 1.1 до 6.2)")
        elif cmd == "/status":
            lines = ["🟢 Працюю. Перевіряю канал щохвилини.",
                     f"Зараз: {now_kyiv():%H:%M}"]
            if st.get("last_date"):
                lines.append(f"Останній графік: на {st['last_date']} (отримано {st.get('last_got', '?')})")
            sub = st["subs"].get(key)
            lines.append(f"Твоя підчерга: {sub}" if sub else "Підчергу не вказано — /queue 3.1")
            reply(uid, "\n".join(lines))
        elif cmd == "/now":
            send_last(st, uid)
        else:
            reply(uid, HELP)


STATE_VERSION = 2


def rebuild_from_channel(st, posts):
    """Одноразово після оновлення: перечитати всі графіки з каналу по порядку
    і правильно скласти основні + додаткові години. Нікому нічого не надсилає."""
    st["by_date"] = {}
    for mid, text in posts:
        date, s = parse_post(text)
        if not any(any(h) for h in s.values()):
            continue
        v = build_view(st, text)
        st["by_date"][date] = v["sched"]
        st["view"], st["last"] = v, text
        st["sent"][str(mid)] = text
    st["state_v"] = STATE_VERSION
    log("Стан перебудовано з", len(posts), "постів каналу")


def check_channel(st):
    posts = schedule_posts()
    if not posts:
        return
    if st.get("state_v", 0) < STATE_VERSION:
        rebuild_from_channel(st, posts)
        return
    # нові або відредаговані пости, від старих до нових
    new = [(mid, t) for mid, t in posts if st["sent"].get(str(mid)) != t]
    if not st["sent"]:
        new = new[-1:]  # перший запуск — лише останній графік
    if not new:
        return
    by_date = st.setdefault("by_date", {})
    for k, (mid, text) in enumerate(new):
        date, s = parse_post(text)
        if not any(any(h) for h in s.values()):
            log("Пост", mid, "схожий на графік, але годин не знайдено")
            st["sent"][str(mid)] = text
            continue
        v = build_view(st, text, edited=str(mid) in st["sent"])
        by_date[date] = v["sched"]
        st["sent"][str(mid)] = text
        st["view"] = v
        st["last"] = text
        if k < len(new) - 1:
            log("Пропущений пост", mid, "враховано без розсилки")
            continue
        png = make_png(v)
        n = 0
        for uid in list(st["subs"]):
            n += send_view(st, int(uid), v, png)
            time.sleep(0.1)
        st["last_date"] = date
        st["last_got"] = now_kyiv().strftime("%d.%m %H:%M")
        log(f"Розіслано графік на {date} ({v['kind']} {v['note']}): {n} отримувачів")
    st["by_date"] = dict(list(by_date.items())[-5:])


def setup_profile(st):
    """Один раз: опис бота (видно до натискання Start) і меню команд."""
    if st.get("setup") == SETUP_VERSION:
        return
    try:
        api("setMyDescription", data={"description": DESCRIPTION})
        api("setMyCommands", data={"commands": json.dumps(COMMANDS, ensure_ascii=False)})
        st["setup"] = SETUP_VERSION
        log("Опис і команди бота оновлено")
    except RuntimeError as e:
        log("Не вдалося оновити опис:", e)


_status = {"ok": None, "at": 0}


def update_status(ok):
    """Статус у профілі бота — видно, коли він востаннє перевіряв канал.
    Оновлюємо раз на 5 хв або коли змінився стан (щоб не впертися в ліміти Telegram)."""
    if _status["ok"] == ok and time.time() - _status["at"] < 300:
        return
    _status.update(ok=ok, at=time.time())
    t = now_kyiv().strftime("%H:%M")
    text = (f"🟢 Працює · канал перевіряю щохвилини · остання перевірка о {t}" if ok
            else f"🟡 Не вдалося прочитати канал о {t} — пробую ще")
    try:
        api("setMyShortDescription", data={"short_description": text})
    except RuntimeError as e:
        log("Не вдалося оновити статус:", e)


def main():
    st = load_state()
    before = json.dumps(st, sort_keys=True, ensure_ascii=False)
    deadline = time.time() + RUN_SECONDS
    last_check = 0
    try:
        setup_profile(st)
        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            if time.time() - last_check >= CHECK_EVERY:
                last_check = time.time()
                ok = False
                try:
                    check_channel(st)
                    ok = True
                except Exception as e:
                    log("Помилка при перевірці каналу:", e)
                update_status(ok)
            try:
                process_updates(st, wait=int(max(0, min(25, left, CHECK_EVERY - (time.time() - last_check)))))
            except Exception as e:
                log("Помилка при читанні повідомлень:", e)
                time.sleep(5)
    finally:
        if json.dumps(st, sort_keys=True, ensure_ascii=False) != before:
            save_state(st)
            log("Стан збережено")


if __name__ == "__main__":
    main()
