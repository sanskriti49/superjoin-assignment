"""Document-agnostic fact extraction.

The extractor knows nothing about India, logistics, or any other subject. It
looks for one thing: a **measured quantity** (a number carrying a currency, a
magnitude word, a percent sign or a measurement noun) and then reads the
surrounding text to answer three questions:

    what is being measured   -> predicate
    what is it measured for  -> subject
    when / on what basis     -> time period, scope, qualifier

Because predicates are minted from the words on the page, the schema grows by
itself as new kinds of documents arrive; nothing needs to be added here to
support a new domain.

Every fact carries the exact character span of its evidence inside the stored
canonical page text, and a fact whose quote is not a literal substring of that
text is discarded rather than down-weighted.
"""

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.pipeline.llm_provider import LLMProvider
from app.pipeline.normalizer import (
    COUNT_UNIT_HINTS,
    MULTIPLIERS,
    STOPWORDS,
    FactNormalizer,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lexical building blocks. All of these describe English reporting prose, not
# any particular document.
# ---------------------------------------------------------------------------

# Single letter magnitudes ("b", "m", "k") collide with list markers and
# initials in extracted PDF text, so only multi-letter forms are matched here.
_MAGNITUDE_ALT = "|".join(
    sorted((re.escape(word) for word in MULTIPLIERS if len(word) > 1), key=len, reverse=True)
)
_UNIT_ALT = "|".join(sorted((re.escape(word) for word in COUNT_UNIT_HINTS), key=len, reverse=True))

QUANTITY_RE = re.compile(
    rf"""
    (?P<lead>[-−(])?\s*
    (?P<cur>US\$|U\.S\.\$|Rs\.?|INR|USD|EUR|GBP|CNY|JPY|AED|SGD|[₹$€£¥])?\s*
    (?<![A-Za-z0-9.])
    (?P<num>\d{{1,3}}(?:,\d{{2,3}})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<mag>{_MAGNITUDE_ALT})?\b
    \s*
    (?P<pct>per\s*cent|percent|%|bps|basis\s+points)?
    \s*
    (?:(?P<unit>{_UNIT_ALT})\b)?
    (?P<close>\s*\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Reporting verbs that separate a metric phrase from its value. In "X stood at
# N", everything before the verb is the metric; everything after is the measure.
VERB_LINK_RE = re.compile(
    r"\b(?:stood|stands|standing|was|were|is|are|be|been|being|remained|remains|"
    r"reached|reaching|touched|touching|totall?ed|amounted|aggregated|aggregating|"
    r"rose|rising|grew|grown|grow|grows|growing|increased|increasing|declined|"
    r"declining|decreased|decreasing|fell|falling|dropped|dropping|moderated|"
    r"moderating|eased|easing|softened|softening|improved|improving|expanded|"
    r"expanding|contracted|contracting|averaged|averaging|recorded|recording|"
    r"reported|reporting|registered|registering|estimated|projected|forecast|"
    r"forecasted|expected|placed|contained|clocked|posted|posting|accelerated|"
    r"decelerated|surged|jumped|slipped|widened|narrowed|crossed|crossing|hit|"
    r"achieved|delivered|generated|stayed|held|printed|settled|closed|ended)\b",
    re.IGNORECASE,
)

# Weaker links used only when no reporting verb is present on the line.
PREP_LINK_RE = re.compile(
    r"\b(?:at|of|to|by|from|worth|around|about|approximately|nearly|over|above|"
    r"below|under|versus|vs)\b",
    re.IGNORECASE,
)

# Trailing verb forms are trimmed from a label because an English noun phrase
# ends in its head noun, never in a reporting verb.
TRAILING_VERB_RE = re.compile(
    r"^(?:" + VERB_LINK_RE.pattern[2:-2] + r"|driven|led|aided|supported|helped|"
    r"boosted|weighed|dragged|compared|including|excluding|being|having)$",
    re.IGNORECASE,
)

# Words that open a new clause and therefore end the metric phrase to their left.
CLAUSE_OPENERS = {
    "although", "though", "while", "whereas", "however", "moreover", "meanwhile",
    "despite", "notwithstanding", "because", "if", "when", "unless", "thus",
    "hence", "therefore", "furthermore", "nonetheless", "nevertheless",
}

# Period expressions, longest-first so "Q4 FY24" wins over "FY24".
_MONTH_ALT = ("january|february|march|april|may|june|july|august|september|october|"
              "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
TIME_RE = re.compile(
    rf"""
    (?:as\s+(?:at|on|of)\s+)?
    (?:
        Q[1-4]\s*[:\-]?\s*(?:FY)?\s*\d{{2,4}}(?:\s*[-/]\s*\d{{2,4}})?
      | (?:end[\s-]*(?:of\s+)?)?(?:{_MONTH_ALT})[\s,-]+\d{{4}}
      | \d{{1,2}}\s+(?:{_MONTH_ALT})[\s,]+\d{{4}}
      | (?:{_MONTH_ALT})\s+\d{{1,2}},\s*\d{{4}}
      | FY\s?\d{{4}}\s*[-/]\s*\d{{2,4}}
      | FY\s?\d{{2,4}}
      | (?:19|20)\d{{2}}\s*[-–/]\s*\d{{2,4}}
      | (?:19|20)\d{{2}}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Reporting-basis cues. These are conventions of financial and statistical
# publishing in general, not facts about any specific document.
QUALIFIER_CUES: Tuple[Tuple[str, str], ...] = (
    (r"second\s+advance\s+estimate|2nd\s+advance\s+estimate|\bSAE\b", "Second Advance Estimate"),
    (r"first\s+advance\s+estimate|1st\s+advance\s+estimate|\bFAE\b", "First Advance Estimate"),
    (r"advance\s+estimate", "Advance Estimate"),
    (r"revised\s+estimate|\(RE\)|\[RE\]", "Revised Estimate"),
    (r"budget\s+estimate|\(BE\)|\[BE\]", "Budget Estimate"),
    (r"provisional", "Provisional"),
    (r"preliminary", "Preliminary"),
    (r"restated", "Restated"),
    (r"pro[\s-]?forma", "Pro Forma"),
    (r"seasonally\s+adjust\w*", "Seasonally Adjusted"),
    (r"annualis\w+|annualiz\w+", "Annualised"),
    (r"constant\s+currency", "Constant Currency"),
    (r"like[\s-]for[\s-]like", "Like for Like"),
    (r"trailing\s+twelve\s+months|\bTTM\b", "Trailing Twelve Months"),
    (r"\bunaudited\b", "Unaudited"),
    (r"\baudited\b", "Audited"),
    (r"\bprojected\b|\bforecast\w*\b", "Projection"),
    (r"\bestimated\b|\bestimates?\b", "Estimate"),
)

SCOPE_CUES: Tuple[Tuple[str, str], ...] = (
    # An exclusion changes what the figure covers, so two figures that differ
    # only by an exclusion are not in conflict.
    (r"\bexcluding\b|\bexclusive\s+of\b|\bnet\s+of\b|\bex[- ](?=[A-Z])", "Excluding items"),
    (r"\bincluding\b|\binclusive\s+of\b", "Including items"),
    (r"\bconsolidated\b", "Consolidated"),
    (r"\bstandalone\b", "Standalone"),
    (r"\bsegment(?:al)?\b", "Segment"),
    (r"\badjusted\b", "Adjusted"),
    (r"\bgeneral\s+government\b", "General Government"),
    (r"\bcentral\s+government\b|\bunion\s+government\b", "Central Government"),
    (r"\bstate\s+government", "State Governments"),
    (r"\bgross\b(?=\s+\w)", None),   # descriptive only; folded into the predicate
    (r"\bper\s+capita\b", "Per Capita"),
    (r"\brural\b", "Rural"),
    (r"\burban\b", "Urban"),
    (r"\bdomestic\b", "Domestic"),
    (r"\bglobal\b|\bworldwide\b", "Global"),
)

PROPER_NOUN_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z&.\-]*(?:\s+(?:of|for|and|the)\s+)?){1,5}[A-Z][A-Za-z&.\-]*\b|\b[A-Z][A-Za-z&.\-]{2,}\b"
)
POSSESSIVE_RE = re.compile(r"\b((?:[A-Z][A-Za-z&.\-]+\s*){1,4})[‘’']s\b")
ACRONYM_DEF_RE = re.compile(r"\b((?:[A-Z][A-Za-z&.\-]+\s+){1,5})\(([A-Z]{2,6})\)")

