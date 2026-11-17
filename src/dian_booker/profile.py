"""The applicant's data, from their Airtable case (cases.Case.personal)."""
import re
from dataclasses import dataclass

FIELDS = ("full_name", "document_number", "email", "phone")


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    doc_number: str
    full_name: str
    email: str
    phone: str
    doc_type: str = "Cédula de ciudadanía"  # always; label as shown in the DIAN TipoDocumento list

    @classmethod
    def from_case(cls, case):
        """The person of one case. Errors name the case and the field, never a value."""
        p = {k: str(case.personal.get(k) or "").strip() for k in FIELDS}
        missing = [k for k in FIELDS if not p[k]]
        if missing:
            raise ProfileError(f"case {case.id!r}: missing {', '.join(missing)}")
        doc = re.sub(r"[\s.]", "", p["document_number"])
        phone = re.sub(r"\D", "", p["phone"])
        if len(phone) == 12 and phone.startswith("57"):  # +57 typed in the form
            phone = phone[2:]
        if not doc.isdigit():
            raise ProfileError(f"case {case.id!r}: document_number must be digits only")
        if "@" not in p["email"]:
            raise ProfileError(f"case {case.id!r}: email is not an email")
        if len(phone) != 10:
            raise ProfileError(f"case {case.id!r}: phone must have 10 digits")
        return cls(doc_number=doc, full_name=p["full_name"], email=p["email"], phone=phone)

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
