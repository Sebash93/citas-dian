"""Drives the DIAN booking wizard in a real browser page (Playwright).

Controls are addressed by their `nombre` attribute; the server answers each step in
5-10 s when slots are open, so every wait is long. Verified live on 2026-10-26 up to
the reCAPTCHA (PasoUno, PasoDos, PasoTres before the captcha). After the captcha the
flow follows the site's scripts and must be confirmed with a --dry-run:
  - the document number field appears only once `captchaValido` is true and blocks paste;
  - a known user gets a "datos correctos?" prompt, a new one gets name/email/phone;
  - PasoCuatro shows a summary and `btnAgendarCita`.
"""
import re
import time

from dian_monitor.check import normalize
from dian_monitor.config import CATEGORIAS

STEP_TIMEOUT = 95  # seconds; DIAN's slowest observed answers are ~35 s, the client allows 90

PERSONA = {"1": "Natural", "2": "Jurídica"}
ATENCION = {"1": "Presencial", "2": "Videoatención"}


class SlotUnavailable(Exception):
    """The wizard says there is nothing to book (anymore)."""


class PageUnresponsive(Exception):
    """DIAN's page did not answer in time (it slows down a lot when slots open)."""


class CityMismatch(Exception):
    """Safety stop: the tramite about to be booked does not name the target city."""


def is_unresponsive(error):
    # Playwright's TimeoutError is matched by name: tests never import Playwright.
    return isinstance(error, PageUnresponsive) or type(error).__name__ == "TimeoutError"


def mentions_city(text, city):
    """True if `city` appears as a word in `text`, ignoring accents and case.

    DIAN writes the city in different ways ('“Medellín - Solicitud…”' in the API,
    'Medellín “Solicitud…”' in the wizard), so the check is on the word, not the format.
    """
    return re.search(rf"\b{re.escape(normalize(city))}\b", normalize(text)) is not None


class WizardError(Exception):
    """The page did not do what we expected; a human can take over through the link."""


def choose_service(options, city, preferred=""):
    """Index of the tramite to book, or None. Never a default: only a tramite whose name
    contains the city word is eligible. `preferred` narrows the choice among those
    (e.g. "vehiculos"); if none of them matches it, nothing is chosen."""
    eligible = [i for i, o in enumerate(options) if o.strip() and mentions_city(o, city)]
    if preferred:
        eligible = [i for i in eligible if normalize(preferred) in normalize(options[i])]
    return eligible[0] if eligible else None


def choose_option(options, wanted):
    """Index of the option whose text equals `wanted` ignoring accents/case, else None."""
    target = normalize(wanted)
    return next((i for i, o in enumerate(options) if o.strip() and normalize(o) == target), None)


