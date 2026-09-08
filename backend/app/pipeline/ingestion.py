"""PDF ingestion: raw bytes to a canonical, offset-stable text representation.

Every downstream evidence offset points into the canonical page text produced
here, and the canonical text is persisted alongside the document. That is what
makes the grounding guarantee checkable: an evidence quote is accepted only if
it is a literal substring of the stored page text, and the API can hand that
page text back so a reader can verify the quote themselves.
"""

import hashlib
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pypdf

from app.pipeline.normalizer import strip_accents_and_controls

logger = logging.getLogger(__name__)

# A line shorter than this is treated as a standalone block (heading, table
# cell, KPI tile) rather than as part of a flowing paragraph.
SHORT_LINE_CHARS = 45

SENTENCE_END = re.compile(r"[.!?:;][\"'’”\)]?$")
BULLET_START = re.compile(r"^\s*(?:[-•‣▪*·]|\(?\d{1,2}[.)]|[a-z][.)])\s+")
# A short line opening with a figure is a table row or a slide tile. A long one
# is just a wrapped sentence that happened to break before a number.
FIGURE_START = re.compile(r"^\s*[-−(]?\s*(?:US\$|Rs\.?|INR|USD|EUR|GBP|[₹$€£¥])?\s*\d")


class IngestionError(Exception):
    """Raised for files that cannot be turned into usable text."""


