"""Document-agnostic normalization of entities, quantities, units and time periods.

Nothing in this module is specific to a document, a company or a country. Every
rule here encodes a general convention of written English business/statistical
prose (accounting parentheses, SI/Indian magnitude words, fiscal-year notation,
legal-suffix stripping) rather than knowledge about a particular source.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Magnitude words. Keys are matched case-insensitively against a single token.
# --------------------------------------------------------------------------
MULTIPLIERS: Dict[str, float] = {
    "hundred": 1e2,
    "k": 1e3,
    "thousand": 1e3,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "mn": 1e6,
    "m": 1e6,
    "million": 1e6,
    "millions": 1e6,
    "cr": 1e7,
    "crore": 1e7,
    "crores": 1e7,
    "bn": 1e9,
    "b": 1e9,
    "billion": 1e9,
    "billions": 1e9,
    "tn": 1e12,
    "trillion": 1e12,
    "trillions": 1e12,
}

# Currency symbols and ISO-ish codes -> canonical currency code.
CURRENCY_SYMBOLS: Dict[str, str] = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "u.s.$": "USD",
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "¥": "JPY",
    "jpy": "JPY",
    "aed": "AED",
    "sgd": "SGD",
    "cny": "CNY",
    "rmb": "CNY",
}

# Unit families decide whether two quantities are even comparable. Facts in
# different families are never compared numerically.
UNIT_FAMILIES: Dict[str, str] = {
    "USD": "currency",
    "INR": "currency",
    "EUR": "currency",
    "GBP": "currency",
    "JPY": "currency",
    "AED": "currency",
    "SGD": "currency",
    "CNY": "currency",
    "%": "ratio",
    "% of GDP": "ratio_of_gdp",
    "bps": "ratio",
    "x": "ratio",
    "days": "duration",
    "months": "duration",
    "years": "duration",
}

# Physical unit symbols. This is the one closed vocabulary in the system, and
# it is closed because SI and its common commercial cousins are: a watt-hour
# means the same thing in an energy report and a data-centre filing. Anything
# else that counts something is recognised by shape (see ``measurement_token``)
# rather than by being listed here, which is what lets a document introduce
# "outpatient visits" or "teaching hours" without a code change.
#
# Single letter symbols ("m", "g", "t") are excluded: they collide with
# magnitude words and initials far more often than they carry a measurement.
UNIT_SYMBOLS: Dict[str, str] = {
    # energy / power
    "wh": "energy", "kwh": "energy", "mwh": "energy", "gwh": "energy",
    "twh": "energy", "kw": "power", "mw": "power", "gw": "power", "tw": "power",
    "kva": "power", "mva": "power", "btu": "energy", "kcal": "energy",
    # distance / area / volume
    "km": "distance", "cm": "distance", "mm": "distance", "nm": "distance",
    "mi": "distance", "ft": "distance", "sqft": "area", "sqm": "area",
    "ha": "area", "acre": "area", "km2": "area", "ml": "volume",
    "cl": "volume", "bbl": "volume", "gal": "volume", "cbm": "volume",
    # mass
    "kg": "mass", "mg": "mass", "kt": "mass", "mt": "mass", "lb": "mass",
    "lbs": "mass", "oz": "mass",
    # information / frequency / pressure / other
    "kb": "data", "mb": "data", "gb": "data", "tb": "data", "pb": "data",
    "hz": "frequency", "khz": "frequency", "mhz": "frequency", "ghz": "frequency",
    "psi": "pressure", "bar": "pressure", "db": "sound",
    "ppm": "concentration", "mmhg": "pressure",
}

# Unit words spelled out. Kept short on purpose: a plural noun is recognised by
# shape, so this only names the singular forms that would otherwise be missed.
UNIT_WORDS: Dict[str, str] = {
    "tonne": "mass", "tonnes": "mass", "ton": "mass", "tons": "mass",
    "gram": "mass", "grams": "mass", "kilogram": "mass", "kilograms": "mass",
    "litre": "volume", "litres": "volume", "liter": "volume", "liters": "volume",
    "metre": "distance", "metres": "distance", "meter": "distance",
    "meters": "distance", "kilometre": "distance", "kilometres": "distance",
    "kilometer": "distance", "kilometers": "distance", "mile": "distance",
    "miles": "distance", "acre": "area", "acres": "area", "hectare": "area",
    "hectares": "area", "day": "duration", "days": "duration",
    "week": "duration", "weeks": "duration", "month": "duration",
    "months": "duration", "year": "duration", "years": "duration",
    "hour": "duration", "hours": "duration", "minute": "duration",
    "minutes": "duration", "second": "duration", "seconds": "duration",
}

# Words that may sit directly after a number without being what is measured:
# reporting verbs, comparison words and connectives. A number followed by one
# of these is not a counted quantity.
NON_UNIT_FOLLOWERS = {
    "and", "or", "but", "than", "then", "versus", "vs", "compared", "against",
    "respectively", "each", "more", "less", "fewer", "higher", "lower", "above",
    "below", "over", "under", "around", "about", "approximately", "nearly",
    "almost", "times", "point", "points", "further", "additional", "new",
    "other", "such", "same", "different", "including", "excluding", "namely",
    "was", "were", "is", "are", "be", "been", "being", "has", "have", "had",
    "will", "would", "may", "might", "can", "could", "should", "must",
    "reported", "recorded", "registered", "estimated", "projected", "expected",
    "stood", "stands", "reached", "totalled", "totaled", "amounted", "rose",
    "grew", "fell", "declined", "increased", "decreased", "remained",
}

# Legal / corporate suffixes stripped when canonicalising an entity name.
LEGAL_SUFFIXES = {
    "ltd", "limited", "plc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "llp", "llc", "pvt", "private", "gmbh", "sa", "nv", "ag",
    "holdings", "group",
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

MONTH_END_DAY = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

# Words that carry no metric meaning and must not survive on their own as a
# predicate label.
STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "by", "with", "from",
    "and", "or", "as", "is", "was", "were", "are", "be", "been", "it", "its",
    "this", "that", "these", "those", "which", "while", "during", "over",
    "under", "than", "then", "also", "about", "into", "per", "such", "same",
    "other", "our", "their", "his", "her", "your", "we", "they", "he", "she",
    "there", "here", "however", "although", "though", "but", "not", "no",
    "more", "most", "less", "least", "some", "any", "all", "both", "each",
    "further", "moreover", "meanwhile", "despite", "amid", "amidst", "against",
    "between", "within", "including", "included", "including", "well", "very",
    "up", "down", "out", "off", "again", "only", "own", "so", "too", "s",
}


def strip_accents_and_controls(text: str) -> str:
    """Remove control/format characters that PDF extraction leaves behind."""
    return "".join(
        ch for ch in unicodedata.normalize("NFC", text)
        if unicodedata.category(ch) not in ("Cf", "Cc") or ch in "\n\t"
    )


def measurement_token(token: Optional[str]) -> Optional[Tuple[str, str]]:
    """Decide whether a word sitting after a number states what is measured.

    Returns ``(canonical_unit, family)`` or ``None``. Three shapes qualify, none
    of which needs the word to have been seen before:

    * a physical unit symbol ("MW", "kWh", "kg"),
    * a spelled-out unit ("tonnes", "days", "kilometres"),
    * any plural common noun ("visits", "students", "shipments", "beds").

    The third rule is the one that makes the extractor document-agnostic. A
    document may count anything it likes, and English marks a counted noun by
    making it plural, so the shape of the word carries the information that a
    list of allowed nouns would otherwise have to supply.
    """
    if not token:
        return None
    raw = token.strip().strip(".,;:")
    if not raw or not raw[0].isalpha():
        return None

    low = raw.lower()
    if low in UNIT_SYMBOLS:
        return low, UNIT_SYMBOLS[low]
    if low in UNIT_WORDS:
        return low, UNIT_WORDS[low]

    # Everything below is a word rather than a symbol, so it must look like an
    # ordinary English noun and must not be a word that merely follows numbers.
    if not raw.isalpha() or len(low) < 4:
        return None
    if low in NON_UNIT_FOLLOWERS or low in STOPWORDS or low in MULTIPLIERS:
        return None
    if low in MONTHS or low in CURRENCY_SYMBOLS or low in LEGAL_SUFFIXES:
        return None
    # An adverb or a participle describes the measuring, it is not the thing
    # measured, and a capitalised word mid-phrase is a name rather than a noun.
    if low.endswith("ly") or low.endswith("ing") or low.endswith("ed"):
        return None
    if not raw.islower():
        return None
    if low.endswith("s") and not low.endswith("ss") and not low.endswith("us"):
        return low, "count"
    return None


class FactNormalizer:
    """Stateless normalization helpers shared by extraction and comparison."""

    MULTIPLIERS = MULTIPLIERS  # kept as class attrs for backwards compatibility

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------
    @classmethod
    def normalize_entity(cls, raw_entity: str) -> Tuple[str, str]:
        """Canonicalise an entity mention.

        Returns ``(display_name, match_key)``. The match key strips punctuation,
        possessives, articles and legal suffixes so that "Delhivery Limited",
        "Delhivery Ltd." and "Delhivery" collapse onto one key without any
        per-company alias table.
        """
        if not raw_entity:
            return "Unknown", "unknown"

        display = re.sub(r"\s+", " ", raw_entity).strip().strip(",;:.")
        display = re.sub(r"[‘’']s\b", "", display).strip()

        tokens = re.findall(r"[A-Za-z0-9&]+", display.lower())
        tokens = [t for t in tokens if t not in {"the", "a", "an"}]
        while tokens and tokens[-1] in LEGAL_SUFFIXES:
            tokens.pop()
        if not tokens:
            tokens = re.findall(r"[a-z0-9]+", display.lower()) or ["unknown"]

        return display or "Unknown", "_".join(tokens)

    @classmethod
    def entities_compatible(cls, key_a: str, key_b: str) -> bool:
        """True when two entity keys plausibly denote the same subject.

        Equality, or one key being a token-subset of the other ("delhivery" vs
        "delhivery_express"). Generic containment only -- no curated synonyms.
        """
        if not key_a or not key_b:
            return False
        if key_a == key_b:
            return True
        ta, tb = set(key_a.split("_")), set(key_b.split("_"))
        if not ta or not tb:
            return False
        return ta.issubset(tb) or tb.issubset(ta)

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------
    @classmethod
    def normalize_predicate(
        cls, raw_label: str, acronyms: Optional[Dict[str, str]] = None
    ) -> Tuple[str, str]:
        """Turn a free-text metric phrase into ``(display_label, match_key)``.

        The key is a slug with light singularisation so that "foreign exchange
        reserves" and "foreign exchange reserve" agree. Acronyms the document
        defined itself are expanded, which is what lets "real GDP growth" in one
        report line up with "real gross domestic product growth" in another
        without either spelling being written into this file.
        """
        if not raw_label:
            return "", ""

        label = re.sub(r"\s+", " ", raw_label).strip(" -–—:;,.")

        expanded: List[str] = []
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9-]*", label):
            replacement = (acronyms or {}).get(token.upper()) if token.isupper() else None
            if replacement:
                expanded.extend(re.findall(r"[a-zA-Z][a-zA-Z0-9-]*", replacement.lower()))
            else:
                expanded.append(token.lower())

        tokens = [cls._singularise(t) for t in expanded if t not in STOPWORDS]
        # Keep order but drop repeats introduced by expansion.
        seen: set = set()
        unique = [t for t in tokens if not (t in seen or seen.add(t))]
        return label, "_".join(unique)

    @staticmethod
    def _singularise(token: str) -> str:
        if len(token) > 4 and token.endswith("ies"):
            return token[:-3] + "y"
        if len(token) > 4 and token.endswith("sses"):
            return token[:-2]
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

    @classmethod
    def predicate_match(cls, key_a: str, key_b: str) -> Tuple[Optional[str], float]:
        """How strongly two minted predicates denote the same metric.

        Metric names are read off the page, so the same quantity can arrive as
        "real gross domestic product growth" from one document and "real GDP"
        from another. Rather than a synonym table, agreement is measured by how
        much of the shorter name the longer one contains, and reported as a
        tier so that callers can be careful with the weaker ones:

        ``exact``     identical keys
        ``phrase``    at least two shared words, most of the shorter name
        ``head``      only the head noun agrees; enough to notice, not enough
                      to call two figures contradictory
        ``None``      unrelated
        """
        if not key_a or not key_b:
            return None, 0.0
        if key_a == key_b:
            return "exact", 1.0

        tokens_a = [t for t in key_a.split("_") if t]
        tokens_b = [t for t in key_b.split("_") if t]
        if not tokens_a or not tokens_b:
            return None, 0.0

        set_a, set_b = set(tokens_a), set(tokens_b)
        shared = set_a & set_b
        if not shared:
            return None, 0.0

        containment = len(shared) / min(len(set_a), len(set_b))
        same_head = tokens_a[-1] == tokens_b[-1]

        # Most of the shorter name must be present in the longer one, and the
        # two must either end in the same noun or share three words. Without
        # both conditions, "industrial sector" would match "manufacturing sector
        # accounts", which measure different things.
        if len(shared) >= 2 and containment >= 0.75 and (same_head or len(shared) >= 3):
            return "phrase", round(containment, 3)
        # A head-only match is trustworthy when one name is simply a shortened
        # form of the other ("reserves" inside "foreign exchange reserves"). It
        # is not when each name carries its own distinguishing modifier, as in
        # "headline inflation" against "core inflation", which name different
        # measures despite sharing a head noun.
        if same_head and containment == 1.0 and len(tokens_a[-1]) >= 5:
            return "head", round(containment, 3)
        return None, round(containment, 3)

    @classmethod
    def predicates_compatible(cls, key_a: str, key_b: str) -> bool:
        """Convenience wrapper: do these two names denote the same metric?"""
        tier, _ = cls.predicate_match(key_a, key_b)
        return tier is not None

    @classmethod
    def predicates_related(cls, key_a: str, key_b: str) -> bool:
        """Overlapping but distinct metrics, such as EBITDA and adjusted EBITDA."""
        if not key_a or not key_b or key_a == key_b:
            return False
        tier, _ = cls.predicate_match(key_a, key_b)
        if tier is not None:
            return False
        return bool(set(key_a.split("_")) & set(key_b.split("_")))

    # ------------------------------------------------------------------
    # Quantities
    # ------------------------------------------------------------------
    @classmethod
    def parse_numeric_value(
        cls, raw_str: str
    ) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        """Parse a quantity string into ``(mantissa, unit, normalized_value)``.

        ``normalized_value`` applies the magnitude multiplier so that
        ``₹8,142 Cr`` and ``₹81,415 Mn`` both become 8.142e10. Percentages are
        never scaled.

        Accounting parentheses mark a negative number *only* when the brackets
        wrap the numeral itself -- ``(452)`` is -452 but ``6.5 per cent (of
        GDP)`` stays positive.
        """
        if not raw_str:
            return None, None, None

        s = strip_accents_and_controls(raw_str).strip()

        # Negative if a minus sign leads, or if brackets directly wrap a number.
        is_negative = bool(re.match(r"^\s*[-−]", s))
        if re.search(r"\(\s*[^()a-zA-Z]*\d[\d,.\s]*[a-zA-Z%.]{0,8}\s*\)", s):
            is_negative = True

        unit = cls._detect_unit(s)

        # Number: first numeric group, tolerating thousands separators.
        num_match = re.search(r"\d[\d, ']*(?:\.\d+)?", s)
        if not num_match:
            return None, unit, None
        mantissa = float(num_match.group().replace(",", "").replace(" ", "").replace("'", ""))

        multiplier = cls._detect_multiplier(s[num_match.end():])

        if is_negative:
            mantissa = -mantissa

        if unit in ("%", "% of GDP", "bps"):
            normalized = mantissa
        else:
            normalized = mantissa * multiplier

        return mantissa, unit, normalized

    @classmethod
    def _detect_unit(cls, s: str) -> Optional[str]:
        low = s.lower()
        is_percent = "%" in s or bool(re.search(r"\bper\s*cent\b|\bpercent\b|\bpercentage\b", low))
        if is_percent:
            # "of GDP" changes what the percentage is a share of, so it is part
            # of the unit. Punctuation may sit between the two, as in "(of GDP)".
            return "% of GDP" if re.search(r"\bof\s+gdp\b", low) else "%"
        if re.search(r"\bbps\b|basis points", low):
            return "bps"
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol.isalpha():
                if re.search(rf"\b{re.escape(symbol)}\b", low):
                    return code
            elif symbol in low:
                return code
        # A measurement noun or symbol trailing the number names the unit.
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", s):
            classified = measurement_token(token)
            if classified:
                return classified[0]
        return None

    @classmethod
    def _detect_multiplier(cls, tail: str) -> float:
        """Find a magnitude word in the text immediately following the number."""
        # Only look at the first few tokens; "578 Cr to Rs. 127 Cr" must use Cr,
        # not something further down the sentence.
        window = tail[:24].lower()
        for token in re.findall(r"[a-z]+", window)[:2]:
            if token in MULTIPLIERS:
                return MULTIPLIERS[token]
        return 1.0

    @classmethod
    def unit_family(cls, unit: Optional[str]) -> str:
        """Coarse family used to gate numeric comparison."""
        if not unit:
            return "unknown"
        if unit in UNIT_FAMILIES:
            return UNIT_FAMILIES[unit]
        classified = measurement_token(unit)
        if classified:
            return classified[1]
        return "other"

    @classmethod
    def units_comparable(cls, unit_a: Optional[str], unit_b: Optional[str]) -> bool:
        """Two quantities may be compared numerically only within one family.

        Currencies are the deliberate exception: USD and INR are both
        ``currency`` but are *not* comparable without an FX rate, which the
        system does not have, so differing currency codes are rejected.
        """
        fam_a, fam_b = cls.unit_family(unit_a), cls.unit_family(unit_b)
        if "unknown" in (fam_a, fam_b):
            # One side lacks a unit: allow comparison but callers lower confidence.
            return True
        if fam_a != fam_b:
            return False
        if fam_a == "currency" and unit_a != unit_b:
            return False
        return True

    # ------------------------------------------------------------------
    # Time periods
    # ------------------------------------------------------------------
    @classmethod
    def normalize_time_period(cls, raw_period: Optional[str]) -> Optional[str]:
        """Map a period expression onto a canonical key.

        Produces ``YYYY-YYYY`` for fiscal years, ``YYYY-Qn`` for quarters,
        ``YYYY-MM-DD`` for month/day references and ``YYYY`` for calendar years.
        Returns ``None`` when nothing can be parsed, so callers can distinguish
        "no period" from "unparsed period".
        """
        if not raw_period:
            return None
        p = re.sub(r"\s+", " ", raw_period).strip()
        low = p.lower()

        # Quarter, optionally attached to a fiscal year: "Q4 FY24", "Q3:2024-25"
        q = re.search(r"\bq([1-4])\s*[:\-]?\s*(?:fy)?\s*(\d{4}|\d{2})(?:\s*[-/]\s*(\d{2,4}))?", low)
        if q:
            quarter = int(q.group(1))
            year = cls._expand_year(q.group(2))
            if q.group(3) or "fy" in low:
                # Fiscal quarter: label by the fiscal year's end year.
                year = year if len(q.group(2)) == 2 else year
            return f"{year}-Q{quarter}"

        # Explicit day: "31 March 2025", "March 31, 2025"
        d = re.search(r"\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b", low)
        if d and d.group(2) in MONTHS:
            return f"{int(d.group(3)):04d}-{MONTHS[d.group(2)]:02d}-{int(d.group(1)):02d}"
        d = re.search(r"\b([a-z]+)\s+(\d{1,2}),?\s+(\d{4})\b", low)
        if d and d.group(1) in MONTHS:
            return f"{int(d.group(3)):04d}-{MONTHS[d.group(1)]:02d}-{int(d.group(2)):02d}"

        # Month + year, with or without an "end-" prefix: "end-March 2025".
        m = re.search(r"\b(?:end[\s-]*(?:of\s+)?)?([a-z]+)[\s-]+(\d{4})\b", low)
        if m and m.group(1) in MONTHS:
            month = MONTHS[m.group(1)]
            year = int(m.group(2))
            day = MONTH_END_DAY[month]
            if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
                day = 29
            return f"{year:04d}-{month:02d}-{day:02d}"

        # Fiscal / spanning year: "FY2024-25", "2024-25", "FY 24/25"
        fy = re.search(r"\b(?:fy\s*)?(\d{4}|\d{2})\s*[-/–]\s*(\d{4}|\d{2})\b", low)
        if fy:
            y1 = cls._expand_year(fy.group(1))
            y2 = cls._expand_year(fy.group(2), century_of=y1)
            if 1900 <= y1 <= 2100 and y2 == y1 + 1:
                return f"{y1}-{y2}"

        # Single fiscal year: "FY25", "FY2025" -> the year it ends in.
        fy1 = re.search(r"\bfy\s*(\d{4}|\d{2})\b", low)
        if fy1:
            end = cls._expand_year(fy1.group(1))
            return f"{end - 1}-{end}"

        # Bare calendar year.
        cy = re.search(r"\b(19\d{2}|20\d{2})\b", low)
        if cy:
            return cy.group(1)

        return None

    @staticmethod
    def _expand_year(token: str, century_of: Optional[int] = None) -> int:
        if len(token) == 4:
            return int(token)
        two = int(token)
        if century_of is not None:
            base = (century_of // 100) * 100
            candidate = base + two
            if candidate < century_of:
                candidate += 100
            return candidate
        return 2000 + two if two < 80 else 1900 + two

    @classmethod
    def periods_overlap(cls, a: Optional[str], b: Optional[str]) -> bool:
        """True when two canonical periods refer to the same span of time.

        A fiscal year ``2024-2025`` contains the date ``2025-03-31`` only if the
        document says so, which the system cannot know, so containment is *not*
        treated as a match. This keeps "end-December 2024" and "FY2024-25"
        distinct, which is what makes the contextual-difference case work.
        """
        if not a or not b:
            return False
        return a == b

    # ------------------------------------------------------------------
    # Descriptive helpers
    # ------------------------------------------------------------------
    @classmethod
    def describe_value(cls, value: Optional[float], unit: Optional[str]) -> str:
        """Human-readable rendering of a normalized value."""
        if value is None:
            return "n/a"
        if unit in ("%", "% of GDP", "bps"):
            return f"{value:g}{'%' if unit != 'bps' else ' bps'}"
        magnitude = abs(value)
        for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
            if magnitude >= threshold:
                return f"{value / threshold:,.4g}{suffix} {unit or ''}".strip()
        return f"{value:,.4g} {unit or ''}".strip()
