"""The applicant's data, read only from the environment (GitHub secrets or a local shell)."""
import os
import re
from dataclasses import dataclass


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    doc_number: str
    full_name: str
    email: str
    phone: str
    doc_type: str = "Cédula de ciudadanía"  # label as shown in the DIAN TipoDocumento list

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        get = lambda name: env.get(name, "").strip()  # noqa: E731
        missing = [n for n in ("BOOKER_DOC_NUMBER", "BOOKER_FULL_NAME", "BOOKER_EMAIL",
                               "BOOKER_PHONE") if not get(n)]
        if missing:
            raise ProfileError(f"missing {', '.join(missing)}")
        doc = re.sub(r"[\s.]", "", get("BOOKER_DOC_NUMBER"))
        phone = re.sub(r"\D", "", get("BOOKER_PHONE"))
        if not doc.isalnum():
            raise ProfileError("BOOKER_DOC_NUMBER must be letters/digits only")
        if "@" not in get("BOOKER_EMAIL"):
            raise ProfileError("BOOKER_EMAIL is not an email")
        return cls(doc_number=doc, full_name=get("BOOKER_FULL_NAME"), email=get("BOOKER_EMAIL"),
                   phone=phone, doc_type=get("BOOKER_DOC_TYPE") or cls.doc_type)

    def scrub(self, text):
        """Mask this person's data (and any live-browser link) in free text such as errors."""
        text = re.sub(r"https://[a-z0-9-]+\.trycloudflare\.com\S*", "<link>", str(text))
        secrets = [self.doc_number, self.email, self.phone, self.full_name]
        secrets += [w for w in self.full_name.split() if len(w) >= 3]
        for value in sorted(filter(None, secrets), key=len, reverse=True):
            text = re.sub(re.escape(value), "▒", text, flags=re.IGNORECASE)
        if len(self.phone) >= 10:  # also as typed without the country code
            text = text.replace(self.phone[-10:], "▒")
        # The document number as DIAN may print it: "1.017.234.567" or "1 017 234 567".
        spaced = r"[.\s]?".join(map(re.escape, self.doc_number))
        return re.sub(spaced, "▒", text, flags=re.IGNORECASE)

    def __repr__(self):
        # Never let personal data reach a log line through an f-string or traceback.
        return f"Profile(doc_type={self.doc_type!r}, doc=…{self.doc_number[-2:]})"

    __str__ = __repr__
