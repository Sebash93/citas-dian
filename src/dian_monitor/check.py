"""Turn a raw 'colas' response into a verdict for the target city."""
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field

from .client import STEP_COLAS, STEP_NO_COLAS, SchemaChanged

NONE = "NONE"            # nothing open for the category, or open only in other cities
FOUND = "FOUND"          # the target city has an open tramite


@dataclass
class CheckResult:
    status: str
    matches: list = field(default_factory=list)       # tramite names in the target city
    other_cities: list = field(default_factory=list)  # open, but not our city (log only)
    raw: dict = None


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


def evaluate(data, city):
    """Classify one ValidadorValidar(colas) response.

    The whole item (not only its Nombre) is searched for the city, so a match still
    works if DIAN moves the city into another field.
    """
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

    target = normalize(city)
    matches, others = [], []
    for item in items:
        name = clean(item.get("Nombre") or json.dumps(item, ensure_ascii=False))
        haystack = normalize(json.dumps(item, ensure_ascii=False))
        (matches if target in haystack else others).append(name)
    return CheckResult(FOUND if matches else NONE, matches, others, raw=data)