def canonicalize_page_text(raw: str) -> str:
    """Reflow extracted PDF text into stable, sentence-friendly lines.

    PDF text layers break lines at typeset width, which splits sentences at
    arbitrary points and makes evidence quotes unreadable. This joins the soft
    breaks inside a paragraph while preserving hard structural breaks, so that:

    * flowing prose becomes one line per paragraph, and
    * slide tiles / table cells stay on their own lines.

    The result is what gets stored, quoted and offset into.
    """
    if not raw:
        return ""

    text = unicodedata.normalize("NFKD", raw)
    text = (
        text.replace("ﬁ", "fi")
        .replace("ﬂ", "fl")
        .replace("ﬀ", "ff")
        .replace("ﬃ", "ffi")
        .replace("ﬄ", "ffl")
    )
    text = strip_accents_and_controls(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of 2+ spaces/tabs (common in layout mode or justified text)
    text = re.sub(r"[ \t ]{2,}", " ", text)
    text = re.sub(r"[ \t ]+", " ", text)

    lines = [ln.strip() for ln in text.split("\n")]
    non_empty = [ln for ln in lines if ln]

    # Detect if page text has severe line fragmentation (e.g. single-word lines)
    is_fragmented = False
    if len(non_empty) > 10:
        single_words = sum(1 for ln in non_empty if len(ln.split()) <= 1)
        if single_words / len(non_empty) > 0.30:
            is_fragmented = True

    # Column width varies wildly between a two-column report (about 45
    # characters) and a slide (about 25). Measuring it per page is what lets one
    # rule serve both: a line that runs close to the page's own full width and
    # does not end a sentence was wrapped by the typesetter, so the next line
    # continues it. A short line was broken on purpose and stands alone.
    wrap_width = _estimate_wrap_width(lines)
    continuation_floor = wrap_width * 0.72

    continuation_words = {
        "and", "or", "to", "in", "of", "for", "with", "at", "by", "from",
        "as", "on", "into", "through", "that", "which", "than", "is", "are", "was", "were"
    }

    def is_table_data_row(ln: str) -> bool:
        tokens = ln.split()
        if len(tokens) < 3:
            return False
        num_tokens = sum(1 for t in tokens if re.match(r"^[-+]?\d+(?:\.\d+)?%?$", t))
        return num_tokens >= 2 and (num_tokens / len(tokens) >= 0.40 or bool(re.search(r"\d+(?:\.\d+)?%?\s+\d+(?:\.\d+)?%?", ln)))

    def is_table_header_line(ln: str) -> bool:
        tokens = ln.split()
        if len(tokens) < 2:
            return False
        if re.match(r"^(?:Table|Figure|Fig\.)\s+\d+", ln, re.IGNORECASE):
            return True
        metric_acronyms = {"FRR", "FAR", "EER", "ACC", "F1", "RMSE", "MAE", "LOSS", "PRECISION", "RECALL", "FNMR", "FMR", "ANIA", "ANGA"}
        if any(t.upper() in metric_acronyms for t in tokens) and not any(t.lower() in {"the", "and", "that", "which", "was", "were", "is", "are"} for t in tokens):
            return True
        if sum(1 for t in tokens if re.match(r"^[A-Z][A-Za-z0-9_+-]*(?:\([0-9]+\))?$", t)) >= 3 and not any(t.endswith((".", "!", "?")) for t in tokens):
            return True
        return False

    out: List[str] = []
    buffer = ""
    last_physical = ""

    def flush() -> None:
        nonlocal buffer, last_physical
        if buffer.strip():
            out.append(buffer.strip())
        buffer = ""
        last_physical = ""

    for line in lines:
        if not line:
            if not is_fragmented or (buffer and SENTENCE_END.search(buffer)):
                flush()
            continue

        is_bullet = bool(BULLET_START.match(line))
        is_figure_tile = bool(FIGURE_START.match(line) and len(line) < SHORT_LINE_CHARS)
        is_prev_figure_tile = bool(FIGURE_START.match(buffer) and len(buffer) < SHORT_LINE_CHARS)
        is_kv = bool(re.match(r"^\s*[A-Z][A-Za-z0-9\s/_\-]{1,30}:", line))
        is_heading = bool(re.match(r"^\s*(?:[IVXLCDM]+\.|\d{1,2}(?:\.\d{1,3})+\s+[A-Z])", line))
        is_t_row = is_table_data_row(line)
        is_buf_t_row = is_table_data_row(buffer) if buffer else False
        is_t_head = is_table_header_line(line)
        is_buf_t_head = is_table_header_line(buffer) if buffer else False
        ends_sentence = bool(SENTENCE_END.search(last_physical))

        # Grammatical continuation signals
        starts_lower = bool(line and line[0].islower())
        last_tokens = last_physical.split()
        ends_cont_word = bool(last_tokens and last_tokens[-1].lower() in continuation_words)
        first_tokens = line.split()
        starts_cont_word = bool(first_tokens and first_tokens[0].lower() in continuation_words)

        length_ok = len(last_physical) >= continuation_floor
        grammar_ok = starts_lower or ends_cont_word or starts_cont_word

        continues = (
            bool(buffer)
            and not is_bullet
            and not is_figure_tile
            and not is_prev_figure_tile
            and not is_kv
            and not is_heading
            and not is_t_row
            and not is_buf_t_row
            and not is_t_head
            and not is_buf_t_head
            and not ends_sentence
            and (length_ok or is_fragmented or grammar_ok)
        )

        if not continues:
            flush()
            buffer = line
            last_physical = line
            continue

        if buffer.endswith("-") and (line[:1].islower() or line[:1].isdigit()):
            buffer = buffer[:-1] + line  # de-hyphenate a word split at the break
        else:
            buffer = f"{buffer} {line}"
        last_physical = line

    flush()
    return "\n".join(out)


def _estimate_wrap_width(lines: List[str]) -> float:
    """Typical full-width line length for a page, ignoring stray long lines."""
    lengths = sorted(len(line) for line in lines if len(line) > 10)
    if not lengths:
        return float(SHORT_LINE_CHARS)
    index = int(len(lengths) * 0.9)
    return float(lengths[min(index, len(lengths) - 1)])


class PDFIngestionPipeline:
    """Reads a PDF page by page, yielding canonical text plus provenance."""

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @classmethod
    def open_reader(cls, file_path: str) -> pypdf.PdfReader:
        path = Path(file_path)
        if not path.exists():
            raise IngestionError(f"File not found: {file_path}")
        if path.stat().st_size == 0:
            raise IngestionError("PDF file is completely empty (0 bytes).")

        try:
            reader = pypdf.PdfReader(file_path)
        except Exception as exc:  # noqa: BLE001 - pypdf raises many types
            raise IngestionError(f"Malformed or corrupt PDF: {exc}") from exc

        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    raise IngestionError("PDF is password protected and cannot be read.")
            except IngestionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise IngestionError(f"PDF is password protected and cannot be read: {exc}") from exc

        if len(reader.pages) == 0:
            raise IngestionError("PDF contains zero pages.")
        return reader

    @classmethod
    def iter_pages(
        cls, file_path: str, max_pages: Optional[int] = None
    ) -> Iterator[Dict[str, Any]]:
        """Stream one page at a time so memory stays flat on large PDFs."""
        reader = cls.open_reader(file_path)
        total = len(reader.pages)
        limit = min(total, max_pages) if max_pages else total

        for index in range(limit):
            page_number = index + 1
            started = time.perf_counter()
            try:
                page_obj = reader.pages[index]
                raw_text = page_obj.extract_text() or ""
                error: Optional[str] = None
            except Exception as exc:  # noqa: BLE001 - never let one page abort a run
                logger.warning("Text extraction failed on page %s of %s: %s",
                               page_number, file_path, exc)
                raw_text, error = "", str(exc)

            canonical = canonicalize_page_text(raw_text)
            yield {
                "page_number": page_number,
                "text": canonical,
                "char_length": len(canonical),
                "is_empty": len(canonical.strip()) == 0,
                "extract_error": error,
                "extract_seconds": round(time.perf_counter() - started, 4),
            }

    @classmethod
    def probe(cls, file_path: str) -> Dict[str, Any]:
        """Cheap metadata read that does not decode any page text."""
        path = Path(file_path)
        reader = cls.open_reader(file_path)
        title = None
        try:
            meta = reader.metadata
            if meta and meta.title:
                title = str(meta.title).strip() or None
        except Exception:  # noqa: BLE001 - metadata is optional
            title = None

        return {
            "filename": path.name,
            "file_path": str(path.resolve()),
            "file_size_bytes": path.stat().st_size,
            "page_count": len(reader.pages),
            "pdf_title": title,
        }

    @classmethod
    def ingest_pdf(cls, file_path: str, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Eagerly read a whole document. Convenience wrapper over ``iter_pages``."""
        info = cls.probe(file_path)
        pages = list(cls.iter_pages(file_path, max_pages=max_pages))
        info.update({
            "file_hash": cls.compute_sha256(file_path),
            "pages": pages,
            "pages_read": len(pages),
            "total_chars": sum(page["char_length"] for page in pages),
            "empty_pages": sum(1 for page in pages if page["is_empty"]),
        })
        return info
