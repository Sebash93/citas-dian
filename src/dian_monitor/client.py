"""Read-only client for the DIAN scheduling backend.

The site is a Ciel "C-Media WebPlayer" (ASP.NET WebForms). Every wizard step is a
POST to /Player.aspx/ValidadorValidar; the step is picked by
respuestaBase.DetalleAdicional. Requests need:
  - a single-use anti-CSRF token: scraped from the page, then rotated through the
    X-token response header of each ValidadorValidar call;
  - ConfiguracionServicioRestEncriptado, returned by ObtenerConfiguracion (~500 KB,
    fetched once per session).
Only availability lookups are implemented. The booking step is reCAPTCHA-gated
and deliberately out of scope.
"""
import json
import re
import time

import requests

BASE = "https://agendamiento.dian.gov.co"
RUTA_RECURSOS = "Recursos/CitasDIAN/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
# DIAN gets very slow when slots open (15-35 s per request observed on 2026-10-20),
# so be patient: a slow answer is a signal, not a failure.
TIMEOUT = 90

STEP_COLAS = "manejadorEncontroColas"
STEP_NO_COLAS = "manejadorNoEncontroColas"


class DianError(Exception):
    """Transient failure (network, 5xx). Retry later."""


class Blocked(DianError):
    """403/429 or a challenge page: stop polling and back off."""


class SchemaChanged(DianError):
    """The response no longer looks like what we know. Needs a human."""


class TimedOut(DianError):
    """DIAN did not answer within TIMEOUT. Often means it is overloaded by an opening."""


class DianClient:
    def __init__(self, session=None):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "es-CO,es;q=0.9"})
        self._token = None
        self._config = None
        self.requests_made = 0
        # Seconds the last ValidadorValidar POST took (the availability lookup itself).
        self.last_latency = None

    # --- transport ---------------------------------------------------------

    def _check_status(self, resp, what):
        if resp.status_code in (403, 429):
            raise Blocked(f"{what}: HTTP {resp.status_code}")
        if resp.status_code >= 500:
            raise DianError(f"{what}: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise SchemaChanged(f"{what}: unexpected HTTP {resp.status_code}")

    def _fetch_token(self):
        try:
            resp = self.s.get(f"{BASE}/?recurso=CitasDIAN", timeout=TIMEOUT)
        except requests.Timeout as e:
            raise TimedOut(f"GET page: no answer in {TIMEOUT} s") from e
        except requests.RequestException as e:
            raise DianError(f"GET page: {e}") from e
        self.requests_made += 1
        self._check_status(resp, "GET page")
        if "recaptcha" in resp.text.lower() and "anticsrf" not in resp.text:
            raise Blocked("GET page: challenge page instead of the app")
        m = re.search(r'name="anticsrf"[^>]*value="([^"]+)"', resp.text)
        if not m:
            raise SchemaChanged("GET page: anticsrf token not found")
        return m.group(1)

    def _post(self, method, payload):
        token = self._token or self._fetch_token()
        self._token = None  # single use
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE,
            "Referer": f"{BASE}/?recurso=CitasDIAN",
            "RequestVerificationToken": token,
            "g-recaptcha-response": "",
        }
        started = time.monotonic()
        try:
            resp = self.s.post(f"{BASE}/Player.aspx/{method}", headers=headers,
                               data=json.dumps(payload), timeout=TIMEOUT)
        except requests.Timeout as e:
            raise TimedOut(f"{method}: no answer in {TIMEOUT} s") from e
        except requests.RequestException as e:
            raise DianError(f"{method}: {e}") from e
        if method == "ValidadorValidar":
            self.last_latency = time.monotonic() - started
        self.requests_made += 1
        self._check_status(resp, method)
        self._token = resp.headers.get("X-token") or None
        try:
            outer = resp.json()
            inner = outer["d"]
            return json.loads(inner) if isinstance(inner, str) else inner
        except (ValueError, KeyError, TypeError) as e:
            raise SchemaChanged(f"{method}: response is not the expected {{'d': ...}} JSON") from e

    # --- session config ----------------------------------------------------

    def _configuracion(self):
        if self._config is None:
            data = self._post("ObtenerConfiguracion",
                              {"rutaRecurso": RUTA_RECURSOS, "nombreRecurso": ""})
            raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            enc = re.search(r'"ConfiguracionServicioRestEncriptado"\s*:\s*"([^"]+)"', raw)
            if not enc:
                raise SchemaChanged("ObtenerConfiguracion: encrypted config not found")
            fecha = re.search(r'"FechaActualizacion"\s*:\s*"([^"]+)"', raw)
            self._config = {
                "ConfiguracionServicioRestEncriptado": enc.group(1),
                "ControlesGeolocalizacion": [],
                "FormatoCitas": "{cita.codigo}    {cita.fecha}    {cita.Oficina.Nombre}",
                "IdPais": 1,
                "DistanciaMinima": 0,
                "TopOficinasCercanas": 0,
                "FormatoEncabezadoOficina": "",
                "FormatoOficina": "{0}",
                "ModoWebPlayer": True,
                "Archivo": {"Ruta": RUTA_RECURSOS,
                            "FechaActualizacion": fecha.group(1) if fecha else ""},
                "TipoPolitica": 0,
                "ObtenerEspecialidadesVirtuales": False,
                "ObtenerEspecialidadesPresenciales": False,
                "GenerarTurno": False,
            }
        return self._config

    def reset(self):
        """Drop the session so the next call bootstraps from scratch."""
        self._token = None
        self._config = None
        self.s.cookies.clear()

    # --- steps -------------------------------------------------------------

    def find_colas(self, tipo_persona, categoria, tipo_atencion):
        """Return the raw inner response of the 'colas' (tramites with open slots) step."""
        cita = {
            "CodigoCita": None,
            "CodigoCitaModificada": None,
            "Cola": {"IdEspecialidad": "0", "Nombre": None},
            "TipoEspecialidad": {
                "IdTipoEspecialidad": int(tipo_atencion),
                "Nombre": "Virtual" if str(tipo_atencion) == "2" else "Presencial",
            },
            "Oficina": {"IdOficina": "001", "Nombre": None, "Latitud": 0, "Longitud": 0},
            "UsuarioCliente": {
                "IdTipoCliente": str(tipo_persona), "IdTipoDocumento": 0,
                "Nombre": None, "Apellido": None, "NumeroDocumento": None,
                "CorreoElectronico": None, "Celular": None, "Telefono": None,
                "Direccion": None, "IdCiudad": 0, "IdEstado": 0,
                "AceptaPoliticaDatos": False,
            },
            "Fecha": "2001-01-01T17:00:00.000Z",
            "Hora": "2001-01-01T17:00:00.000Z",
            "IdAgenda": 0,
            "Estado": {"IdEstado": 0, "Nombre": None},
            "Funcionario": {"NombreAMostrar": None, "Id": None},
            "Archivo": None,
            "CamposAdicionales": None,
            "EsFlujoCitaCreacion": "true",
        }
        payload = {
            "nombre": "ValidadorDatos.CitasWeb",
            "configuracion": self._configuracion(),
            "respuestaBase": {
                "Fuente": "Validador",
                "Encontrado": False,
                "DetalleAdicional": STEP_COLAS,
                "ObjetosEncontrados": [json.dumps(cita), "Nombre", str(tipo_persona),
                                       str(categoria), str(tipo_atencion)],
                "Recurso": "CitasDIAN",
            },
        }
        data = self._post("ValidadorValidar", payload)
        if not isinstance(data, dict) or "Encontrado" not in data:
            raise SchemaChanged("ValidadorValidar: missing 'Encontrado'")
        return data
