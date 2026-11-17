"""One booking attempt: browser up, wizard to the captcha, human solves it, wizard to the end."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime

from dian_monitor import cadence
from dian_monitor.events import EventLog
from dian_monitor.notify import Message

from .remote import LocalDisplay, RemoteDisplay
from .wizard import SlotUnavailable, Wizard, is_unresponsive, mentions_city

WIDTH, HEIGHT = 412, 900  # phone-sized, so the noVNC view needs no zooming
CDP_PORT = 9222
STEP_RE = re.compile(r'DetalleAdicional"?\s*:\s*"?([A-Za-z]+)')
CHROMES = ("google-chrome", "google-chrome-stable", "chromium",
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

BOOKED = "BOOKED"
DRY_RUN_OK = "DRY_RUN_OK"
NOT_OPEN = "NOT_OPEN"
CAPTCHA_TIMEOUT = "CAPTCHA_TIMEOUT"
SLOT_GONE = "SLOT_GONE"  # the service was offered, but the dates or the slot went before we got them
UNRESPONSIVE = "UNRESPONSIVE"  # DIAN's page stopped answering (it slows down when slots open)
FAILED = "FAILED"
# Outcomes that are part of the race for a slot, not bugs: the process exits 0 for these, so
# a GitHub run only shows as failed when the booker itself broke (FAILED).
EXPECTED = (BOOKED, DRY_RUN_OK, NOT_OPEN, SLOT_GONE, CAPTCHA_TIMEOUT, UNRESPONSIVE)
# Only from the captcha on is there something for the human to finish by hand; earlier
# failures end at once so the next opening can start a new booker without waiting.
HOLD_STAGES = ("captcha", "datos", "confirmar")


def log(msg):
    print(f"[{cadence.now_bogota():%Y-%m-%d %H:%M:%S}] booker: {msg}", flush=True)


def find_chrome():
    for c in (os.environ.get("BOOKER_CHROME"),) + CHROMES:
        if c and (shutil.which(c) or os.path.exists(c)):
            return shutil.which(c) or c
    raise RuntimeError("Google Chrome not found (set BOOKER_CHROME)")


def launch_chrome(env):
    """A normal (not automation-flagged) Chrome window; Playwright attaches over CDP."""
    profile = tempfile.mkdtemp(prefix="booker-chrome-")
    args = [find_chrome(), f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--lang=es-CO",
            f"--window-size={WIDTH},{HEIGHT}", "--window-position=0,0", "--app=about:blank"]
    if os.environ.get("BOOKER_HEADLESS") == "1":  # wizard checks only: nobody can solve a captcha
        args.append("--headless=new")
    proc = subprocess.Popen(args, env={**os.environ, **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2)
            return proc
        except OSError:
            time.sleep(0.3)
    proc.kill()
    raise RuntimeError("Chrome did not open its debugging port")


# --- messages (Spanish, for the Telegram chats) -------------------------------

def msg_get_ready(url, who):
    return Message(f"🟠 PREPÁRATE: el bot está entrando a la DIAN por una cita en {who}.\n\n"
                   "Abre el navegador ya; cuando llegue al captcha te aviso para que lo toques.",
                   urgent=True, button=("Abrir navegador", url), private=True)


def msg_tap_captcha(url, chosen, who):
    lines = "\n".join(f"{k}: {v}" for k, v in chosen.items() if v)
    return Message(f"🔴 TOCA EL CAPTCHA AHORA (\"No soy un robot\") — {who}\n\n{lines}\n\n"
                   "Después de tocarlo no hagas nada más: el bot termina solo.",
                   urgent=True, button=("Abrir navegador", url), private=True)


def msg_booked(chosen, who):
    lines = "\n".join(f"{k}: {v}" for k, v in chosen.items() if v)
    return Message(f"✅ CITA AGENDADA EN LA DIAN — {who}\n\n{lines}\n\n"
                   "Revisa el correo del caso para la confirmación.",
                   urgent=True)


def msg_dry_run(chosen, who):
    lines = "\n".join(f"{k}: {v}" for k, v in chosen.items() if v)
    return Message(f"🧪 Prueba completa ({who}): llegué al botón Agendar sin tocarlo.\n\n{lines}")


def msg_failed(stage, error, url, hold_minutes, who):
    text = f"⚠️ El bot no pudo terminar — {who} ({stage}): {str(error)[:300]}"
    if url:
        text += f"\n\nEl navegador queda abierto {hold_minutes} min por si quieres seguir tú."
        return Message(text, urgent=True, button=("Abrir navegador", url), private=True)
    return Message(text, urgent=True)


def msg_not_open(minutes, who):
    return Message(f"ℹ️ Bot DIAN: {who} no apareció en {minutes} min; cierro el navegador.")


def msg_unresponsive(stage, minutes_held, who):
    tail = (f"Dejo el navegador abierto {minutes_held} min por si quieres seguir a mano."
            if minutes_held else "Cierro el navegador; el próximo aviso lo vuelve a intentar.")
    return Message(f"⏳ La página de la DIAN no respondió — {who} ({stage}). {tail}")


# --- run ----------------------------------------------------------------------

class Booker:
    def __init__(self, settings, profile, notifier, trigger="manual", dry_run=False, local=False,
                 wait_minutes=12, captcha_minutes=10, hold_minutes=10, shots_dir="booker",
                 service_hint="", *, case, load_profile=None, on_booked=None):
        """`settings.city` and `settings.booking_file` must already be the case's.
        `profile` may be None with `load_profile` (called once, when the person is needed:
        at once for `found`, otherwise when the city appears). `on_booked(chosen)` records a
        real booking elsewhere too (Airtable)."""
        self.settings = settings
        self.case = case
        self.who = self.case.who
        self.profile = profile
        self.load_profile = load_profile
        self.on_booked = on_booked
        self.notifier = notifier
        self.trigger = trigger
        self.dry_run = dry_run
        self.local = local
        self.wait_minutes = wait_minutes
        self.captcha_minutes = captcha_minutes
        self.hold_minutes = hold_minutes
        self.shots_dir = shots_dir
        self.service_hint = service_hint
        self.url = None
        self.stage = "starting"
        self.events = EventLog(settings.events_file, cadence.now_bogota, "booker")
        self.error = None
        self.captcha_wait = None
        self.link_thread = None
        self.link_error = None

    def run(self):
        started = time.monotonic()
        self.events.emit("booker_start", trigger=self.trigger, dry_run=self.dry_run,
                         local=self.local, case=self.case.id, city=self.case.city)
        try:
            result = self._run(started)
        except Exception as e:  # noqa: BLE001 - e.g. the display or Chrome did not start
            log(f"Could not start: {type(e).__name__}")
            self.error = e
            result = FAILED
        self.events.emit("booker_end", trigger=self.trigger, result=result, stage=self.stage,
                         case=self.case.id, city=self.case.city,
                         seconds=round(time.monotonic() - started, 1),
                         captcha_wait=self.captcha_wait, link_sent=bool(self.url),
                         error=self._error_text())
        return result

    def _run(self, started):
        from playwright.sync_api import sync_playwright  # only the booker needs Playwright

        display = LocalDisplay() if self.local else RemoteDisplay(WIDTH, HEIGHT, log)
        with display:
            chrome = launch_chrome(display.env)
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                    page = browser.contexts[0].pages[0]
                    self._mark("browser_ready", started)
                    self._time_dian_steps(page)
                    return self._attempt(page, display, started)
            finally:
                chrome.terminate()

    def _mark(self, stage, started):
        seconds = round(time.monotonic() - started, 1)
        log(f"{stage} after {seconds:.0f} s.")
        self.events.emit("booker_stage", stage=stage, seconds=seconds)

    def _time_dian_steps(self, page):
        """Record how long each wizard call to DIAN takes (the bottleneck when slots open)."""
        pending = {}

        def on_request(req):
            if "ValidadorValidar" in req.url:
                m = STEP_RE.search(req.post_data or "")
                pending[req] = (m.group(1) if m else "?", time.monotonic())

        def on_done(req, failed=False):
            if req in pending:
                step, t0 = pending.pop(req)
                self.events.emit("dian_step", step=step, failed=failed,
                                 seconds=round(time.monotonic() - t0, 2))

        page.on("request", on_request)
        page.on("requestfinished", on_done)
        page.on("requestfailed", lambda req: on_done(req, failed=True))

    def _error_text(self):
        if self.error is None:
            return None
        text = f"{type(self.error).__name__}: {self.error}"
        if self.profile is None:  # no personal data loaded yet: only the link can leak
            return re.sub(r"https://[a-z0-9-]+\.trycloudflare\.com\S*", "<link>", text)[:400]
        return self.profile.scrub(text)[:400]

    def _ensure_profile(self):
        if self.profile is None:
            stage, self.stage = self.stage, "case_data"
            self.profile = self.load_profile()
            self.stage = stage

    def _attempt(self, page, display, started):
        wizard = Wizard(page, self.settings, log, self.service_hint)
        try:
            if self.trigger == "found":  # confirmed opening: get the human moving right away
                self._share_link_soon(display)
                self._ensure_profile()  # and find a bad record before the slot matters
            self.stage = "PasoUno"
            deadline = started + self.wait_minutes * 60
            service = wizard.wait_for_service(deadline)
            if not service:
                if wizard.unresponsive:
                    log(f"DIAN's page was still unresponsive after {self.wait_minutes} min.")
                    if self.url:
                        self.notifier.send(msg_unresponsive(self.stage, 0, self.who))
                    return UNRESPONSIVE
                log(f"Target not offered within {self.wait_minutes} min.")
                if self.url:
                    self.notifier.send(msg_not_open(self.wait_minutes, self.who))
                return NOT_OPEN
            self._mark("service_chosen", started)
            self._share_link_soon(display)
            self._ensure_profile()
            self.stage = "PasoDos"
            wizard.pick_slot()
            self._mark("slot_chosen", started)
            self.stage = "PasoTres"
            wizard.before_captcha(self.profile)
            self._wait_for_link()
            self.notifier.send(msg_tap_captcha(self.url, wizard.chosen, self.who) if self.url else
                               Message("🔴 Toca el captcha en la ventana de Chrome.", urgent=True))
            self._mark("captcha_shown", started)
            self.stage = "captcha"
            shown = time.monotonic()
            solved = wizard.wait_captcha(shown + self.captcha_minutes * 60)
            self.captcha_wait = round(time.monotonic() - shown, 1)
            if not solved:
                self.notifier.send(Message(f"⌛ Nadie tocó el captcha de {self.who} en "
                                           f"{self.captcha_minutes} min; cierro el navegador."))
                return CAPTCHA_TIMEOUT
            self._mark("captcha_solved", started)
            self.stage = "datos"
            wizard.after_captcha(self.profile)
            self._mark("form_filled", started)
            if self.dry_run:
                self._shot(page, "dry-run")
                self.notifier.send(msg_dry_run(wizard.chosen, self.who))
                self._hold(page, 2, wizard.chosen)
                return DRY_RUN_OK
            self.stage = "confirmar"
            confirmation = wizard.confirm()
            self._mark("booked", started)
            self._record(wizard.chosen)
            self.notifier.send(msg_booked(wizard.chosen, self.who))
            self.notifier.send_photo(self._shot(page, "booked"), confirmation[:900])
            return BOOKED
        except Exception as e:  # noqa: BLE001 - report it, then let the human take over
            self.error = e
            hold = self.hold_minutes if self.url and self.stage in HOLD_STAGES else 0
            if is_unresponsive(e):
                log(f"DIAN's page stopped answering at {self.stage}.")
                self.notifier.send(msg_unresponsive(self.stage, hold, self.who))
                result = UNRESPONSIVE
            else:
                log(f"Failed at {self.stage}: {type(e).__name__}")
                shot = self._shot(page, "failed")
                self.notifier.send(msg_failed(self.stage, e, self.url if hold else None, hold,
                                              self.who))
                if shot:
                    self.notifier.send_photo(shot, f"Pantalla al fallar ({self.stage})")
                result = SLOT_GONE if isinstance(e, SlotUnavailable) else FAILED
            if hold:
                self._hold(page, hold, wizard.chosen)
            return result

    def _share_link_soon(self, display):
        """Send the link once the tunnel is up, without holding back the wizard: an opening can
        be gone in under a minute and the tunnel alone takes 10-40 s. Started once."""
        if self.link_thread is None:
            self.link_thread = threading.Thread(target=self._share_link, args=(display,),
                                                daemon=True)
            self.link_thread.start()

    def _share_link(self, display):
        try:
            self.url = display.url()
            if self.url:
                self.notifier.send(msg_get_ready(self.url, self.who))
                self.events.emit("booker_link_sent")
        except Exception as e:  # noqa: BLE001 - raised again by _wait_for_link at the captcha
            log(f"Live link failed: {type(e).__name__}")
            self.link_error = e

    def _wait_for_link(self):
        """The captcha message needs the link, and must come after "prepárate"."""
        if self.link_thread is not None:
            self.link_thread.join()
        if self.link_error is not None:
            raise self.link_error

    def _hold(self, page, minutes, chosen):
        """Keep the browser alive for the human; stop early if they book by hand."""
        log(f"Holding the browser open for {minutes} min.")
        deadline = time.monotonic() + minutes * 60
        while time.monotonic() < deadline:
            try:
                if page.locator('[nombre="ModalInfoCita"]:visible').count():
                    if self._record({**chosen, "via": "a mano en el navegador"}):
                        self.notifier.send(Message("✅ Parece que agendaste a mano. Revisa tu correo."))
                    else:
                        self.notifier.send(Message(
                            f"⚠️ Se agendó a mano una cita que NO es de {self.case.city_name}. "
                            f"No la registro, así que el bot sigue buscando para {self.who}. "
                            "Cancélala si no la quieres."))
                    return
            except Exception:  # noqa: BLE001 - the page may be mid-navigation
                pass
            time.sleep(2)

    def _shot(self, page, tag):
        try:
            os.makedirs(self.shots_dir, exist_ok=True)
            path = os.path.join(self.shots_dir, f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.png")
            page.screenshot(path=path, full_page=True)
            return path
        except Exception:  # noqa: BLE001
            return None

    def _record(self, chosen):
        """Mark the target as booked, which stops further bookers. Only a booking whose
        tramite names the target city counts. Returns whether it was recorded."""
        if not mentions_city(chosen.get("servicio", ""), self.settings.city):
            log("A booking was made for another city; not recording it.")
            self.events.emit("booker_other_city_booking")
            return False
        path = self.settings.booking_file
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"booked": True, "at": cadence.now_bogota().isoformat(timespec="seconds"),
                       "case": self.case.id, "chosen": chosen, "dry_run": False}, f,
                      ensure_ascii=False, indent=2)
        if self.on_booked is not None:
            try:
                self.on_booked(chosen)
                self.events.emit("booker_recorded", case=self.case.id)
            except Exception as e:  # noqa: BLE001 - the booking itself is done
                log(f"Could not record the booking in Airtable: {type(e).__name__}")
                self.events.emit("booker_record_failed", case=self.case.id,
                                 error=self.profile.scrub(str(e))[:300] if self.profile else "")
                self.notifier.send(Message(
                    f"⚠️ La cita de {self.who} quedó agendada, pero no pude marcarla en Airtable "
                    f"({type(e).__name__}). Ponle Status = Booked a mano.", urgent=True))
        return True
