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
    CURRENCY_SYMBOLS,
    NON_UNIT_FOLLOWERS,
    LEGAL_SUFFIXES,
    MONTHS,
    MULTIPLIERS,
    STOPWORDS,
    UNIT_SYMBOLS,
    UNIT_WORDS,
    FactNormalizer,
    measurement_token,
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

# The trailing word is captured, not vetted, by the pattern: whether it states a
# unit is decided in Python by ``measurement_token``, so a document may count
# anything it likes without a pattern change here.
_QUANTITY_BODY = rf"""
    (?P<lead>[-−(])?\s*
    (?<![A-Za-z])
    (?P<cur>US\$|U\.S\.\$|Rs\.?|INR|USD|EUR|GBP|CNY|JPY|AED|SGD|[₹$€£¥])?\s*
    (?<![A-Za-z0-9.])
    (?P<num>\d{{1,3}}(?:,\d{{2,3}})+(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<mag>{_MAGNITUDE_ALT})?\b
    \s*
    (?P<pct>per\s*cent|percent|%|bps|basis\s+points)?
"""

QUANTITY_RE = re.compile(
    _QUANTITY_BODY + r"""
    [ \t]*
    (?P<unit>[A-Za-z][A-Za-z0-9²³/.-]{0,19}(?:[ \t]+[A-Za-z][A-Za-z0-9²³/.-]{0,19})?)?
    (?P<close>\s*\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# The same pattern without the open-ended unit word, used when a quantity has to
# be blanked out of a phrase. Blanking with the full pattern would swallow the
# word after the number, which is often the metric name itself.
QUANTITY_SCRUB_RE = re.compile(_QUANTITY_BODY + r"(?P<close>\s*\))?", re.IGNORECASE | re.VERBOSE)

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
    r"achieved|delivered|generated|stayed|held|printed|settled|closed|ended|"
    r"accounted|accounting|represented|representing|comprised|comprising|"
    r"confirms|confirmed|verifies|verified|states|stated|notes|noted|finds|"
    r"found|shows|showed|indicates|indicated|assesses|assessed|puts|cites|"
    r"constituted|constituting|contributed|contributing|numbered|numbering)\b",
    re.IGNORECASE,
)

# Verbs of saying rather than of being. "Revenue was 4,215" puts the metric
# before the verb; "the Auditor confirms revenue of 4,215" puts it after. Both
# shapes are ordinary English, and reading the second one backwards names the
# metric after whoever is doing the reporting.
REPORT_VERB_RE = re.compile(
    r"\b(?:reports?|reported|reporting|records?|recorded|registers?|registered|"
    r"confirms?|confirmed|verifies|verified|states?|stated|notes?|noted|finds?|"
    r"found|shows?|showed|indicates?|indicated|assesses|assessed|cites?|cited|"
    r"estimates?|estimated|projects?|projected|forecasts?|announces?|announced|"
    r"discloses?|disclosed|publishes|published|puts|placed)\b",
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

_LEGAL_SUFFIX_ALT = "|".join(sorted(LEGAL_SUFFIXES, key=len, reverse=True))

# A name is a run of capitalised words, optionally joined by a lower-case
# connective ("Bank of England", "Ministry of Health and Family Welfare") and
# optionally closed by a legal suffix that is written in lower case ("plc").
# The previous pattern required the capitalised words to be adjacent with no
# space, so it could only ever match single words, which is why a document
# whose subject is named in two or more words was filed under one of them.
PROPER_NOUN_RE = re.compile(
    r"\b[A-Z][A-Za-z&.\-']*"
    r"(?:[ \t]+(?:of|for|and|the|und|de|van|von)[ \t]+[A-Z][A-Za-z&.\-']*"
    r"|[ \t]+[A-Z][A-Za-z&.\-']*){0,5}"
    rf"(?:[ \t]+(?:{_LEGAL_SUFFIX_ALT})\b\.?)?"
)
POSSESSIVE_RE = re.compile(r"\b((?:[A-Z][A-Za-z&.\-]+\s*){1,4})[‘’']s\b")
ACRONYM_DEF_RE = re.compile(r"\b((?:[A-Z][A-Za-z&.\-]+\s+){1,5})\(([A-Z]{2,6})\)")

FOOTNOTE_RE = re.compile(r"^\(\d{1,2}\)$|^\[\d{1,2}\]$")

# Paragraph and section numbering that publishers put at the start of a line
# ("I.9", "3.2.1", "II.6.14"). It is navigation, never part of a metric name.
SECTION_MARKER_RE = re.compile(r"^\s*(?:[IVXLCDM]+|\d{1,2})(?:\.\d{1,3})+\.?\s+", re.IGNORECASE)

# A label made only of magnitude, currency or unit words names no metric.
MEASURE_ONLY = set(MULTIPLIERS) | set(UNIT_SYMBOLS) | set(UNIT_WORDS) | {
    "rs", "inr", "usd", "eur", "gbp", "cent", "percent", "percentage", "bps",
    "basis", "point", "points", "rupee", "rupees", "dollar", "dollars",
}

# Reason codes recorded when a candidate quantity does not become a fact.
REJECT_BARE_NUMBER = "bare_number_without_unit"
REJECT_LOOKS_LIKE_YEAR = "number_is_a_year_not_a_measure"
REJECT_NO_LABEL = "no_metric_label_in_context"
REJECT_LABEL_STOPWORDS = "label_is_only_function_words"
REJECT_UNPARSEABLE = "value_could_not_be_normalized"
REJECT_GROUNDING = "evidence_quote_not_found_in_page_text"
REJECT_DUPLICATE = "duplicate_of_higher_confidence_fact"
REJECT_LLM_UNGROUNDED = "llm_quote_not_present_in_page_text"
REJECT_LABEL_IS_UNIT = "label_names_a_unit_not_a_metric"
REJECT_TOC_NAVIGATION = "table_of_contents_or_index_navigation"

# Rejections that are expected on every page (page numbers, list markers, dates
# in running text, table of contents/index navigation). They are counted but not stored
# one by one, because a hundred thousand of them would bury the diagnostics that matter.
ROUTINE_FILTERS = {REJECT_BARE_NUMBER, REJECT_LOOKS_LIKE_YEAR, REJECT_TOC_NAVIGATION}

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
    # Names seen in the block of text at the top of the first page, which is
    # where a document states what it is about.
    masthead: Counter = field(default_factory=Counter)
    # Which masthead line a name first appeared on. A letterhead names the
    # organisation above the title of the document it is publishing.
    masthead_rank: Dict[str, int] = field(default_factory=dict)
    # How many distinct pages each name appeared on.
    page_counts: Counter = field(default_factory=Counter)
    pages_seen: int = 0
    # How often a name was written as "the <name>". A phrase that is almost
    # always introduced by a definite article is a description of the subject
    # ("the Company", "the Bank"), not the name of one.
    article_counts: Counter = field(default_factory=Counter)
    # Mentions that share a line with a measured quantity. The subject of a
    # document is the thing its figures are about, which is not always the
    # organisation that published it.
    prose_counts: Counter = field(default_factory=Counter)
    # Names the document's own title says it is about ("Review of X").
    titled_subject: Counter = field(default_factory=Counter)
    # The runners-up and their scores, kept so that a wrong subject can be
    # explained rather than just observed.
    subject_ranking: List[Tuple[float, str]] = field(default_factory=list)

    def finalize(self, fallback_title: str = "") -> "DocumentProfile":
        """Choose the entity the document is about.

        A document names its subject in three ways at once, and the three
        together are far more reliable than any one of them:

        * it puts the name in its masthead, the first lines of the first page;
        * it repeats the name throughout, so the name appears on many pages
          rather than many times in one paragraph;
        * it refers back to the name in short form ("the System", "the Bank"),
          which is why a shorter name whose words are contained in a longer one
          is counted as another mention of the longer one.

        None of that is specific to a company, a country or a genre, so a
        document about something the system has never seen is ranked the same
        way as one it has.
        """
        self._merge_case_variants()
        self._absorb_short_forms()
        self._drop_definite_descriptions()

        title_tokens = {
            token for token in re.findall(r"[a-z]+", fallback_title.lower())
            if len(token) > 3
        }

        def weight(name: str, count: int) -> float:
            words = name.split()
            score = float(count)

            # A name is usually a phrase, though only mildly so: without a cap
            # a long heading would outrank a repeatedly used two-word name.
            score *= 1.0 + 0.35 * min(len(words) - 1, 4)

            # The decisive signal. A document mentions its subject again in the
            # body; it prints its own title once and never refers back to it.
            if count > self.masthead.get(name, 0):
                score *= 2.5

            # Appearing on many pages separates the subject of the document
            # from a term that happens to recur inside one section.
            if self.pages_seen > 1:
                score *= 1.0 + 2.0 * (self.page_counts.get(name, 1) / self.pages_seen)

            if self.titled_subject.get(name):
                # The document's own title said this is what it is about.
                score *= 3.0

            if name in self.masthead_rank:
                # Earlier is stronger: the letterhead sits above the title. A
                # masthead phrase the body never uses again is a caption or a
                # heading on a content page, so it gets much less.
                if count > self.masthead.get(name, 0):
                    score *= 3.0 if self.masthead_rank[name] == 0 else 1.8
                else:
                    score *= 1.5

            # Being written into sentences rather than into headings.
            score *= 1.0 + 2.0 * (self.prose_counts.get(name, 0) / count)

            # A bare acronym the document never expands is as likely to be a
            # metric ("GDP", "EBITDA") as an organisation.
            if (len(words) == 1 and words[0].isupper()
                    and words[0] not in self.acronyms):
                score *= 0.4

            # A legal suffix says the phrase names an organisation outright.
            if words[-1].lower().strip(".") in LEGAL_SUFFIXES:
                score *= 1.8

            if title_tokens and any(word.lower() in title_tokens for word in words):
                # The name the document was filed under, or that its own
                # metadata gives it, is the most direct statement anyone has
                # made about what the document covers.
                score *= 4.0
            return score

        ranked = sorted(
            ((weight(name, count), name) for name, count in self.subject_counts.items()),
            reverse=True,
        )
        self.subject_ranking = [(round(score, 2), name) for score, name in ranked[:5]]
        if ranked:
            self.default_subject = ranked[0][1]
            # An alternative subject must be reasonably well established in the
            # document before a single sentence may override the dominant one.
            self.subject_threshold = max(3, self.subject_counts.most_common(1)[0][1] // 10)
        elif fallback_title:
            words = [word for word in fallback_title.split()
                     if word.lower() not in DocumentProfiler.NON_ENTITY]
            self.default_subject = " ".join(words) or fallback_title

        if self.period_counts:
            self.default_period_raw = self.period_counts.most_common(1)[0][0]
        return self

    def _merge_case_variants(self) -> None:
        """Fold the spellings of one name together.

        "DELHIVERY" on a cover, "Delhivery Limited" in a signature block and
        "Delhivery" in the body are one company. The normalizer already knows
        how to reduce all three to the same key -- case-folded, punctuation and
        legal suffix removed -- so grouping on that key needs no new rules and
        keeps the profile agreeing with the comparison stage.
        """
        groups: Dict[str, List[str]] = {}
        for name in self.subject_counts:
            key = FactNormalizer.normalize_entity(name)[1]
            groups.setdefault(key, []).append(name)

        for variants in groups.values():
            if len(variants) < 2:
                continue
            # Prefer the form a reader would write: the shortest spelling, and
            # mixed case over shouting.
            display = min(variants, key=lambda name: (name.isupper(), len(name)))
            for name in variants:
                if name == display:
                    continue
                self.subject_counts[display] += self.subject_counts.pop(name)
                self.prose_counts[display] += self.prose_counts.get(name, 0)
                self.article_counts[display] += self.article_counts.get(name, 0)
                self.titled_subject[display] += self.titled_subject.get(name, 0)
                self.page_counts[display] = max(self.page_counts.get(display, 0),
                                                self.page_counts.get(name, 0))
                if self.masthead.get(name):
                    self.masthead[display] += self.masthead[name]
                    self.masthead_rank[display] = min(
                        self.masthead_rank.get(display, self.masthead_rank[name]),
                        self.masthead_rank[name])

    def _drop_definite_descriptions(self) -> None:
        """Remove one-word names that are really "the <noun>" self-references.

        A prospectus calls its issuer "the Company" on every page. Counting
        those mentions files the document under a common noun instead of a
        name. A phrase that is almost always article-introduced, is a single
        word, and never appears in the masthead, is such a self-reference.
        """
        for name, count in list(self.subject_counts.items()):
            if " " in name or self.masthead_rank.get(name) == 0:
                continue
            if self.article_counts.get(name, 0) >= max(2, 0.6 * count):
                del self.subject_counts[name]

    def _absorb_short_forms(self) -> None:
        """Credit a short mention to the full name it abbreviates.

        "The System employed 8,940 staff" is a mention of "Northfield Regional
        Health System". Without this, the short form wins on frequency and the
        document ends up filed under a word rather than a name.
        """
        longer = [name for name in self.subject_counts if " " in name]
        if not longer:
            return

        for short in sorted(self.subject_counts, key=lambda name: len(name.split())):
            short_tokens = set(short.lower().split())
            if len(short_tokens) > 2:
                continue
            hosts = [name for name in longer
                     if name != short and name in self.subject_counts
                     and short_tokens < set(name.lower().split())]
            if len(hosts) > 1:
                # Several longer names contain the short one. If they all share
                # the short form as their opening words it is still one name
                # written at different lengths ("Delhivery", "Delhivery Corp
                # Limited"); if they diverge, the short form is a common word.
                opening = short.lower().split()
                if all(name.lower().split()[:len(opening)] == opening for name in hosts):
                    for name in hosts:
                        self.subject_counts[short] += self.subject_counts.pop(name)
                        self.prose_counts[short] += self.prose_counts.get(name, 0)
                        self.titled_subject[short] += self.titled_subject.get(name, 0)
                        self.page_counts[short] = max(self.page_counts.get(short, 0),
                                                      self.page_counts.get(name, 0))
                        if self.masthead.get(name):
                            self.masthead[short] += self.masthead[name]
                            self.masthead_rank[short] = min(
                                self.masthead_rank.get(short, self.masthead_rank[name]),
                                self.masthead_rank[name])
                    continue
            if len(hosts) != 1:
                continue  # ambiguous short form; leave it standing on its own
            host = hosts[0]
            self.subject_counts[host] += self.subject_counts[short]
            self.prose_counts[host] += self.prose_counts.get(short, 0)
            self.titled_subject[host] += self.titled_subject.get(short, 0)
            self.page_counts[host] = max(self.page_counts.get(host, 0),
                                         self.page_counts.get(short, 0))
            if self.masthead.get(short):
                self.masthead[host] += self.masthead[short]
                self.masthead_rank[host] = min(
                    self.masthead_rank.get(host, self.masthead_rank[short]),
                    self.masthead_rank[short],
                )
            del self.subject_counts[short]


class DocumentProfiler:
    """Learns the dominant entity, period and acronyms of a document."""

    # Words that are capitalised for typographic reasons rather than because
    # they name an entity.
    # Words that name a part of a document rather than an entity. Used only to
    # reject a *leading* word, because "Chart" can end a name but cannot head one.
    STRUCTURAL_WORDS = {
        "table", "figure", "fig", "chart", "graph", "exhibit", "box", "annex",
        "annexure", "appendix", "chapter", "section", "part", "note", "notes",
        "page", "source", "sources", "panel", "schedule", "statement", "para",
        "paragraph", "item", "column", "row",
    }

    NON_ENTITY = {
        "the", "this", "that", "these", "those", "it", "in", "on", "at", "for",
        "and", "or", "but", "however", "although", "while", "during", "as",
        "table", "figure", "chart", "annex", "annexure", "appendix", "chapter",
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
        "audit", "assessment", "evaluation", "inspection", "opinion",
        "independent", "draft", "final", "interim",
        "volume", "disclosure", "disclosures", "highlights", "appendix",
    }

    # "Review of X", "Independent Audit of X", "Report on X". A title built this
    # way names the document's genre and then its subject, and the subject is
    # the half that matters -- an audit of a hospital is about the hospital, not
    # about the auditor whose name sits above it on the page.
    TITLE_SUBJECT_RE = re.compile(
        r"\b([A-Za-z]+)\s+(?:of|on|into|about|for)\s+(" + PROPER_NOUN_RE.pattern + r")")

    # A capitalised word introduced by a determiner is a defined term or a
    # reference back ("the Company", "our Company", "its Board"), not a name.
    DETERMINERS = {"the", "a", "an", "our", "its", "their", "this", "that",
                   "his", "her", "your", "these", "those"}

    # How many lines at the top of the first page count as the masthead. A
    # cover page, a letterhead and a slide title all sit inside this many lines.
    MASTHEAD_LINES = 6

    @classmethod
    def profile_pages(cls, pages: Iterable[Dict[str, Any]], title: str = "") -> DocumentProfile:
        profile = DocumentProfile()
        for index, page in enumerate(pages):
            cls.observe(profile, page.get("text", ""), is_first_page=index == 0)
        return profile.finalize(fallback_title=title)

    @classmethod
    def observe(cls, profile: DocumentProfile, text: str, is_first_page: bool = False) -> None:
        if not text:
            return

        # Character offsets of the lines that carry a measured quantity. A name
        # that keeps company with figures is what those figures are about; a
        # name that only ever appears on headings and cover lines is the
        # publisher, the genre, or a caption.
        sentence_spans: List[Tuple[int, int]] = []
        offset = 0
        for line in text.split("\n"):
            if any(m.group("cur") or m.group("mag") or m.group("pct")
                   for m in QUANTITY_RE.finditer(line)):
                sentence_spans.append((offset, offset + len(line)))
            offset += len(line) + 1

        def in_prose(position: int) -> bool:
            return any(start <= position < end for start, end in sentence_spans)

        profile.pages_seen += 1
        on_this_page: set = set()

        if is_first_page:
            for rank, line in enumerate(text.split("\n")[:cls.MASTHEAD_LINES]):
                for match in cls.TITLE_SUBJECT_RE.finditer(line):
                    if match.group(1).lower() not in cls.NON_ENTITY:
                        continue
                    named = cls._clean_entity(match.group(2))
                    if named:
                        profile.titled_subject[named] += 1

                for match in PROPER_NOUN_RE.finditer(line):
                    candidate = cls._clean_entity(match.group(0))
                    if candidate:
                        profile.masthead[candidate] += 1
                        profile.masthead_rank.setdefault(candidate, rank)

        for match in ACRONYM_DEF_RE.finditer(text):
            expansion = match.group(1).strip()
            acronym = match.group(2).strip()
            if 1 <= len(expansion.split()) <= 6:
                profile.acronyms.setdefault(acronym, expansion)

        for match in POSSESSIVE_RE.finditer(text):
            candidate = cls._clean_entity(match.group(1))
            if candidate:
                profile.subject_counts[candidate] += 3
                on_this_page.add(candidate)

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
                on_this_page.add(candidate)
                head = candidate.split(" ")[0]
                if in_prose(match.start()):
                    profile.prose_counts[candidate] += 1
                if head != candidate and len(head) > 2 and head.lower() not in cls.NON_ENTITY:
                    # "Delhivery Corp Limited" and "Delhivery Freight Services"
                    # are two mentions of one company. Counting the head of the
                    # phrase as well lets the short form gather them together
                    # under one name in ``_absorb_short_forms``.
                    profile.subject_counts[head] += 1
                    on_this_page.add(head)
                lead = match.group(0).split(" ", 1)[0].lower()
                before = text[max(0, match.start() - 6):match.start()].lower()
                previous_word = before.strip().rsplit(" ", 1)[-1] if before.strip() else ""
                if lead in cls.DETERMINERS or previous_word in cls.DETERMINERS:
                    profile.article_counts[candidate] += 1

        for candidate in on_this_page:
            profile.page_counts[candidate] += 1

        for match in TIME_RE.finditer(text):
            period = match.group(0).strip()
            if FactNormalizer.normalize_time_period(period):
                profile.period_counts[period.lower()] += 1

    @staticmethod
    def _is_sentence_initial(text: str, position: int) -> bool:
        before = text[:position].rstrip()
        return not before or before[-1] in ".!?\n"

    # Internal capitals in a *short* token mark an abbreviated comparison
    # ("YoY", "QoQ", "MoM"). In a longer token they are ordinary house style for
    # a name ("GridCo", "PepsiCo", "McKinsey"), so the length bound matters.
    ABBREVIATION_SHAPE = re.compile(r"^[A-Z][a-z]{1,2}[A-Z][a-z]?$")

    @classmethod
    def _clean_entity(cls, raw: str) -> Optional[str]:
        cleaned = re.sub(r"\s+", " ", raw).strip(" .,;:-")
        if len(cleaned) < 3 or len(cleaned) > 60:
            return None
        tokens = cleaned.split()
        if not tokens:
            return None
        if all(token.lower() in cls.NON_ENTITY for token in tokens):
            return None
        # A currency code, a unit symbol or a magnitude word is capitalised
        # because of what it is, not because it names anything. Reusing the
        # normalizer's own tables here means no second list to maintain.
        if all(token.lower().strip(".") in MEASURE_ONLY or token.lower() in CURRENCY_SYMBOLS
               for token in tokens):
            return None
        # "Chart I", "Table 3", "Annex B" reference a part of the document.
        # A name cannot be headed by the word for a piece of furniture.
        if tokens[0].lower().strip(".") in cls.STRUCTURAL_WORDS:
            return None
        # A run of month names is a chart axis or a table header.
        if all(token.lower().strip(".") in MONTHS for token in tokens):
            return None
        # Three or more all-capital tokens in a row is a header of abbreviated
        # column names, not somebody's name.
        if sum(1 for token in tokens if token.isupper() and len(token) > 1) >= 3:
            return None
        if any(cls.ABBREVIATION_SHAPE.match(token) for token in tokens):
            return None
        if tokens[0].lower() in cls.NON_ENTITY and len(tokens) == 1:
            return None
        # Drop leading function words so "The Reserve Bank" and "Reserve Bank"
        # do not compete as separate entities.
        while tokens and tokens[0].lower() in {"the", "a", "an", "in", "of", "and",
                                               "for", "by", "to", "at", "from", "with",
                                               "through", "via", "under", "about"}:
            tokens.pop(0)
        # "Independent Audit of Northfield Regional Health System" names the
        # document and then the entity. Dropping the genre words in front, and
        # the connective that joins them on, leaves the name itself.
        # "Northwind Freight Annual Report" and "Northwind Freight Investor
        # Presentation" name one company and two documents. Trimming the genre
        # words off the end leaves the name, which is what lets two documents
        # about the same subject be recognised as such.
        while len(tokens) > 1 and tokens[-1].lower().strip(".") in cls.NON_ENTITY:
            tokens.pop()

        lowered = [token.lower() for token in tokens]
        if "of" in lowered[:4]:
            cut = lowered.index("of")
            if any(token in cls.NON_ENTITY for token in lowered[:cut]) and len(tokens) > cut + 1:
                tokens = tokens[cut + 1:]
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
    # Where the previous quantity on this line ended. A metric name never spans
    # across another number, so the search for a label stops here.
    scan_start: int = 0
    unit: Optional[str] = None
    unit_family: Optional[str] = None
    # The full noun phrase the unit was read from ("outpatient visits"), which
    # names the metric when the sentence does not.
    unit_phrase: Optional[str] = None
    value_span: Tuple[int, int] = (0, 0)


class FactExtractionPipeline:
    """Turns canonical page text into grounded facts plus rejection diagnostics."""

    @classmethod
    def is_table_of_contents_page(cls, page_text: str) -> bool:
        """Detect whether a page is a Table of Contents, Index, or list of figures/tables.

        Navigational pages list sections, chapters, and page references rather than
        stating empirical measurements.
        """
        if not page_text or len(page_text.strip()) < 20:
            return False

        lines = [ln.strip() for ln in page_text.strip().split("\n") if ln.strip()]
        if not lines:
            return False

        # 1. Header check: does the page start with or prominently feature a TOC / Index title?
        header_candidates = lines[:6]
        has_toc_header = any(
            re.match(
                r"^(?:table\s+of\s+contents|contents|index(?:\s+of\s+[\w\s]+)?|"
                r"list\s+of\s+(?:tables|figures|boxes|charts|illustrations|exhibits|appendices|abbreviations)|"
                r"appendix\s+tables|brief\s+contents)\s*$",
                c.lower().strip(),
            )
            for c in header_candidates
        )

        # 2. Count dot-leaders or dashed leaders leading to page numbers
        dot_leaders = sum(
            1 for ln in lines
            if re.search(r"(?:\.{3,}|…{2,}|_{3,}|-{4,})\s*(?:[ivxlcdm]+|\d+)\s*$", ln, re.IGNORECASE)
        )

        # 3. Decision rule:
        # A. Prominent TOC header with dot leaders or short directory list
        if has_toc_header and (dot_leaders >= 1 or len(lines) <= 50):
            return True

        # B. Multiple dot leaders connecting headings to page numbers (unambiguous TOC/Index)
        if dot_leaders >= 3:
            return True

        return False

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

        if cls.is_table_of_contents_page(page_text):
            return facts, [{
                "document_id": doc_id,
                "page_number": page_number,
                "candidate_text": "table_of_contents_page",
                "context_snippet": page_text[:200].strip(),
                "reason_code": REJECT_TOC_NAVIGATION,
                "detail": cls._explain(REJECT_TOC_NAVIGATION, "table_of_contents_page"),
                "is_routine_filter": True,
            }]

        profile = profile or DocumentProfile()
        lines = cls._lines_with_offsets(page_text)

        for index, (line, line_start) in enumerate(lines):
            prev_line = lines[index - 1][0] if index > 0 else None
            next_line = lines[index + 1][0] if index + 1 < len(lines) else None

            # Paragraph numbering ("2.1", "II.6") is navigation, and reading it
            # as a quantity is the single most common false positive in a
            # numbered report. It is skipped before anything else is looked at.
            marker = SECTION_MARKER_RE.match(line)
            scan_from = marker.end() if marker else 0

            for match in QUANTITY_RE.finditer(line, scan_from):
                candidate = cls._build_candidate(line, line_start, match, prev_line,
                                                 next_line, scan_from)
                fact, issue = cls._evaluate(candidate, doc_id, page_number, page_text, profile)
                if fact:
                    facts.append(fact)
                elif issue:
                    issues.append(issue)
                if fact:
                    # Only a quantity that became a fact blocks the next label
                    # from reading past it. A number that was filtered out --
                    # "30" inside "30-day" -- is part of the wording, and
                    # cutting the phrase there would lose the metric's name.
                    scan_from = max(scan_from, candidate.value_span[1])

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
    def _build_candidate(
        cls,
        line: str,
        line_start: int,
        match: re.Match,
        prev_line: Optional[str],
        next_line: Optional[str],
        scan_start: int,
    ) -> Candidate:
        """Decide how much of the match is the quantity, and what its unit is.

        The pattern captures the word after the number without judging it. Here
        that word is put to ``measurement_token``: if it names a unit it belongs
        to the value, and if it does not it is left in the sentence, where it is
        usually the start of the metric name.
        """
        groups = match.groupdict()
        unit, family, unit_end, unit_phrase = None, None, None, None

        # "412,600 outpatient visits" puts an adjective between the number and
        # the noun that says what is counted, so both words are considered and
        # the head noun -- the last one -- wins.
        words = (groups.get("unit") or "").split()
        for index in range(len(words) - 1, -1, -1):
            # Only a modifier may stand between the number and the noun it
            # counts. A preposition means the noun belongs to the next phrase,
            # as in "page 17 for details", where nothing is being counted.
            if any(not word.isalpha() or not word.islower()
                   or word in STOPWORDS or word in NON_UNIT_FOLLOWERS
                   for word in words[:index]):
                continue
            classified = measurement_token(words[index])
            if classified:
                unit, family = classified
                unit_phrase = " ".join(words[:index + 1])
                unit_end = match.start("unit") + len(unit_phrase)
                break

        end = match.end("num")
        for name in ("mag", "pct"):
            if groups.get(name):
                end = max(end, match.end(name))
        if unit_end is not None:
            end = max(end, unit_end)

        start = match.start("num")
        if groups.get("cur"):
            start = match.start("cur")
        if groups.get("lead"):
            start = match.start("lead")

        # Keep a closing bracket only when the value opened one, so an
        # accounting negative reads as it does on the page.
        if groups.get("lead") == "(" and line[end:end + 2].strip().startswith(")"):
            end = line.index(")", end) + 1

        return Candidate(
            line=line,
            line_start=line_start,
            match=match,
            prev_line=prev_line,
            next_line=next_line,
            scan_start=scan_start,
            unit=unit,
            unit_family=family,
            unit_phrase=unit_phrase,
            value_span=(start, end),
        )

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
        raw_quantity = candidate.line[candidate.value_span[0]:candidate.value_span[1]].strip()
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

        has_measure = bool(groups["cur"] or groups["mag"] or groups["pct"] or candidate.unit)
        if not has_measure:
            return reject(REJECT_BARE_NUMBER)

        number_text = groups["num"].replace(",", "")
        # A four digit number with no decimals that reads as a year is a date
        # reference, not a measurement, unless a currency makes it a value.
        if (not groups["cur"] and not groups["pct"] and "." not in number_text
                and len(number_text) == 4 and 1900 <= int(number_text) <= 2100):
            return reject(REJECT_LOOKS_LIKE_YEAR)

        # Reject numbers that sit inside a Table of Contents / Index dot-leader line
        if re.search(r"(?:\.{3,}|…{2,}|_{3,}|-{4,})\s*(?:[ivxlcdm]+|\d+)\s*$", candidate.line, re.IGNORECASE):
            return reject(REJECT_TOC_NAVIGATION)

        label, strategy, label_span = cls._find_label(candidate)
        if not label:
            return reject(REJECT_NO_LABEL)

        predicate_label, predicate_key = FactNormalizer.normalize_predicate(
            label, acronyms=profile.acronyms
        )
        if not predicate_key:
            return reject(REJECT_LABEL_STOPWORDS)

        # "The System recorded 412,600 outpatient visits" leaves the subject's
        # own name in front of the verb, which names who reported the figure
        # rather than what was measured. When the quantity counts something,
        # the thing it counts is the better name for the metric.
        subject_tokens = set(FactNormalizer.normalize_entity(profile.default_subject)[1].split("_"))

        # "readmission rate for Northfield Regional Health System" and
        # "readmission rate" are one metric. Carrying the subject's name inside
        # the metric's name would keep them apart.
        trimmed = [token for token in predicate_key.split("_") if token not in subject_tokens]
        if trimmed and len(trimmed) < len(predicate_key.split("_")):
            predicate_key = "_".join(trimmed)
            kept = [word for word in predicate_label.split()
                    if FactNormalizer._singularise(word.lower().strip(",.")) not in subject_tokens]
            # Removing the name leaves the preposition that introduced it
            # ("readmission rate for"), which is not part of the name either.
            while kept and kept[-1].lower() in STOPWORDS:
                kept.pop()
            predicate_label = " ".join(kept) or predicate_label

        if (candidate.unit_phrase and subject_tokens
                and set(predicate_key.split("_")) <= subject_tokens):
            predicate_label, predicate_key = FactNormalizer.normalize_predicate(
                candidate.unit_phrase, acronyms=profile.acronyms)
            strategy = "unit_noun"

        if not predicate_key:
            return reject(REJECT_LABEL_STOPWORDS)
        if all(token in MEASURE_ONLY for token in predicate_key.split("_")):
            return reject(REJECT_LABEL_IS_UNIT)
        if all(token in DocumentProfiler.STRUCTURAL_WORDS for token in predicate_key.split("_")):
            # "the figure of 11.4 per cent" says where the number is printed,
            # not what it measures.
            return reject(REJECT_LABEL_IS_UNIT)
        pred_tokens = predicate_key.split("_")
        if pred_tokens and pred_tokens[0] in {"note", "notes", "disclaimer", "source", "sources", "footnote", "annexure", "appendix"}:
            return reject(REJECT_LABEL_IS_UNIT)

        mantissa, unit, normalized = FactNormalizer.parse_numeric_value(raw_quantity)
        if mantissa is None:
            return reject(REJECT_UNPARSEABLE)
        unit = unit or candidate.unit

        quote, quote_start, quote_end = cls._build_evidence(candidate, label_span, page_text)
        if quote != page_text[quote_start:quote_end]:
            return reject(REJECT_GROUNDING)

        subject_raw, subject_explicit = cls._find_subject(
            candidate, label_span, predicate_key, profile
        )
        subject_display, subject_key = FactNormalizer.normalize_entity(subject_raw)

        anchor = (candidate.line_start + candidate.value_span[0]) - quote_start
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
    def _find_label(cls, candidate: Candidate) -> Tuple[Optional[str], str, Tuple[int, int]]:
        """Locate the metric phrase for a quantity.

        Four generic layouts are tried, in decreasing order of reliability:

        1. ``prose``      "forex reserves stood at US$ 668.3 billion"
        2. ``trailing``   "740 Mn express parcel shipments"
        3. ``next_line``  a KPI tile whose caption sits under the number
        4. ``prev_line``  a table row whose header sits above the number
        """
        line = candidate.line
        start, end = candidate.value_span
        # The label may not reach back past the previous quantity on the line:
        # in "11.4 per cent in 2024, below the benchmark of 13.1 per cent" the
        # second figure is a different measurement from the first.
        prefix = line[candidate.scan_start:start]
        suffix = line[end:]

        label = cls._clean_label(cls._label_from_prefix(prefix))
        if label:
            comp_terms = {"yoy", "qoq", "mom", "y-o-y", "q-o-q", "m-o-m"}
            if label.lower() in comp_terms:
                context_label = None
                if candidate.prev_line:
                    context_label = cls._clean_label(cls._label_from_neighbour(candidate.prev_line))
                if context_label and context_label.lower() not in comp_terms:
                    label = f"{context_label} ({label})"
                else:
                    label = f"{label} Growth"
            return label, "prose", (max(candidate.scan_start, start - 220), end)

        label = cls._clean_label(cls._label_from_suffix(suffix))
        if label:
            return label, "trailing", (start, min(len(line), end + 160))

        if candidate.next_line:
            label = cls._clean_label(cls._label_from_neighbour(candidate.next_line))
            if label:
                return label, "next_line", (start, end)

        if candidate.prev_line:
            label = cls._clean_label(cls._label_from_neighbour(candidate.prev_line))
            if label:
                return label, "prev_line", (start, end)

        # "880 Mn parcels" names what is counted in the quantity itself.
        if candidate.unit and candidate.unit_family == "count":
            return candidate.unit, "unit_noun", (start, min(len(line), end + 80))

        return None, "none", (start, end)

    @staticmethod
    def _label_from_prefix(prefix: str) -> str:
        """Take the metric phrase sitting before the reporting verb.

        In "real GDP growth moderated to 6.5 per cent", the subject noun phrase
        runs from the start of the clause to the *first* reporting verb. Cutting
        at the first verb rather than the last is what keeps "headline inflation"
        from becoming "headline inflation moderated to an average of".
        """
        clause = re.split(r"[.;:]\s|•", prefix)[-1]
        # A comma followed by a linking word starts a new claim about a
        # different quantity, so the metric name begins after it.
        clause = re.split(
            r",\s+(?:which|while|and|or|of\s+which|below|above|compared|against|"
            r"versus|vs|up|down|including|excluding|with|from|led|driven|but|"
            r"higher|lower|greater|smaller|better|worse|broadly|roughly)\b",
            clause,
        )[-1]
        clause = SECTION_MARKER_RE.sub("", clause.lstrip())
        clause = re.sub(r"\([^)]*\)", " ", clause)
        clause = QUANTITY_SCRUB_RE.sub(" ", clause)
        clause = TIME_RE.sub(" ", clause)

        # "The company reported that throughput reached N" puts the metric after
        # the complementizer, not before the reporting verb.
        complementizer = list(re.finditer(r"\bthat\b", clause, re.IGNORECASE))
        if complementizer:
            clause = clause[complementizer[-1].end():]

        # A verb of saying hands the metric to the words after it.
        head = ""
        saying = REPORT_VERB_RE.search(clause)
        if saying:
            tail = clause[saying.end():]
            stop = VERB_LINK_RE.search(tail)
            candidate_head = tail[: stop.start()] if stop else tail
            if [word for word in re.findall(r"[A-Za-z][A-Za-z0-9&/-]*", candidate_head)
                    if word.lower() not in STOPWORDS]:
                head = candidate_head

        if not head:
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
        # A caption is a fragment. A neighbouring line that is a sentence in its
        # own right is about something else, and reading a label out of it
        # attaches this number to the wrong metric.
        if neighbour.rstrip().endswith((".", "!", "?")) or VERB_LINK_RE.search(neighbour):
            return ""
        cleaned = re.sub(r"\(\d+\)|\[\d+\]", " ", neighbour)
        cleaned = QUANTITY_SCRUB_RE.sub(" ", cleaned)
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

        # Reject structural boilerplate that introduces notes, sources, or disclaimers
        if tokens and tokens[0].lower() in {"note", "notes", "disclaimer", "source", "sources", "footnote", "annexure", "appendix"}:
            return None

        # If label contains exclusion clause ("Revenue from services excludes..."), cut at the exclusion
        exclusion_match = re.split(r"\b(?:excludes?|excluding|excl\.?)\b", label, maxsplit=1, flags=re.IGNORECASE)
        if len(exclusion_match) > 1 and len(exclusion_match[0].strip()) >= 3:
            return cls._clean_label(exclusion_match[0].strip())

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

        # Cap on meaningful words, not raw tokens, so "growth in gross value
        # added in the agriculture and allied sector" survives intact while a
        # runaway phrase still gets cut.
        kept: List[str] = []
        seen_content = 0
        for token in tokens:
            if token.lower() not in STOPWORDS and len(token) > 1:
                if seen_content == MAX_LABEL_TOKENS:
                    break
                seen_content += 1
            kept.append(token)
        while kept and kept[-1].lower() in STOPWORDS:
            kept.pop()
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
        # A sentence often carries two figures for two periods: "grew to X in
        # 2024 from Y in 2023". The connective marks where one claim ends and
        # the next begins, so a period on the far side of it belongs to the
        # other figure, however close it happens to sit.
        segment_start, segment_end = cls._clause_bounds(quote, anchor)

        best: Optional[Tuple[Tuple[int, int], str]] = None
        for match in TIME_RE.finditer(quote):
            period = match.group(0).strip()
            if not FactNormalizer.normalize_time_period(period):
                continue
            outside = 0 if segment_start <= match.start() < segment_end else 1
            distance = min(abs(match.start() - anchor), abs(match.end() - anchor))
            key = (outside, distance)
            if best is None or key < best[0]:
                best = (key, period)
        if best:
            return best[1], True
        return profile.default_period_raw, False

    # Connectives that separate one reported figure from another inside a single
    # sentence. Everything here is a general English comparison word.
    CLAUSE_SPLIT_RE = re.compile(
        r"\b(?:from|against|versus|vs\.?|compared\s+(?:with|to)|as\s+against|"
        r"up\s+from|down\s+from|while|whereas|whilst|below\s+the|above\s+the)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _clause_bounds(cls, text: str, anchor: int) -> Tuple[int, int]:
        """The stretch of the sentence that belongs to the figure at ``anchor``."""
        boundaries = [0] + [m.start() for m in cls.CLAUSE_SPLIT_RE.finditer(text)] + [len(text)]
        for index in range(len(boundaries) - 1):
            if boundaries[index] <= anchor < boundaries[index + 1]:
                return boundaries[index], boundaries[index + 1]
        return 0, len(text)

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
        cls, candidate: Candidate, label_span: Tuple[int, int], page_text: str
    ) -> Tuple[str, int, int]:
        """Return the evidence quote and its exact span in the page text.

        The quote is the sentence the value sits in, cut from ``page_text``
        itself, so verification is a substring comparison rather than a
        similarity score. ``label_span`` is accepted for callers that want to
        widen the quote and is currently only used to keep that option open.
        """
        line = candidate.line
        value_start, value_end = candidate.value_span

        # The evidence is the sentence the number sits in. Searching outwards
        # from the number itself (rather than from the widened label window) is
        # what keeps the previous sentence out of the quote, which matters
        # because the period and the reporting basis are read back out of it.
        sentence_start = cls._sentence_start(line, value_start)
        sentence_end = cls._sentence_end(line, value_end)

        # The evidence is exactly the sentence, never a window around the
        # label: a quote cropped at the previous number hides the period and the
        # reporting basis that the sentence states, and both are read back out
        # of the quote further down.
        start, end = sentence_start, sentence_end

        abs_start = candidate.line_start + start
        abs_end = candidate.line_start + end
        return page_text[abs_start:abs_end], abs_start, abs_end

    # Abbreviations whose full stop does not end a sentence. All of them are
    # ordinary English or accounting shorthand, not document specific.
    ABBREVIATIONS = {
        "rs", "no", "nos", "fig", "vs", "etc", "inc", "ltd", "plc", "co", "corp",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
        "nov", "dec", "approx", "est", "dept", "govt", "mr", "mrs", "ms", "dr",
    }

    @classmethod
    def _is_sentence_break(cls, line: str, position: int) -> bool:
        """True when the full stop at ``position`` really ends a sentence."""
        word = re.search(r"([A-Za-z]+)$", line[:position])
        if word and word.group(1).lower() in cls.ABBREVIATIONS:
            return False
        if word and len(word.group(1)) == 1:
            return False  # an initial, as in "U.S." or "A. Kumar"
        if position and line[position - 1].isdigit() and line[position + 1:position + 2].isdigit():
            return False  # a decimal point or a section number
        after = line[position + 1:position + 3]
        return not after or after[:1].isspace()

    @classmethod
    def _sentence_start(cls, line: str, position: int) -> int:
        for index in range(position - 1, 0, -1):
            if line[index] in ".!?" and cls._is_sentence_break(line, index):
                return index + 1 + (1 if line[index + 1:index + 2] == " " else 0)
        return 0

    @classmethod
    def _sentence_end(cls, line: str, position: int) -> int:
        for index in range(position, len(line)):
            if line[index] in ".!?" and cls._is_sentence_break(line, index):
                return index + 1
        return len(line)

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
            key = (fact["predicate"], fact["value_numeric"], fact["time_period_normalized"],
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
            REJECT_TOC_NAVIGATION: (
                f"'{candidate_text}' appears inside a Table of Contents, Index, or "
                f"navigational page reference rather than an empirical claim."
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
                continue

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

            fingerprint = f"{doc_id}|{page_number}|{predicate_key}|{normalized}|llm"
            facts.append({
                "id": f"fact_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}",
                "document_id": doc_id,
                "subject": subject_display,
                "subject_normalized": subject_key,
                "predicate": predicate_key,
                "predicate_label": predicate_label,
                "value_raw": value_raw,
                "value_numeric": normalized,
                "value_text": str(mantissa),
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
