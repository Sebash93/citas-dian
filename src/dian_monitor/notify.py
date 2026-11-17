"""Alert channels and message texts.

Telegram is used when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set; otherwise
alerts are only printed to the log.
"""
import os
import time
from dataclasses import dataclass

import requests


@dataclass
class Message:
    text: str
    urgent: bool = False
    button: tuple = None  # (label, url)
    private: bool = False  # button URL is a secret (live-browser link): keep it out of logs


class ConsoleNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        bar = "!" * 60 if message.urgent else "-" * 60
        print(f"{bar}\n{message.text}", flush=True)
        if message.button:
            url = "(link sent only to Telegram)" if message.private else message.button[1]
            print(f"[{message.button[0]}] {url}", flush=True)
        print(bar, flush=True)
        return True

    def send_photo(self, path, caption=""):
        print(f"[photo] {path} {caption}", flush=True)
        return True


class TelegramNotifier:
    """Sends each message to every chat id; also echoes it to the log."""

    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token, chat_ids, session=None):
        self.token = token
        self.chat_ids = chat_ids
        self.s = session or requests.Session()
        self.console = ConsoleNotifier()

    def call(self, method, payload):
        resp = self.s.post(self.API.format(token=self.token, method=method),
                           json=payload, timeout=20)
        body = resp.json()
        if not body.get("ok"):
            # Never echo the URL: it contains the token.
            raise RuntimeError(f"Telegram {method}: {body.get('description', resp.status_code)}")
        return body["result"]

    def send(self, message):
        self.console.send(message)
        payload = {"text": message.text, "disable_web_page_preview": True,
                   "disable_notification": not message.urgent}
        if message.button:
            label, url = message.button
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": url}]]}
        ok = True
        for chat_id in self.chat_ids:
            for attempt in range(1, 4):
                try:
                    self.call("sendMessage", {**payload, "chat_id": chat_id})
                    break
                except (requests.RequestException, ValueError, RuntimeError) as e:
                    print(f"Telegram send to {chat_id} failed (attempt {attempt}): {e}", flush=True)
                    if attempt == 3:
                        ok = False
                    else:
                        time.sleep(2 * attempt)
        return ok

    def send_photo(self, path, caption=""):
        """Screenshots go only to the chats: they may show personal data."""
        ok = True
        for chat_id in self.chat_ids:
            try:
                with open(path, "rb") as f:
                    resp = self.s.post(self.API.format(token=self.token, method="sendPhoto"),
                                       data={"chat_id": chat_id, "caption": caption[:1000]},
                                       files={"photo": f}, timeout=60)
                if not resp.json().get("ok"):
                    raise RuntimeError(f"Telegram sendPhoto: {resp.status_code}")
            except (OSError, requests.RequestException, ValueError, RuntimeError) as e:
                print(f"Telegram photo to {chat_id} failed: {e}", flush=True)
                ok = False
        return ok


def build_notifier(chat_ids=None):
    """Telegram to `chat_ids` (default: TELEGRAM_CHAT_ID, comma-separated), else the log."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not chat_ids:
        chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",")
                    if c.strip()]
    if token and chat_ids:
        return TelegramNotifier(token, chat_ids)
    print("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID): log-only alerts.",
          flush=True)
    return ConsoleNotifier()


def find_chat_ids(token):
    """Return (bot info, webhook url, [(chat id, description)]) for chats that messaged the bot."""
    notifier = TelegramNotifier(token, [])
    bot = notifier.call("getMe", {})
    webhook = notifier.call("getWebhookInfo", {}).get("url")
    chats = {}
    for update in notifier.call("getUpdates", {}):
        msg = update.get("message") or update.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            name = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")]))
            chats[chat["id"]] = f"{chat.get('type')}: {name}"
    return bot, webhook, list(chats.items())


def test_message():
    return Message("✅ Bot DIAN conectado. Te avisaré aquí apenas haya cita.",
                   urgent=True,
                   button=("Abrir agendamiento", "https://agendamiento.dian.gov.co/?recurso=CitasDIAN"))


# --- texts ------------------------------------------------------------------

def opening(settings, city, matches, when, first=True, cases=()):
    """`city` is the display name (e.g. "Medellín"); `cases` the ids waiting for it."""
    head = (f"🚨 HAY CITA EN LA DIAN — {city.upper()}" if first
            else f"⏰ Sigue abierta la cita en {city}")
    lines = "\n".join(f"• {m}" for m in matches)
    waiting = f"\nCasos: {', '.join(cases)}\n" if cases else ""
    return Message(
        text=(f"{head}\n\n{settings.label} / {city}\n\n{lines}\n{waiting}\n"
              "Ruta: Agendar cita → Persona Natural → Videoatención → Devoluciones\n"
              f"Visto: {when:%a %d %b %H:%M:%S} (Bogotá)"),
        urgent=True,
        button=("Agendar ahora", settings.booking_url),
    )


def broken(error, failures):
    return Message(f"⚠️ Bot DIAN: {failures} revisiones seguidas fallaron.\n"
                   f"Último error: {error}\nMientras tanto NO estoy vigilando bien.")


def blocked(blocks, until):
    return Message(f"⛔ Bot DIAN: la DIAN rechazó las consultas {blocks} veces seguidas. "
                   f"Pauso hasta {until:%H:%M} (Bogotá) y bajo el ritmo.")


def booker_unavailable(error):
    return Message(f"⚠️ No pude arrancar el bot que agenda ({error}). "
                   "Si hay una alerta de cita, agenda a mano con su botón.", urgent=True)


def booker_skipped_booked(city, artifacts):
    return Message(f"ℹ️ Abrió {city}, pero ya hay una cita registrada para cada caso de {city}, "
                   "así que no aviso ni arranco el bot. Si falta alguna, borra su registro "
                   f"en GitHub ({', '.join(artifacts)}) para que vuelva a agendar.", urgent=True)


def recovered():
    return Message("✅ Bot DIAN: las revisiones volvieron a funcionar.")


def heartbeat(prev_day, open_cities, watched):
    stats = (f"Ayer ({prev_day['date']}): {prev_day['checks']} revisiones, "
             f"{prev_day['errors']} errores." if prev_day else "Primer día de operación.")
    status = (f"Con cupo abierto ahora: {', '.join(open_cities)}." if open_cities
              else "Sin cupo por ahora.")
    return Message(f"🟢 Bot DIAN sigue vivo. Vigilo: {', '.join(watched) or 'nada'}. "
                   f"{stats} {status}")


def cases_problem(error, have_cases=False):
    tail = ("Sigo vigilando los últimos casos que conozco y vuelvo a intentar."
            if have_cases else "No tengo casos para vigilar hasta que lo pueda leer.")
    return Message(f"⚠️ Bot DIAN: no pude leer los casos ({error}). {tail}", urgent=True)


def no_cases_source():
    return Message("⚠️ Bot DIAN: Airtable no está configurado (AIRTABLE_TOKEN, AIRTABLE_BASE_ID), "
                   "así que no tengo casos y no vigilo nada.", urgent=True)


def cases_recovered():
    return Message("✅ Bot DIAN: volví a leer los casos.")


def cases_skipped(problems):
    lines = "\n".join(f"• {p}" for p in problems[:20])
    return Message("⚠️ Bot DIAN: estos casos tienen datos incompletos y no los vigilo hasta que "
                   f"los corrijas en Airtable:\n{lines}")