FOOTNOTE_RE = re.compile(r"^\(\d{1,2}\)$|^\[\d{1,2}\]$")

# Paragraph and section numbering that publishers put at the start of a line
# ("I.9", "3.2.1", "II.6.14"). It is navigation, never part of a metric name.
SECTION_MARKER_RE = re.compile(r"^\s*(?:[IVXLCDM]+|\d{1,2})(?:\.\d{1,3})+\.?\s+", re.IGNORECASE)

# A label made only of magnitude, currency or unit words names no metric.
MEASURE_ONLY = set(MULTIPLIERS) | set(COUNT_UNIT_HINTS) | {
    "rs", "inr", "usd", "eur", "gbp", "cent", "percent", "percentage", "bps",
    "basis", "point", "points", "rupee", "rupees", "dollar", "dollars",
}

# Reason codes recorded when a candidate quantity does not become a fact.
REJECT_BARE_NUMBER = "bare_number_without_unit"
REJECT_LOOKS_LIKE_YEAR = "number_is_a_year_not_a_measure"
REJECT_SECTION_NUMBER = "section_number_not_a_measure"
REJECT_NO_LABEL = "no_metric_label_in_context"
REJECT_LABEL_STOPWORDS = "label_is_only_function_words"
REJECT_UNPARSEABLE = "value_could_not_be_normalized"
REJECT_GROUNDING = "evidence_quote_not_found_in_page_text"
REJECT_DUPLICATE = "duplicate_of_higher_confidence_fact"
REJECT_LLM_UNGROUNDED = "llm_quote_not_present_in_page_text"
REJECT_LABEL_IS_UNIT = "label_names_a_unit_not_a_metric"

# Rejections that are expected on every page (page numbers, list markers, dates
# in running text). They are counted but not stored one by one, because a
# hundred thousand of them would bury the diagnostics that matter.
ROUTINE_FILTERS = {REJECT_BARE_NUMBER, REJECT_LOOKS_LIKE_YEAR, REJECT_SECTION_NUMBER}

MAX_LABEL_TOKENS = 8
MIN_CONFIDENCE = 0.30
MAX_CONFIDENCE = 0.95


