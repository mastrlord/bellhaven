"""Normalization helpers so formatting differences don't look like real differences."""
import re
from difflib import SequenceMatcher

SUFFIXES = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "pk": "pike",
    "parkway": "pkwy", "highway": "hwy", "circle": "cir", "terrace": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}

# Website care offering -> CRM care_type picklist value
CARE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}

PO_BOX = re.compile(r"^\s*p\.?\s*o\.?\s*box\b", re.I)


def norm_street(s):
    s = re.sub(r"[.,#]", " ", (s or "").lower())
    return " ".join(SUFFIXES.get(t, t) for t in s.split())


def street_number(s):
    m = re.match(r"\s*(\d+)", s or "")
    return m.group(1) if m else ""


def is_po_box(s):
    return bool(PO_BOX.match(s or ""))


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def name_similarity(a, b):
    return SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def map_care(offerings):
    """Map website offerings to CRM values, preserving order, dropping unknowns."""
    out = []
    for o in offerings:
        v = CARE_MAP.get(o.strip().lower())
        if v and v not in out:
            out.append(v)
    return out