class Wizard:
    def __init__(self, page, settings, log, service_hint=""):
        self.page = page
        self.settings = settings
        self.log = log
        self.service_hint = service_hint
        self.chosen = {}
        self.unresponsive = False

    # --- helpers -----------------------------------------------------------

    def ctrl(self, nombre, pantalla=None):
        base = f'div[pantalla="{pantalla}"][nombre="{nombre}"]' if pantalla else f'[nombre="{nombre}"]'
        return self.page.locator(f"{base}:visible").first

    def wait_any(self, selectors, timeout=STEP_TIMEOUT):
        """First of `selectors` (CSS) to become visible."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for sel in selectors:
                if self.page.locator(sel).first.is_visible():
                    return sel
            time.sleep(0.25)
        raise PageUnresponsive(f"none of {selectors} appeared in {timeout} s")

    def error_text(self):
        for nombre in ("ModalError", "ModalSinCitas"):
            modal = self.ctrl(nombre)
            if modal.is_visible():
                return modal.inner_text().strip()
        return None

    def select_options(self, nombre, timeout=STEP_TIMEOUT):
        select = self.page.locator(f'[nombre="{nombre}"]:visible select').first
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            options = select.locator("option").all_text_contents()
            if len([o for o in options if o.strip()]):
                return select, options
            time.sleep(0.25)
        raise PageUnresponsive(f"{nombre} had no options after {timeout} s")

    def click_text(self, nombre, text):
        self.ctrl(nombre).wait_for(timeout=STEP_TIMEOUT * 1000)
        self.ctrl(nombre).get_by_text(text, exact=False).first.click()

    def next_page(self):
        self.ctrl("btnSiguiente").click()

    # --- PasoUno: persona, atencion, categoria, tramite ----------------------

    def find_service(self):
        """Walk PasoUno; return the chosen tramite text, or None if the city is not open."""
        s = self.settings
        self.page.goto(s.booking_url, wait_until="domcontentloaded", timeout=STEP_TIMEOUT * 1000)
        self.ctrl("btnSolicitarCita").wait_for(timeout=STEP_TIMEOUT * 1000)
        self.ctrl("btnSolicitarCita").click()
        self.click_text("TipoPersona", PERSONA[s.tipo_persona])
        self.click_text("TipoAtencion", ATENCION[s.tipo_atencion])
        self.click_text("Categorias", CATEGORIAS[s.categoria])
        found = self.wait_any(['[nombre="Servicios"] select', '[nombre="ModalError"]'])
        if "ModalError" in found:
            self.log(f"PasoUno: {self.ctrl('ModalError').inner_text().strip()[:120]}")
            return None
        select, options = self.select_options("Servicios")
        index = choose_service(options, s.city, self.service_hint)
        if index is None:
            self.log(f"PasoUno: target not offered; open: {[o for o in options if o.strip()]}")
            return None
        select.select_option(index=index)
        self.chosen["servicio"] = options[index].strip()
        return self.chosen["servicio"]

    def wait_for_service(self, deadline, retry_seconds=20):
        """Retry PasoUno until the target tramite is offered or `deadline` (monotonic) passes.
        A page that does not load is retried too; `self.unresponsive` tells whether the last
        try timed out (as opposed to DIAN answering that the city is not offered)."""
        while True:
            try:
                service = self.find_service()
                self.unresponsive = False
            except Exception as e:  # noqa: BLE001 - only timeouts are retried
                if not is_unresponsive(e):
                    raise
                self.unresponsive = True
                self.log(f"PasoUno: DIAN's page is unresponsive ({type(e).__name__}); retrying.")
                service = None
            if service:
                return service
            if time.monotonic() + retry_seconds > deadline:
                return None
            time.sleep(retry_seconds)

    def check_city(self):
        """Safety stop before the captcha and before Agendar: the chosen tramite must name
        the target city, whatever happened earlier in the wizard."""
        service = self.chosen.get("servicio", "")
        if not mentions_city(service, self.settings.city):
            raise CityMismatch(f"chosen tramite does not name {self.settings.city}: {service!r}")

    # --- PasoDos: ciudad, sede, fecha, hora ---------------------------------

    def pick_slot(self):
        self.next_page()
        found = self.wait_any(['[nombre="Ciudades"] select', '[nombre="btnFecha"]',
                               '[nombre="ModalError"]'])
        if "ModalError" in found:
            raise SlotUnavailable(self.error_text())
        if "Ciudades" in found:
            select, options = self.select_options("Ciudades")
            index = choose_option(options, self.settings.city)
            if index is None:
                raise SlotUnavailable(f"city not in PasoDos list: {[o for o in options if o]}")
            select.select_option(index=index)
            found = self.wait_any(['[nombre="Sedes"] select', '[nombre="btnFecha"]',
                                   '[nombre="ModalError"]'])
            if "Sedes" in found:
                select, options = self.select_options("Sedes")
                index = next(i for i, o in enumerate(options) if o.strip())
                select.select_option(index=index)
                self.chosen["sede"] = options[index].strip()
            self.wait_any(['[nombre="btnFecha"]'])
        self._pick_date()
        select, options = self.select_options("Horas")
        index = next(i for i, o in enumerate(options) if o.strip())  # earliest time
        select.select_option(index=index)
        self.chosen["hora"] = options[index].strip()
        self.next_page()
        self.wait_any(['[nombre="TipoDocumento"] select'])
        return self.chosen

    def _pick_date(self):
        """Open the date popup and click the first enabled day (earliest)."""
        self.ctrl("btnFecha").click()
        cal = self.page.locator('[nombre="Calendario"]')
        enabled = cal.locator("td.k-calendar-td:not(.k-disabled):not(.k-other-month) a.k-link")
        deadline = time.monotonic() + STEP_TIMEOUT
        months_ahead = 0
        while not enabled.count():
            if time.monotonic() > deadline:
                raise SlotUnavailable("no enabled date in the calendar")
            if months_ahead < 3:
                # The arrows are not clickable at phone width; Kendo's own API moves the view.
                self.page.evaluate("() => $('[nombre=Calendario] .k-calendar')"
                                   ".data('kendoCalendar').navigateToFuture()")
                months_ahead += 1
            time.sleep(0.5)
        self.chosen["fecha"] = enabled.first.get_attribute("title")
        enabled.first.click()

    # --- PasoTres: documento, captcha, datos --------------------------------

    def before_captcha(self, profile):
        self.check_city()
        select, options = self.select_options("TipoDocumento")
        index = choose_option(options, profile.doc_type)
        if index is None:
            raise WizardError(f"document type {profile.doc_type!r} not offered")
        select.select_option(index=index)
        self.ctrl("Captcha").scroll_into_view_if_needed()

    def captcha_solved(self):
        return bool(self.page.evaluate("() => window.captchaValido === true"))

    def wait_captcha(self, deadline):
        while time.monotonic() < deadline:
            if self.captcha_solved():
                return True
            time.sleep(0.3)
        return False

    def after_captcha(self, profile):
        doc = self.page.locator('div[pantalla="PasoTres"][nombre="tbxNumeroDoc"] input')
        doc.wait_for(state="visible", timeout=STEP_TIMEOUT * 1000)
        doc.click()
        doc.press_sequentially(profile.doc_number, delay=25)  # paste is blocked on this field
        doc.press("Tab")
        consultar = self.ctrl("btnConsultar", "PasoTres")
        if consultar.is_visible():
            consultar.click()
        fields = {"tbxNombreCompleto": profile.full_name, "tbxCorreo": profile.email,
                  "tbxTelefono": profile.phone}
        self.wait_any([f'div[pantalla="PasoTres"][nombre="{n}"] input' for n in fields]
                      + ['[nombre="DatosCorretos"]', 'div[contenido="usuarioEncontrado"]',
                         '[nombre="ModalError"]'])
        if self.error_text():
            raise SlotUnavailable(self.error_text())
        for nombre, value in fields.items():
            box = self.page.locator(f'div[pantalla="PasoTres"][nombre="{nombre}"] input:visible')
            if box.count() and not box.first.input_value().strip():
                box.first.fill(value)
        si = self.page.locator('[nombre="DatosCorretos"]:visible [nombre="btnSi"], '
                               '[nombre="btnSi"]:visible').first
        if si.is_visible():
            si.click()
        terms = self.page.locator('.checkBoxTerminos input[type="checkbox"]').first
        if not terms.is_checked():
            terms.check(force=True)
        self.next_page()
        self.wait_any(['[nombre="btnAgendarCita"]', '[nombre="ModalError"]'])
        if self.error_text():
            raise SlotUnavailable(self.error_text())

    # --- PasoCuatro ---------------------------------------------------------

    def confirm(self):
        """Click Agendar and return the confirmation text."""
        self.check_city()
        self.ctrl("btnAgendarCita").click()
        found = self.wait_any(['[nombre="ModalInfoCita"]', '[nombre="ModalError"]',
                               '[nombre="ModalSinCitas"]'], timeout=STEP_TIMEOUT * 2)
        text = self.page.locator(f"{found}:visible").first.inner_text().strip()
        if "ModalInfoCita" not in found:
            raise SlotUnavailable(text)
        return text
