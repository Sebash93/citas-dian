"""Turn a raw 'colas' response into a verdict for the watched cities."""
import html
import re
import unicodedata
from dataclasses import dataclass, field

from .client import STEP_COLAS, STEP_NO_COLAS, SchemaChanged

NONE = "NONE"            # nothing open for the category, or open only in unwatched cities
FOUND = "FOUND"          # at least one watched city has an open tramite


@dataclass
class CheckResult:
    status: str
    matches: list = field(default_factory=list)       # tramite names in any watched city
    other_cities: list = field(default_factory=list)  # open, but not watched (log only)
    raw: dict = None
    by_city: dict = field(default_factory=dict)       # watched city -> its open tramite names


def normalize(text):
    """Lowercase, strip accents and HTML, collapse whitespace."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(text)))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(text)))).strip()


def _items(data):
    groups = data.get("ObjetosEncontrados") or []
    if groups and isinstance(groups[0], list):
        return groups[0]
    return groups


def mentions_city(text, city):
    """True if `city` appears as a word in `text`, ignoring accents and case.

    DIAN writes the city in different ways ('“Medellín - Solicitud…”' in the API,
    'Medellín “Solicitud…”' in the wizard), so the check is on the word, not the format.
    """
    return re.search(rf"\b{re.escape(normalize(city))}\b", normalize(text)) is not None


def city_of(name):
    """City prefix of a tramite name, e.g. '“Cali - Solicitud de devolución…”' -> 'cali'.

    Only the name is used: other fields (Requisitos) mention several cities, e.g.
    "las direcciones seccionales de Bogotá, Bucaramanga, Cali y Medellín".
    """
    text = normalize(name).strip(" \"'“”‘’«».")
    head, sep, _ = text.partition(" - ")
    return head.strip(" \"'“”‘’«»") if sep else None


def name_is_in(name, city):
    """Whether a tramite name belongs to `city`. Only the city prefix counts, and it must be
    the city itself ('Bogotá D.C. -' is bogota, 'Medellín Norte -' is not medellin). Without
    a recognizable prefix, the name only (never the whole item)."""
    prefix = city_of(name)
    if prefix is None:
        return mentions_city(name, city)
    prefix = re.sub(r"[\s,]*d\.?\s?c\.?$", "", prefix).strip()  # "bogota d.c." -> "bogota"
    return prefix == normalize(city)


def evaluate(data, cities, service=""):
    """Classify one ValidadorValidar(colas) response for one city or a list of cities.
    With `service`, only tramites whose name contains it count; the others are only logged."""
    cities = [cities] if isinstance(cities, str) else list(cities)
    error = data.get("ErrorString")
    step = data.get("DetalleAdicional")

    if not data.get("Encontrado"):
        if step == STEP_NO_COLAS or (error and "no hay agendas" in normalize(error)):
            return CheckResult(NONE, raw=data)
        raise SchemaChanged(f"not found with unexpected step={step!r} error={error!r}")

    if step != STEP_COLAS:
        raise SchemaChanged(f"found with unexpected step={step!r}")
    items = _items(data)
    if not items or not all(isinstance(i, dict) for i in items):
        raise SchemaChanged("found but ObjetosEncontrados has no item objects")

    by_city, matches, others = {}, [], []
    for item in items:
        name = clean(item.get("Nombre") or "")
        if not name:
            raise SchemaChanged("found an item without Nombre")
        city = next((c for c in cities if name_is_in(name, c)), None)
        if city is None or (service and normalize(service) not in normalize(name)):
            others.append(name)
        else:
            by_city.setdefault(city, []).append(name)
            matches.append(name)
    return CheckResult(FOUND if matches else NONE, matches, others, raw=data, by_city=by_city)