@dataclass
class DocumentProfile:
    """Document-level context learned from the document itself.

    Used only as a fallback when a sentence does not name its own subject or
    period. Learned by frequency, never configured.
    """

    default_subject: str = "Unknown"
    default_period_raw: Optional[str] = None
    subject_threshold: int = 3
    acronyms: Dict[str, str] = field(default_factory=dict)
    subject_counts: Counter = field(default_factory=Counter)
    period_counts: Counter = field(default_factory=Counter)

    def finalize(self, fallback_title: str = "") -> "DocumentProfile":
        """Choose the entity the document is about.

        Raw frequency alone picks the wrong answer on a slide deck, where
        annotations like "YoY" outnumber the company name. Two generic
        corrections fix it: a multi-word name outweighs a bare token, and a name
        that also appears in the document's own title outweighs everything,
        because a title names its subject.
        """
        title_tokens = {
            token for token in re.findall(r"[a-z]+", fallback_title.lower())
            if len(token) > 3
        }

        def weight(name: str, count: int) -> float:
            words = name.split()
            score = count * (1.0 + 1.2 * (len(words) - 1))
            if title_tokens and any(word.lower() in title_tokens for word in words):
                # A document's title names what the document is about, which
                # outranks how often an incidental term happens to appear.
                score *= 10.0
            return score

        ranked = sorted(
            ((weight(name, count), name) for name, count in self.subject_counts.items()),
            reverse=True,
        )
        if ranked:
            self.default_subject = ranked[0][1]
            # An alternative subject must be reasonably well established in the
            # document before a single sentence may override the dominant one.
            self.subject_threshold = max(3, self.subject_counts.most_common(1)[0][1] // 10)
        elif fallback_title:
            words = [word for word in fallback_title.split()
                     if word.lower() not in DocumentProfiler.NON_ENTITY]
            self.default_subject = " ".join(words).title() or fallback_title

        if self.period_counts:
            self.default_period_raw = self.period_counts.most_common(1)[0][0]
        return self


class DocumentProfiler:
    """Learns the dominant entity, period and acronyms of a document."""

    # Words that are capitalised for typographic reasons rather than because
    # they name an entity.
    NON_ENTITY = {
        "the", "this", "that", "these", "those", "it", "in", "on", "at", "for",
        "and", "or", "but", "however", "although", "while", "during", "as",
        "table", "figure", "fig", "figs", "chart", "annex", "annexure", "appendix", "chapter",
        "section", "note", "notes", "page", "source", "sources", "total",
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "report", "annual", "quarterly", "statement", "statements", "results",
        # Words that name a genre of document rather than a subject. Without
        # these, a filename-derived title makes "Prospectus" or "Presentation"
        # look like the entity the document is about.
        "prospectus", "presentation", "survey", "review", "consultation",
        "article", "excerpt", "earnings", "filing", "deck", "update",
        "overview", "summary", "memorandum", "circular", "bulletin", "brief",
        "briefing", "paper", "study", "analysis", "outlook", "edition",
        "volume", "disclosure", "disclosures", "highlights", "appendix",
        # Technical models, algorithms and frameworks that are components, not entities
        "cnn", "rnn", "gru", "lstm", "svm", "mlp", "knn", "ann", "rf", "dt", "nb", "gmm", "hmm",
        "ast", "api", "url", "cpu", "gpu", "tpu", "ram", "os", "pc", "gui",
        # Evaluation metrics
        "eer", "far", "frr", "acc", "auc", "roc", "f1", "fnmr", "fmr", "ania", "anga",
        # Academic & publication terminology
        "ieee", "acm", "elsevier", "sciencedirect", "springer", "proceedings", "conference",
        "symposium", "workshop", "journal", "international", "transactions",
        "author", "authors", "university", "department", "school", "college", "institute",
        "et", "al", "beijing", "china", "hong", "kong",
    }

    @classmethod
    def profile_pages(cls, pages: Iterable[Dict[str, Any]], title: str = "") -> DocumentProfile:
        profile = DocumentProfile()
        if title:
            title_words = [w for w in title.split() if w.lower() not in cls.NON_ENTITY and len(w) > 2]
            if len(title_words) >= 2:
                title_cand = " ".join(title_words[:2]).title()
                profile.subject_counts[title_cand] += 15
            elif len(title_words) == 1:
                profile.subject_counts[title_words[0].title()] += 10
        for page in pages:
            cls.observe(profile, page.get("text", ""))
        return profile.finalize(fallback_title=title)

    @classmethod
    def observe(cls, profile: DocumentProfile, text: str) -> None:
        if not text:
            return

        for match in ACRONYM_DEF_RE.finditer(text):
            expansion = match.group(1).strip()
            acronym = match.group(2).strip()
            if 1 <= len(expansion.split()) <= 6:
                profile.acronyms.setdefault(acronym, expansion)

        for match in POSSESSIVE_RE.finditer(text):
            candidate = cls._clean_entity(match.group(1))
            if candidate:
                profile.subject_counts[candidate] += 3

        for match in PROPER_NOUN_RE.finditer(text):
            # A single capitalised word at the start of a sentence is capitalised
            # by orthography, not because it names an entity. Multi-word phrases
            # and acronyms are kept wherever they appear.
            if cls._is_sentence_initial(text, match.start()) and " " not in match.group(0) \
                    and not match.group(0).isupper():
                continue
            candidate = cls._clean_entity(match.group(0))
            if candidate:
                profile.subject_counts[candidate] += 1

        for match in TIME_RE.finditer(text):
            period = match.group(0).strip()
            if FactNormalizer.normalize_time_period(period):
                profile.period_counts[period.lower()] += 1

    @staticmethod
    def _is_sentence_initial(text: str, position: int) -> bool:
        before = text[:position].rstrip()
        return not before or before[-1] in ".!?\n"

    # Internal capitals mark an abbreviation of a comparison ("YoY", "QoQ"),
    # not the name of an entity.
    ABBREVIATION_SHAPE = re.compile(r"^[A-Z][a-z]+[A-Z]")

    @classmethod
    def _clean_entity(cls, raw: str) -> Optional[str]:
        cleaned = re.sub(r"\s+", " ", raw).strip(" .,;:-")
        if len(cleaned) < 3 or len(cleaned) > 60:
            return None
        tokens = cleaned.split()
        if not tokens:
            return None
        if any(len(token) <= 1 for token in tokens):
            return None
        if any(token.lower() in {"et", "al", "fig", "figs"} for token in tokens):
            return None
        if all(token.lower() in cls.NON_ENTITY for token in tokens):
            return None
        if any(cls.ABBREVIATION_SHAPE.match(token) for token in tokens):
            return None
        if tokens[0].lower() in cls.NON_ENTITY and len(tokens) == 1:
            return None
        # Drop leading function words so "The Reserve Bank" and "Reserve Bank"
        # do not compete as separate entities.
        while tokens and tokens[0].lower() in {"the", "a", "an", "in", "of", "and"}:
            tokens.pop(0)
        if not tokens:
            return None
        return " ".join(tokens)


@dataclass
class Candidate:
    """One quantity found on a page, before it is accepted or rejected."""

    line: str
    line_start: int
    match: re.Match
    prev_line: Optional[str]
    next_line: Optional[str]
    line_index: int = 0
    all_lines: Optional[List[Tuple[str, int]]] = None


class FactExtractionPipeline:
    """Turns canonical page text into grounded facts plus rejection diagnostics."""

    @classmethod
    def extract_from_page(
        cls,
        doc_id: str,
        page_number: int,
        page_text: str,
        profile: Optional[DocumentProfile] = None,
        doc_name: str = "",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Extract facts from one page.

        Returns ``(facts, issues)``. Issues record every quantity the extractor
        saw but chose not to keep, with the reason. They are surfaced through
        the API so the failure modes of the system are inspectable rather than
        invisible.
        """
        facts: List[Dict[str, Any]] = []
        issues: List[Dict[str, Any]] = []

        if not page_text or len(page_text.strip()) < 20:
            return facts, issues

        profile = profile or DocumentProfile()
        lines = cls._lines_with_offsets(page_text)

        for index, (line, line_start) in enumerate(lines):
            prev_line = lines[index - 1][0] if index > 0 else None
            next_line = lines[index + 1][0] if index + 1 < len(lines) else None

            for match in QUANTITY_RE.finditer(line):
                candidate = Candidate(line, line_start, match, prev_line, next_line, index, lines)
                fact, issue = cls._evaluate(candidate, doc_id, page_number, page_text, profile)
                if fact:
                    facts.append(fact)
                elif issue:
                    issues.append(issue)

        semantic_facts = cls._extract_semantic_facts(doc_id, page_number, page_text, lines, profile)
        facts.extend(semantic_facts)

        facts, dropped = cls._dedupe(facts)
        issues.extend(dropped)

        llm_facts, llm_issues = cls._extract_with_llm(doc_id, page_number, page_text, doc_name, profile)
        facts.extend(llm_facts)
        issues.extend(llm_issues)

        return facts, issues

    # ------------------------------------------------------------------
    # Candidate evaluation
    # ------------------------------------------------------------------
    @classmethod
    def _evaluate(
        cls,
        candidate: Candidate,
        doc_id: str,
        page_number: int,
        page_text: str,
        profile: DocumentProfile,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        match = candidate.match
        groups = match.groupdict()
        raw_quantity = match.group(0).strip()
        # Drop a stray bracket the pattern picked up without its partner, so the
        # stored value string reads the way it does on the page.
        if raw_quantity.count("(") != raw_quantity.count(")"):
            raw_quantity = raw_quantity.strip("()").strip()

        def reject(reason: str) -> Tuple[None, Dict[str, Any]]:
            return None, {
                "document_id": doc_id,
                "page_number": page_number,
                "candidate_text": raw_quantity[:120],
                "context_snippet": candidate.line[:300],
                "reason_code": reason,
                "detail": cls._explain(reason, raw_quantity),
                "is_routine_filter": reason in ROUTINE_FILTERS,
            }

        has_measure = bool(groups["cur"] or groups["mag"] or groups["pct"] or groups["unit"])
        trailing_unit = None
        next_unit_len = 0
        if not has_measure:
            window = candidate.line[match.end(): min(len(candidate.line), match.end() + 60)]
            m_unit = re.match(rf"^\s*(?:[A-Za-z0-9&/–-]+\s+){{0,3}}(?P<u_word>{_UNIT_ALT})\b", window, re.IGNORECASE)
            if m_unit:
                trailing_unit = m_unit.group("u_word").lower()
                has_measure = True
            else:
                m_range = re.match(rf"^\s*(?:to|[-–])\s*\d+(?:\.\d+)?\s*(?:[A-Za-z0-9&/–-]+\s+){{0,2}}(?P<u_word>{_UNIT_ALT}|per\s*cent|percent|%)\b", window, re.IGNORECASE)
                if m_range:
                    u_word = m_range.group("u_word").lower()
                    trailing_unit = "%" if u_word in {"percent", "per cent", "%"} else u_word
                    has_measure = True
                elif candidate.next_line and len(candidate.line[match.end():].strip()) <= 1:
                    m_next = re.match(rf"^\s*(?:[A-Za-z0-9&/–-]+\s+){{0,2}}(?P<u_word>{_UNIT_ALT}|per\s*cent|percent|%)\b", candidate.next_line[:60], re.IGNORECASE)
                    if m_next:
                        u_word = m_next.group("u_word").lower()
                        trailing_unit = "%" if u_word in {"percent", "per cent", "%"} else u_word
                        has_measure = True
                        next_unit_len = m_next.end()

        if not has_measure:
            return reject(REJECT_BARE_NUMBER)

        number_text = groups["num"].replace(",", "")
        # A four digit number with no decimals that reads as a year is a date
        # reference, not a measurement, unless a currency makes it a value.
        if (not groups["cur"] and not groups["pct"] and "." not in number_text
                and len(number_text) == 4 and 1900 <= int(number_text) <= 2100 and not trailing_unit):
            return reject(REJECT_LOOKS_LIKE_YEAR)

        # Decimal numbers like "6.2 Agents and Architecture" or "2.1 Gap Identified"
        # at the start of a clause/line or table of contents followed by a Capitalized title
        # word are outline navigation, not discrete counted quantities.
        unit_word = trailing_unit or groups.get("unit")
        if not groups["cur"] and not groups["pct"] and "." in number_text and unit_word:
            sub = candidate.line[max(0, match.start() - 6): min(len(candidate.line), match.end() + 25)]
            if re.search(r"(?:^|[;:\n•|]|\s{2,}|\b\d+\s+)\s*(?:[IVXLCDM]+|\d{1,2})\.\d{1,3}\s+[A-Z]", sub):
                return reject(REJECT_SECTION_NUMBER)

        label, strategy, label_span = cls._find_label(candidate, prefer_prev=bool(next_unit_len > 0))
        if not label and trailing_unit:
            label = trailing_unit
            strategy = "trailing"
            label_span = (match.start(), min(len(candidate.line), match.end() + 80))
        if not label:
            return reject(REJECT_NO_LABEL)

        predicate_label, predicate_key = FactNormalizer.normalize_predicate(
            label, acronyms=profile.acronyms
        )
        if not predicate_key:
            return reject(REJECT_LABEL_STOPWORDS)
        if all(token in MEASURE_ONLY for token in predicate_key.split("_")):
            return reject(REJECT_LABEL_IS_UNIT)

        mantissa, unit, normalized = FactNormalizer.parse_numeric_value(raw_quantity)
        if mantissa is None:
            return reject(REJECT_UNPARSEABLE)
        if unit is None and groups["unit"]:
            unit = groups["unit"].lower()
        if unit is None and trailing_unit:
            unit = trailing_unit
            if unit == "%":
                normalized = mantissa

        quote, quote_start, quote_end = cls._build_evidence(
            candidate, label_span, page_text, strategy=strategy, next_unit_len=next_unit_len
        )
        if quote != page_text[quote_start:quote_end]:
            return reject(REJECT_GROUNDING)

        subject_raw, subject_explicit = cls._find_subject(
            candidate, label_span, predicate_key, profile
        )
        subject_display, subject_key = FactNormalizer.normalize_entity(subject_raw)

        anchor = (candidate.line_start + candidate.match.start()) - quote_start
        period_raw, period_explicit = cls._find_period(quote, anchor, profile)
        period_key = FactNormalizer.normalize_time_period(period_raw)

        qualifier = cls._match_cue(quote, QUALIFIER_CUES)
        scope = cls._match_cue(quote, SCOPE_CUES)

        confidence, signals = cls._score(
            unit=unit,
            period_explicit=period_explicit,
            subject_explicit=subject_explicit,
            strategy=strategy,
            predicate_key=predicate_key,
        )

        fingerprint = f"{doc_id}|{page_number}|{predicate_key}|{normalized}|{period_key}|{quote_start}"
        fact_id = f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}"

        return {
            "id": fact_id,
            "document_id": doc_id,
            "subject": subject_display,
            "subject_normalized": subject_key,
            "predicate": predicate_key,
            "predicate_label": predicate_label.title() if predicate_label.islower() else predicate_label,
            "value_raw": raw_quantity,
            "value_numeric": normalized,
            "value_text": str(mantissa),
            "unit": unit,
            "unit_family": FactNormalizer.unit_family(unit),
            "time_period": period_raw,
            "time_period_normalized": period_key,
            "scope": scope,
            "qualifier": qualifier,
            "confidence": confidence,
            "evidence_quote": quote,
            "evidence_page": page_number,
            "char_start": quote_start,
            "char_end": quote_end,
            "extraction_method": f"pattern:{strategy}",
            "extraction_metadata": {
                "label_source": strategy,
                "raw_label": label,
                "mantissa": mantissa,
                "confidence_signals": signals,
                "period_inferred_from_document": not period_explicit,
                "subject_inferred_from_document": not subject_explicit,
            },
        }, None

    # ------------------------------------------------------------------
    # Label discovery
    # ------------------------------------------------------------------
    @classmethod
    def _find_table_columns(cls, lines: List[Tuple[str, int]], line_idx: int, expected_count: int) -> Optional[List[str]]:
        for b in range(1, 6):
            if line_idx - b < 0:
                break
            prev = lines[line_idx - b][0].strip()
            if not prev:
                continue
            cols = [w.strip(" ,;|:") for w in re.findall(r"[A-Za-z0-9_+-]+(?:\([A-Za-z0-9%_+-]+\))?", prev) if w.strip(" ,|:")]
            if len(cols) == expected_count and all(not c.isdigit() for c in cols):
                return cols
            if len(cols) >= expected_count and all(not c.isdigit() for c in cols[:expected_count]):
                return cols[:expected_count]
            if any(w.endswith((".", "!", "?")) for w in prev.split()) and len(prev.split()) > 8:
                break
        return None

    @classmethod
    def _find_label(cls, candidate: Candidate, prefer_prev: bool = False) -> Tuple[Optional[str], str, Tuple[int, int]]:
        """Locate the metric phrase for a quantity.

        Five generic layouts are tried, in decreasing order of reliability:

        1. ``table_cell`` a 2D data grid where column and row headers combine
        2. ``prose``      "forex reserves stood at US$ 668.3 billion"
        3. ``trailing``   "740 Mn express parcel shipments"
        4. ``next_line``  a KPI tile whose caption sits under the number
        5. ``prev_line``  a table row whose header sits above the number
        """
        match = candidate.match
        line = candidate.line
        prefix = line[: match.start()]
        suffix = line[match.end():]

        # 1. 2D Table Row Check: If line contains multiple quantities in a grid
        all_quantities = list(QUANTITY_RE.finditer(line))
        if len(all_quantities) >= 2 and candidate.all_lines and candidate.line_index is not None:
            first_q = all_quantities[0]
            row_header = line[:first_q.start()].strip()
            row_header = SECTION_MARKER_RE.sub("", row_header).strip(" -–—:;,•")
            cols = cls._find_table_columns(candidate.all_lines, candidate.line_index, len(all_quantities))
            if cols and len(cols) == len(all_quantities):
                try:
                    col_idx = [q.start() for q in all_quantities].index(match.start())
                    col_name = cols[col_idx]
                    combined = f"{col_name} {row_header}".strip() if row_header else col_name
                    clean_c = cls._clean_label(combined)
                    if clean_c:
                        return clean_c, "table_cell", (match.start(), min(len(line), match.end() + 60))
                except (ValueError, IndexError):
                    pass
            elif row_header and len(row_header) >= 2 and not row_header.isdigit():
                clean_r = cls._clean_label(row_header)
                if clean_r:
                    return clean_r, "table_row", (0, min(len(line), match.end() + 60))

        label = cls._clean_label(cls._label_from_prefix(prefix))
        if label:
            return label, "prose", (max(0, match.start() - 220), match.end())

        label = cls._clean_label(cls._label_from_suffix(suffix))
        if label:
            return label, "trailing", (match.start(), min(len(line), match.end() + 160))

        if prefer_prev and candidate.prev_line:
            label = cls._clean_label(cls._label_from_neighbour(candidate.prev_line))
            if label:
                return label, "prev_line", (match.start(), match.end())

        if candidate.next_line:
            label = cls._clean_label(cls._label_from_neighbour(candidate.next_line))
            if label:
                return label, "next_line", (match.start(), match.end())

        if not prefer_prev and candidate.prev_line:
            label = cls._clean_label(cls._label_from_neighbour(candidate.prev_line))
            if label:
                return label, "prev_line", (match.start(), match.end())

        # "880 Mn parcels" names what is counted in the quantity itself.
        unit_noun = match.groupdict().get("unit")
        if unit_noun:
            return unit_noun, "unit_noun", (match.start(), min(len(line), match.end() + 80))

        return None, "none", (match.start(), match.end())

    @staticmethod
    def _label_from_prefix(prefix: str) -> str:
        """Take the metric phrase sitting before the reporting verb.

        In "real GDP growth moderated to 6.5 per cent", the subject noun phrase
        runs from the start of the clause to the *first* reporting verb. Cutting
        at the first verb rather than the last is what keeps "headline inflation"
        from becoming "headline inflation moderated to an average of".
        """
        clause = re.split(r"[.;:]\s|•", prefix)[-1]
        clause = SECTION_MARKER_RE.sub("", clause.lstrip())
        clause = re.sub(r"\([^)]*\)", " ", clause)
        clause = QUANTITY_RE.sub(" ", clause)
        clause = TIME_RE.sub(" ", clause)

        # "The company reported that throughput reached N" puts the metric after
        # the complementizer, not before the reporting verb.
        complementizer = list(re.finditer(r"\bthat\b", clause, re.IGNORECASE))
        if complementizer:
            clause = clause[complementizer[-1].end():]

        verb = VERB_LINK_RE.search(clause)
        if verb:
            head = clause[: verb.start()]
        else:
            prep = PREP_LINK_RE.search(clause)
            head = clause[: prep.start()] if prep else clause

        tokens = re.findall(r"[A-Za-z][A-Za-z0-9&/-]*", head)
        # Start the phrase after the last clause opener, so a subordinate clause
        # does not drag the main clause's words into the label.
        for index in range(len(tokens) - 1, -1, -1):
            if tokens[index].lower() in CLAUSE_OPENERS:
                tokens = tokens[index + 1:]
                break
        return " ".join(tokens)

    @staticmethod
    def _label_from_suffix(suffix: str) -> str:
        """Take the metric phrase sitting after the number on the same line."""
        clause = re.split(r"[.;:]\s|,\s+(?:which|while|and)\b|•", suffix)[0]
        clause = re.sub(r"\(\d+\)|\[\d+\]", " ", clause)
        clause = TIME_RE.sub(" ", clause)
        clause = re.split(r"\b(?:in|for|during|as at|as of|vs|versus|from|compared)\b", clause, maxsplit=1)[0]
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9&/-]*", clause)
        return " ".join(tokens[:MAX_LABEL_TOKENS])

    @staticmethod
    def _label_from_neighbour(neighbour: str) -> str:
        """Take a caption line adjacent to a standalone number."""
        if len(neighbour) > 90:
            return ""
        cleaned = re.sub(r"\(\d+\)|\[\d+\]", " ", neighbour)
        cleaned = QUANTITY_RE.sub(" ", cleaned)
        cleaned = TIME_RE.sub(" ", cleaned)
        cleaned = re.split(r"[/|]", cleaned)[0]
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9&-]*", cleaned)
        return " ".join(tokens[:MAX_LABEL_TOKENS])

    @staticmethod
    def _clean_label(label: str) -> Optional[str]:
        """Trim a candidate phrase down to a usable metric name, or reject it."""
        if not label:
            return None
        tokens = [token for token in label.split() if token]

        # A sentence adverb ("Similarly", "Notably") introduces the clause and is
        # not part of the metric's name.
        while len(tokens) > 1 and tokens[0].lower().endswith("ly") and len(tokens[0]) > 4:
            tokens.pop(0)

        # An English noun phrase ends in its head noun. Anything ending in a
        # reporting verb is a fragment, not a metric name.
        while tokens and (tokens[-1].lower() in STOPWORDS or TRAILING_VERB_RE.match(tokens[-1])):
            tokens.pop()
        while tokens and (tokens[0].lower() in STOPWORDS or TRAILING_VERB_RE.match(tokens[0])):
            tokens.pop(0)
        if not tokens:
            return None

        content = [token for token in tokens if token.lower() not in STOPWORDS and len(token) > 1]
        if not content:
            return None
        if len(content) == 1 and len(content[0]) < 3:
            return None

        # Cap on meaningful words: take the ones closest to the reporting verb or number (from the end)
        kept: List[str] = []
        for token in reversed(tokens):
            if token.lower() not in STOPWORDS and len(token) > 1:
                kept.append(token)
                if len(kept) == MAX_LABEL_TOKENS:
                    break
        kept.reverse()

        while kept and kept[0].lower() in STOPWORDS:
            kept.pop(0)
        while kept and kept[-1].lower() in STOPWORDS:
            kept.pop()

        if not kept:
            return None

        NON_METRIC_LABELS = {
            "table", "figure", "fig", "chart", "work", "fact", "lengths", "author", "authors",
            "section", "page", "step", "steps", "id", "formula", "change", "sequence",
            "intruders", "missing", "data", "noise", "see", "show", "shows", "shown", "item"
        }
        if all(token.lower() in NON_METRIC_LABELS or token.isdigit() for token in kept):
            return None

        return " ".join(kept)

    # ------------------------------------------------------------------
    # Subject, period, cues
    # ------------------------------------------------------------------
    @classmethod
    def _find_subject(
        cls,
        candidate: Candidate,
        label_span: Tuple[int, int],
        label_key: str,
        profile: DocumentProfile,
    ) -> Tuple[str, bool]:
        """Name the entity the measurement is about.

        A possessive ("India's reserves") is the strongest signal. Otherwise the
        sentence is checked for an entity the document has already established
        elsewhere, which avoids treating an incidental capitalised word as the
        subject. Failing both, the document's dominant entity is used and the
        fact is flagged as having an inferred subject.
        """
        window = candidate.line[max(0, label_span[0]): candidate.match.end()]
        label_tokens = set(label_key.split("_"))

        possessive = list(POSSESSIVE_RE.finditer(window))
        if possessive:
            cleaned = DocumentProfiler._clean_entity(possessive[-1].group(1))
            if cleaned:
                return profile.acronyms.get(cleaned, cleaned), True

        # Only an entity the document has established elsewhere may override the
        # document's own subject, and it must not be part of the metric name --
        # "CPI" in "CPI inflation" names the measure, not the thing measured.
        known: List[Tuple[int, str]] = []
        for match in PROPER_NOUN_RE.finditer(window):
            cleaned = DocumentProfiler._clean_entity(match.group(0))
            if not cleaned:
                continue
            if any(word.lower() in label_tokens for word in cleaned.split()):
                continue
            expanded = profile.acronyms.get(cleaned, cleaned)
            if " " not in expanded and expanded == cleaned:
                continue  # bare single-word candidate with no expansion
            weight = profile.subject_counts.get(cleaned, 0) + profile.subject_counts.get(expanded, 0)
            if weight >= profile.subject_threshold:
                known.append((weight, expanded))
        if known:
            return max(known)[1], True

        return profile.default_subject, False

    @classmethod
    def _find_period(cls, quote: str, anchor: int, profile: DocumentProfile) -> Tuple[Optional[str], bool]:
        """Pick the period expression nearest the measured value.

        A sentence can carry several dates ("highest since 2020", "in FY25").
        The one closest to the number is the one that qualifies it.
        """
        best: Optional[Tuple[int, str]] = None
        for match in TIME_RE.finditer(quote):
            period = match.group(0).strip()
            if not FactNormalizer.normalize_time_period(period):
                continue
            distance = min(abs(match.start() - anchor), abs(match.end() - anchor))
            if best is None or distance < best[0]:
                best = (distance, period)
        if best:
            return best[1], True
        return profile.default_period_raw, False

    @staticmethod
    def _match_cue(text: str, cues: Tuple[Tuple[str, Optional[str]], ...]) -> Optional[str]:
        for pattern, value in cues:
            if value and re.search(pattern, text, re.IGNORECASE):
                return value
        return None

    # ------------------------------------------------------------------
    # Evidence and grounding
    # ------------------------------------------------------------------
    @classmethod
    def _build_evidence(
        cls, candidate: Candidate, label_span: Tuple[int, int], page_text: str,
        strategy: str = "", next_unit_len: int = 0
    ) -> Tuple[str, int, int]:
        """Return the evidence quote and its exact span in the page text.

        The quote is always cut from ``page_text`` itself, so verification is a
        substring comparison rather than a similarity score.
        """
        line = candidate.line
        start = max(0, min(label_span[0], candidate.match.start()))
        end = min(len(line), max(label_span[1], candidate.match.end()))

        # Grow to sentence boundaries within the line for a readable quote.
        sentence_start = line.rfind(". ", 0, start)
        start = 0 if sentence_start == -1 else sentence_start + 2
        sentence_end = line.find(". ", end)
        end = len(line) if sentence_end == -1 else sentence_end + 1

        abs_start = candidate.line_start + start
        abs_end = candidate.line_start + end

        # If the metric label was read from the preceding line, include it in the quote
        if strategy == "prev_line" and candidate.all_lines and candidate.line_index > 0:
            prev_line_start = candidate.all_lines[candidate.line_index - 1][1]
            abs_start = min(abs_start, prev_line_start)

        # If the unit was read from the following line, include it in the quote
        if next_unit_len > 0 and candidate.all_lines and candidate.line_index + 1 < len(candidate.all_lines):
            next_line_start = candidate.all_lines[candidate.line_index + 1][1]
            abs_end = max(abs_end, next_line_start + next_unit_len)

        return page_text[abs_start:abs_end], abs_start, abs_end

    # ------------------------------------------------------------------
    # Scoring and deduplication
    # ------------------------------------------------------------------
    @classmethod
    def _score(
        cls,
        unit: Optional[str],
        period_explicit: bool,
        subject_explicit: bool,
        strategy: str,
        predicate_key: str,
    ) -> Tuple[float, Dict[str, Any]]:
        strategy_bonus = {"prose": 0.12, "trailing": 0.09, "next_line": 0.06,
                          "prev_line": 0.04, "unit_noun": 0.02}
        token_count = len(predicate_key.split("_")) if predicate_key else 0

        signals = {
            "base": 0.45,
            "has_unit": 0.12 if unit else 0.0,
            "period_stated_in_sentence": 0.12 if period_explicit else -0.06,
            "subject_named_in_sentence": 0.08 if subject_explicit else 0.0,
            "label_source": strategy_bonus.get(strategy, 0.0),
            "label_specificity": 0.05 if 2 <= token_count <= 5 else (-0.08 if token_count > 6 else 0.0),
        }
        confidence = round(min(MAX_CONFIDENCE, max(MIN_CONFIDENCE, sum(signals.values()))), 3)
        return confidence, signals

    @classmethod
    def _dedupe(cls, facts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Keep the highest-confidence fact per (predicate, value, period) on a page."""
        best: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        dropped: List[Dict[str, Any]] = []

        for fact in facts:
            val = fact.get("value_numeric") if fact.get("value_numeric") is not None else fact.get("value_text")
            key = (fact["predicate"], val, fact["time_period_normalized"],
                   fact["subject_normalized"])
            incumbent = best.get(key)
            if incumbent is None:
                best[key] = fact
                continue
            loser = fact if fact["confidence"] <= incumbent["confidence"] else incumbent
            best[key] = incumbent if loser is fact else fact
            dropped.append({
                "document_id": loser["document_id"],
                "page_number": loser["evidence_page"],
                "candidate_text": loser["value_raw"][:120],
                "context_snippet": loser["evidence_quote"][:300],
                "reason_code": REJECT_DUPLICATE,
                "detail": (f"Same metric, value and period already captured on this page with "
                           f"confidence {best[key]['confidence']}."),
                "is_routine_filter": False,
            })

        return list(best.values()), dropped

    @staticmethod
    def _lines_with_offsets(page_text: str) -> List[Tuple[str, int]]:
        lines: List[Tuple[str, int]] = []
        offset = 0
        for line in page_text.split("\n"):
            lines.append((line, offset))
            offset += len(line) + 1
        return lines

    @staticmethod
    def _explain(reason: str, candidate_text: str) -> str:
        return {
            REJECT_BARE_NUMBER: (
                f"'{candidate_text}' is a number with no currency, magnitude, percent or "
                f"measurement noun attached, so it carries no measurable claim "
                f"(page numbers, list markers and section references land here)."
            ),
            REJECT_LOOKS_LIKE_YEAR: (
                f"'{candidate_text}' parses as a calendar year rather than a measured value."
            ),
            REJECT_SECTION_NUMBER: (
                f"'{candidate_text}' appears to be a section or outline number rather than a measured quantity."
            ),
            REJECT_NO_LABEL: (
                f"A quantity '{candidate_text}' was found but no metric phrase could be read "
                f"from the surrounding line or its neighbours, so what it measures is unknown."
            ),
            REJECT_LABEL_STOPWORDS: (
                f"The phrase around '{candidate_text}' reduced to function words only after "
                f"normalization, which would have produced an empty predicate."
            ),
            REJECT_LABEL_IS_UNIT: (
                f"The phrase around '{candidate_text}' consists only of currency, magnitude or "
                f"unit words, which names how the value is measured but not what it measures."
            ),
            REJECT_UNPARSEABLE: f"'{candidate_text}' could not be normalized to a number.",
            REJECT_GROUNDING: (
                f"The evidence span for '{candidate_text}' did not match the stored page text "
                f"exactly, so the fact was discarded rather than stored ungrounded."
            ),
            REJECT_LLM_UNGROUNDED: (
                f"The language model returned a fact whose quote does not appear in the page "
                f"text. It was discarded as an unverifiable generation."
            ),
        }.get(reason, f"Rejected candidate '{candidate_text}'.")

    # ------------------------------------------------------------------
    # Optional LLM pass
    # ------------------------------------------------------------------
    @classmethod
    def _extract_with_llm(
        cls,
        doc_id: str,
        page_number: int,
        page_text: str,
        doc_name: str,
        profile: DocumentProfile,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Optional enrichment pass, subject to the same grounding rule.

        The model may propose facts the patterns miss, but a proposal whose
        quote is not literally present in the page text is dropped, so the
        provider can never introduce an ungrounded number.
        """
        if not LLMProvider.is_llm_available():
            return [], []

        try:
            proposals = LLMProvider.extract_facts_llm(page_text, page_number, doc_name) or []
        except Exception as exc:  # noqa: BLE001 - the deterministic path must survive
            logger.warning("LLM extraction failed on page %s: %s", page_number, exc)
            return [], []

        facts: List[Dict[str, Any]] = []
        issues: List[Dict[str, Any]] = []

        for item in proposals:
            quote = (item.get("evidence_quote") or "").strip()
            start = page_text.find(quote) if quote else -1
            if start == -1:
                issues.append({
                    "document_id": doc_id,
                    "page_number": page_number,
                    "candidate_text": str(item.get("value_raw", ""))[:120],
                    "context_snippet": quote[:300],
                    "reason_code": REJECT_LLM_UNGROUNDED,
                    "detail": cls._explain(REJECT_LLM_UNGROUNDED, str(item.get("value_raw", ""))),
                    "is_routine_filter": False,
                })
                continue

            value_raw = str(item.get("value_raw", "")).strip()
            mantissa, unit, normalized = FactNormalizer.parse_numeric_value(value_raw)
            if mantissa is None:
                value_text = FactNormalizer.normalize_semantic_value(value_raw)
                if not value_text:
                    continue
                normalized = None
                unit = item.get("unit")
            else:
                value_text = str(mantissa)

            predicate_label, predicate_key = FactNormalizer.normalize_predicate(
                str(item.get("predicate", "")), acronyms=profile.acronyms
            )
            if not predicate_key:
                continue

            subject_display, subject_key = FactNormalizer.normalize_entity(
                str(item.get("subject") or profile.default_subject)
            )
            period_raw = item.get("time_period") or None
            period_key = FactNormalizer.normalize_time_period(period_raw)

            fingerprint = f"{doc_id}|{page_number}|{predicate_key}|{normalized or value_text}|llm"
            facts.append({
                "id": f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}",
                "document_id": doc_id,
                "subject": subject_display,
                "subject_normalized": subject_key,
                "predicate": predicate_key,
                "predicate_label": predicate_label,
                "value_raw": value_raw,
                "value_numeric": normalized,
                "value_text": value_text,
                "unit": unit or item.get("unit"),
                "unit_family": FactNormalizer.unit_family(unit or item.get("unit")),
                "time_period": period_raw,
                "time_period_normalized": period_key,
                "scope": item.get("scope"),
                "qualifier": item.get("qualifier"),
                "confidence": min(0.9, float(item.get("confidence", 0.75) or 0.75)),
                "evidence_quote": quote,
                "evidence_page": page_number,
                "char_start": start,
                "char_end": start + len(quote),
                "extraction_method": "llm",
                "extraction_metadata": {"grounding_verified": True, "provider": LLMProvider.active_provider()},
            })

        return facts, issues

    @classmethod
    def _extract_semantic_facts(
        cls,
        doc_id: str,
        page_number: int,
        page_text: str,
        lines: List[Tuple[str, int]],
        profile: DocumentProfile,
    ) -> List[Dict[str, Any]]:
        semantic_facts: List[Dict[str, Any]] = []
        seen_keys: set = set()

        kv_re = re.compile(
            r"^\s*(?P<key>[A-Za-z][A-Za-z0-9\s/_\-]{1,35})\s*(?::|\s+[-–—]\s+|\s{2,}|\t)\s*(?P<val>[A-Za-z0-9][A-Za-z0-9\s/_,.\(\)\-–+]{2,120})$"
        )
        relation_re = re.compile(
            r"\b(?P<subj>[A-Z][A-Za-z0-9\s&]{2,35})\s+(?:uses|utilizes|implements|incorporates|consists of|comprises|requires|includes|supports)\s+(?P<val>[A-Za-z0-9][A-Za-z0-9\s/_,&-+]{3,80})"
        )
        def_re = re.compile(
            r"\b(?P<subj>[A-Z][A-Za-z0-9\s-]{2,35})\s+(?:is defined as|refers to|denotes)\s+(?P<val>[A-Za-z0-9][A-Za-z0-9\s/_,.\(\)\-–+]{5,100})",
            re.IGNORECASE,
        )
        degree_re = re.compile(
            r"degree\s+of\s+(?P<deg>(?:Bachelor|Master|Doctor(?:ate)?|B\.?Sc|M\.?Sc|B\.?Tech|M\.?Tech|B\.?E|M\.?E|Ph\.?D|Associate|Diploma)[A-Za-z\s\.]+?)(?:\s+in\s+(?P<dept>[A-Za-z\s\(\)]+))?(?:\s+at|\s+from|\.|$)",
            re.IGNORECASE,
        )
        role_status_re = re.compile(
            r"\b(?P<person>[A-Z][a-z]+\s+[A-Z][a-z]+)\s+(?:was appointed as|served as|resigned as|acts as)\s+(?:an?|the)?\s*(?P<role>[A-Za-z\s]{3,50})",
            re.IGNORECASE,
        )

        skip_keys = {
            "note", "notes", "source", "sources", "table", "figure", "fig", "page",
            "section", "chapter", "tel", "fax", "email", "url", "http", "https", "www"
        }
        skip_clause_openers = {
            "this", "that", "these", "those", "it", "there", "what", "which", "he",
            "she", "they", "we", "you", "who", "whom", "when", "because", "to",
            "since", "while", "as", "if", "although", "though", "table", "figure",
            "fig", "section", "in", "for", "with", "after", "before", "during"
        }

        for line, line_start in lines:
            line_str = line.strip()
            if len(line_str) < 10:
                continue

            # 1. Key-Value / Labeled structures
            m_kv = kv_re.match(line_str)
            if m_kv:
                raw_k, raw_v = m_kv.group("key").strip(), m_kv.group("val").strip()
                k_low = raw_k.lower()
                if (k_low not in skip_keys and not any(k_low.startswith(sk + " ") for sk in skip_keys)
                        and len(raw_k.split()) <= 4):
                    pred_label, pred_key = FactNormalizer.normalize_predicate(raw_k, acronyms=profile.acronyms)
                    if pred_key and pred_key not in STOPWORDS and len(pred_key) > 2:
                        subj_disp, subj_key = FactNormalizer.normalize_entity(profile.default_subject)
                        quote = line_str
                        q_start = line_start + line.find(line_str)
                        q_end = q_start + len(quote)
                        if page_text[q_start:q_end] == quote:
                            fingerprint = f"{doc_id}|{page_number}|{pred_key}|{raw_v[:40]}"
                            fact_id = f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}"
                            if fact_id not in seen_keys:
                                seen_keys.add(fact_id)
                                semantic_facts.append({
                                    "id": fact_id,
                                    "document_id": doc_id,
                                    "subject": subj_disp,
                                    "subject_normalized": subj_key,
                                    "predicate": pred_key,
                                    "predicate_label": pred_label.title() if pred_label.islower() else pred_label,
                                    "value_raw": raw_v,
                                    "value_numeric": None,
                                    "value_text": FactNormalizer.normalize_semantic_value(raw_v),
                                    "unit": None,
                                    "unit_family": "semantic",
                                    "time_period": profile.default_period_raw,
                                    "time_period_normalized": FactNormalizer.normalize_time_period(profile.default_period_raw),
                                    "scope": None,
                                    "qualifier": None,
                                    "confidence": 0.80,
                                    "evidence_quote": quote,
                                    "evidence_page": page_number,
                                    "char_start": q_start,
                                    "char_end": q_end,
                                    "extraction_method": "semantic:key_value",
                                    "extraction_metadata": {
                                        "pattern": "key_value",
                                        "raw_key": raw_k,
                                        "period_inferred_from_document": True,
                                        "subject_inferred_from_document": True,
                                    },
                                })

            # 2. Degree and Department
            m_deg = degree_re.search(line_str)
            if m_deg:
                raw_deg = m_deg.group("deg").strip()
                if len(raw_deg) > 3 and raw_deg.lower() not in STOPWORDS:
                    pred_label, pred_key = FactNormalizer.normalize_predicate("degree")
                    subj_disp, subj_key = FactNormalizer.normalize_entity(profile.default_subject)
                    quote = line_str
                    q_start = line_start + line.find(line_str)
                    q_end = q_start + len(quote)
                    if page_text[q_start:q_end] == quote:
                        fingerprint = f"{doc_id}|{page_number}|{pred_key}|{raw_deg}"
                        fact_id = f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}"
                        if fact_id not in seen_keys:
                            seen_keys.add(fact_id)
                            semantic_facts.append({
                                "id": fact_id,
                                "document_id": doc_id,
                                "subject": subj_disp,
                                "subject_normalized": subj_key,
                                "predicate": pred_key,
                                "predicate_label": "Degree",
                                "value_raw": raw_deg,
                                "value_numeric": None,
                                "value_text": FactNormalizer.normalize_semantic_value(raw_deg),
                                "unit": None,
                                "unit_family": "semantic",
                                "time_period": profile.default_period_raw,
                                "time_period_normalized": FactNormalizer.normalize_time_period(profile.default_period_raw),
                                "scope": None,
                                "qualifier": None,
                                "confidence": 0.85,
                                "evidence_quote": quote,
                                "evidence_page": page_number,
                                "char_start": q_start,
                                "char_end": q_end,
                                "extraction_method": "semantic:degree",
                                "extraction_metadata": {
                                    "pattern": "degree",
                                    "period_inferred_from_document": True,
                                    "subject_inferred_from_document": True,
                                },
                            })

            # 3. Governance / Role / Status assertions
            m_role = role_status_re.search(line_str)
            if m_role:
                person = m_role.group("person").strip()
                role_val = m_role.group("role").strip()
                subj_disp, subj_key = FactNormalizer.normalize_entity(person)
                pred_label, pred_key = FactNormalizer.normalize_predicate("role_status")
                quote = line_str
                q_start = line_start + line.find(line_str)
                q_end = q_start + len(quote)
                if page_text[q_start:q_end] == quote:
                    fingerprint = f"{doc_id}|{page_number}|{subj_key}|{pred_key}|{role_val[:30]}"
                    fact_id = f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}"
                    if fact_id not in seen_keys:
                        seen_keys.add(fact_id)
                        semantic_facts.append({
                            "id": fact_id,
                            "document_id": doc_id,
                            "subject": subj_disp,
                            "subject_normalized": subj_key,
                            "predicate": pred_key,
                            "predicate_label": "Role Status",
                            "value_raw": role_val,
                            "value_numeric": None,
                            "value_text": FactNormalizer.normalize_semantic_value(role_val),
                            "unit": None,
                            "unit_family": "semantic",
                            "time_period": profile.default_period_raw,
                            "time_period_normalized": FactNormalizer.normalize_time_period(profile.default_period_raw),
                            "scope": None,
                            "qualifier": None,
                            "confidence": 0.82,
                            "evidence_quote": quote,
                            "evidence_page": page_number,
                            "char_start": q_start,
                            "char_end": q_end,
                            "extraction_method": "semantic:role_status",
                            "extraction_metadata": {
                                "pattern": "role_status",
                                "period_inferred_from_document": True,
                                "subject_inferred_from_document": False,
                            },
                        })

            # 4. Architecture / Technology / Component relation
            m_rel = relation_re.search(line_str)
            if m_rel:
                raw_s, raw_v = m_rel.group("subj").strip(), m_rel.group("val").strip()
                s_words = raw_s.lower().split()
                if (s_words and s_words[0] not in skip_clause_openers
                        and not any(w in {"table", "figure", "fig", "section"} for w in s_words)
                        and len(s_words) <= 4
                        and not any(bad in raw_s.lower() for bad in ["data", "intruder", "result", "sample"])):
                    subj_disp, subj_key = FactNormalizer.normalize_entity(raw_s)
                    pred_label, pred_key = FactNormalizer.normalize_predicate("architecture_components")
                    quote = line_str
                    q_start = line_start + line.find(line_str)
                    q_end = q_start + len(quote)
                    if page_text[q_start:q_end] == quote:
                        fingerprint = f"{doc_id}|{page_number}|{subj_key}|{pred_key}|{raw_v[:30]}"
                        fact_id = f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}"
                        if fact_id not in seen_keys:
                            seen_keys.add(fact_id)
                            semantic_facts.append({
                                "id": fact_id,
                                "document_id": doc_id,
                                "subject": subj_disp,
                                "subject_normalized": subj_key,
                                "predicate": pred_key,
                                "predicate_label": "Architecture Components",
                                "value_raw": raw_v,
                                "value_numeric": None,
                                "value_text": FactNormalizer.normalize_semantic_value(raw_v),
                                "unit": None,
                                "unit_family": "semantic",
                                "time_period": profile.default_period_raw,
                                "time_period_normalized": FactNormalizer.normalize_time_period(profile.default_period_raw),
                                "scope": None,
                                "qualifier": None,
                                "confidence": 0.76,
                                "evidence_quote": quote,
                                "evidence_page": page_number,
                                "char_start": q_start,
                                "char_end": q_end,
                                "extraction_method": "semantic:relation",
                                "extraction_metadata": {
                                    "pattern": "relation",
                                    "period_inferred_from_document": True,
                                    "subject_inferred_from_document": False,
                                },
                            })

            # 5. Definition / Concept mapping
            m_def = def_re.search(line_str)
            if m_def:
                raw_term, raw_def = m_def.group("subj").strip(), m_def.group("val").strip()
                t_words = raw_term.lower().split()
                if (t_words and t_words[0] not in skip_clause_openers
                        and not any(w in {"table", "figure", "fig", "section"} for w in t_words)
                        and len(t_words) <= 4):
                    subj_disp, subj_key = FactNormalizer.normalize_entity(raw_term)
                    pred_label, pred_key = FactNormalizer.normalize_predicate("definition")
                    quote = line_str
                    q_start = line_start + line.find(line_str)
                    q_end = q_start + len(quote)
                    if page_text[q_start:q_end] == quote:
                        fingerprint = f"{doc_id}|{page_number}|{subj_key}|{pred_key}|{raw_def[:30]}"
                        fact_id = f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}"
                        if fact_id not in seen_keys:
                            seen_keys.add(fact_id)
                            semantic_facts.append({
                                "id": fact_id,
                                "document_id": doc_id,
                                "subject": subj_disp,
                                "subject_normalized": subj_key,
                                "predicate": pred_key,
                                "predicate_label": "Definition",
                                "value_raw": raw_def,
                                "value_numeric": None,
                                "value_text": FactNormalizer.normalize_semantic_value(raw_def),
                                "unit": None,
                                "unit_family": "semantic",
                                "time_period": profile.default_period_raw,
                                "time_period_normalized": FactNormalizer.normalize_time_period(profile.default_period_raw),
                                "scope": None,
                                "qualifier": None,
                                "confidence": 0.78,
                                "evidence_quote": quote,
                                "evidence_page": page_number,
                                "char_start": q_start,
                                "char_end": q_end,
                                "extraction_method": "semantic:definition",
                                "extraction_metadata": {
                                    "pattern": "definition",
                                    "period_inferred_from_document": True,
                                    "subject_inferred_from_document": False,
                                },
                            })

        return semantic_facts
