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


def build_notifier():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
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
    return Message("✅ Bot DIAN conectado. Te avisaré aquí apenas haya cita en Medellín.",
                   urgent=True,
                   button=("Abrir agendamiento", "https://agendamiento.dian.gov.co/?recurso=CitasDIAN"))


# --- texts ------------------------------------------------------------------

def opening(settings, matches, when, first=True):
    head = "🚨 HAY CITA EN LA DIAN — MEDELLÍN" if first else "⏰ Sigue abierta la cita en Medellín"
    lines = "\n".join(f"• {m}" for m in matches)
    return Message(
        text=(f"{head}\n\n{settings.label}\n\n{lines}\n\n"
              "Ruta: Agendar cita → Persona Natural → Videoatención → Devoluciones\n"
              f"Visto: {when:%a %d %b %H:%M:%S} (Bogotá)"),
        urgent=True,
        button=("Agendar ahora", settings.booking_url),
    )


def possible(settings, seconds, when, timed_out=False):
    how = (f"no respondió en {seconds:.0f} s" if timed_out
           else f"está respondiendo muy lento ({seconds:.0f} s)")
    return Message(
        text=(f"🟡 POSIBLE CITA EN LA DIAN\n\nLa DIAN {how}. Suele pasar cuando abren cupos, "
              "pero todavía no veo Medellín en la lista.\n\nRevisa ahora:\n"
              "Agendar cita → Persona Natural → Videoatención → Devoluciones → Medellín\n"
              f"Visto: {when:%a %d %b %H:%M:%S} (Bogotá)"),
        urgent=True,
        button=("Revisar agendamiento", settings.booking_url),
    )


def broken(error, failures):
    return Message(f"⚠️ Bot DIAN: {failures} revisiones seguidas fallaron.\n"
                   f"Último error: {error}\nMientras tanto NO estoy vigilando bien.")


def blocked(blocks, until):
    return Message(f"⛔ Bot DIAN: la DIAN rechazó las consultas {blocks} veces seguidas. "
                   f"Pauso hasta {until:%H:%M} (Bogotá) y bajo el ritmo.")


def booker_unavailable(error):
    return Message(f"⚠️ No pude arrancar el bot que agenda ({error}). "
                   "Agenda a mano con el botón de la alerta.", urgent=True)


def recovered():
    return Message("✅ Bot DIAN: las revisiones volvieron a funcionar.")


def heartbeat(prev_day, is_open):
    stats = (f"Ayer ({prev_day['date']}): {prev_day['checks']} revisiones, "
             f"{prev_day['errors']} errores." if prev_day else "Primer día de operación.")
    status = "Medellín TIENE cupo abierto ahora." if is_open else "Medellín sin cupo por ahora."
    return Message(f"🟢 Bot DIAN sigue vivo. {stats} {status}")
