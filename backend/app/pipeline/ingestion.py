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


def _is_heading(line: str) -> bool:
    """True when a line reads as a heading rather than as wrapped prose.

    A heading is set in title case and carries no terminal punctuation. Joining
    one into the sentence below it merges a document's own title into its first
    claim, and a metric then gets named after the title.
    """
    if SENTENCE_END.search(line):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", line)
    if len(words) < 2:
        return False
    capitalised = sum(1 for word in words if word[0].isupper())
    return capitalised / len(words) >= 0.6


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

    text = strip_accents_and_controls(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)

    lines = [ln.strip() for ln in text.split("\n")]

    # Column width varies wildly between a two-column report (about 45
    # characters) and a slide (about 25). Measuring it per page is what lets one
    # rule serve both: a line that runs close to the page's own full width and
    # does not end a sentence was wrapped by the typesetter, so the next line
    # continues it. A short line was broken on purpose and stands alone.
    wrap_width = _estimate_wrap_width(lines)
    continuation_floor = wrap_width * 0.72

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
            flush()
            continue

        continues = (
            bool(buffer)
            and not _is_heading(last_physical)
            and not BULLET_START.match(line)
            and not (FIGURE_START.match(line) and len(line) < SHORT_LINE_CHARS)
            and len(last_physical) >= continuation_floor
            and not SENTENCE_END.search(last_physical)
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
                raw_text = reader.pages[index].extract_text() or ""
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
