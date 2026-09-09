"""Extraction on documents the code has never seen.

Every document here is about invented companies and countries, so nothing in the
extractor can have been written with these facts in mind. If extraction works
here, it is not keyed to the starter dataset.
"""

from app.pipeline.extraction import DocumentProfiler, FactExtractionPipeline
from app.pipeline.ingestion import canonicalize_page_text


def extract(text, title="Northwind Freight Annual Report"):
    page = canonicalize_page_text(text)
    profile = DocumentProfiler.profile_pages([{"text": page}], title=title)
    facts, issues = FactExtractionPipeline.extract_from_page("doc1", 1, page, profile)
    return page, facts, issues


def predicates(facts):
    return {fact["predicate"] for fact in facts}


class TestGenericExtraction:
    def test_reads_a_metric_from_prose_about_an_invented_company(self):
        _, facts, _ = extract(
            "Northwind Freight reported that warehouse throughput reached 42.5 million "
            "tonnes in FY24, the highest on record."
        )
        fact = next(f for f in facts if "throughput" in f["predicate"])
        assert fact["value_numeric"] == 42_500_000
        assert fact["time_period_normalized"] == "2023-2024"

    def test_reads_a_metric_stated_after_the_number(self):
        _, facts, _ = extract("880 Mn parcels delivered in FY23 across the network.")
        assert any("parcel" in predicate for predicate in predicates(facts))

    def test_reads_a_caption_under_a_standalone_figure(self):
        # The layout of a slide: the number on one line, its label on the next.
        _, facts, _ = extract("₹3,150 Cr\nFY24 revenue from services\nYoY: 18.2%")
        assert any("revenue" in predicate for predicate in predicates(facts))

    def test_metric_names_come_from_the_page_not_a_fixed_list(self):
        _, facts, _ = extract(
            "Zubrowka's sturgeon caviar output reached 91.4 tonnes in 2023."
        )
        assert any("caviar" in predicate for predicate in predicates(facts))

    def test_subject_is_learned_from_the_document(self):
        _, facts, _ = extract(
            "Total income grew to 2,400 crore in FY24. Operating cost fell to 1,900 crore "
            "in FY24.", title="Northwind Freight Annual Report")
        assert all(fact["subject_normalized"] == "northwind_freight" for fact in facts)


class TestFiltering:
    def test_a_number_with_no_unit_is_not_a_fact(self):
        _, facts, issues = extract("See section 4 on page 17 for details.")
        assert facts == []
        assert all(issue["is_routine_filter"] for issue in issues)

    def test_a_year_is_not_a_measurement(self):
        _, facts, _ = extract("The programme was launched in 2019 and expanded later.")
        assert facts == []

    def test_a_label_made_only_of_units_is_rejected(self):
        # A table whose only nearby caption is the unit says how the figure is
        # measured, but never what it measures.
        _, _, issues = extract(
            "Statement of results for the period under review is set out below.\ncrore\n"
            "250 crore reported")
        assert any(issue["reason_code"] == "label_names_a_unit_not_a_metric"
                   for issue in issues)


class TestGrounding:
    def test_every_quote_is_literally_in_the_page(self):
        page, facts, _ = extract(
            "Revenue from services rose to ₹8,142 Cr in FY24. Freight tonnage reached "
            "1.4 million tonnes in FY24, while parcel volume was 740 million in FY24."
        )
        assert facts
        for fact in facts:
            assert fact["evidence_quote"] in page

    def test_offsets_point_at_the_quote(self):
        page, facts, _ = extract("Warehouse capacity stood at 5.2 million tonnes in FY24.")
        for fact in facts:
            assert page[fact["char_start"]:fact["char_end"]] == fact["evidence_quote"]


class TestProvenanceFlags:
    def test_a_period_taken_from_the_document_is_flagged(self):
        _, facts, _ = extract(
            "The results for FY24 are set out in the paragraphs that follow here.\n\n"
            "Operating margin for the segment stood at 4.5 per cent on a reported basis.")
        inherited = [f for f in facts
                     if f["extraction_metadata"]["period_inferred_from_document"]]
        assert inherited, "a sentence with no period of its own should be flagged"

    def test_a_period_in_the_sentence_is_not_flagged(self):
        _, facts, _ = extract("Operating margin stood at 4.5 per cent in FY24.")
        assert not facts[0]["extraction_metadata"]["period_inferred_from_document"]


class TestReflow:
    def test_a_wrapped_paragraph_becomes_one_line(self):
        # Two column reports wrap at a fixed width; sentences must survive it.
        wrapped = ("Although real gross domestic product growth moderated to\n"
                   "6.5 per cent in 2024-25, the economy remained resilient\n"
                   "through the year despite external pressures on trade.")
        assert canonicalize_page_text(wrapped).count("\n") == 0

    def test_short_standalone_lines_are_kept_apart(self):
        tiles = "₹8,142 Cr\nFY24 revenue from services\n740 Mn\nParcels shipped"
        assert canonicalize_page_text(tiles).count("\n") == 3

    def test_surrogate_characters_do_not_crash_pipeline(self):
        corrupt_text = "Operating profit reached \udc5a $50 million in 2024."
        page, facts, _ = extract(corrupt_text)
        assert "\udc5a" not in page
        # Must safely encode to UTF-8 without raising UnicodeEncodeError
        encoded = page.encode("utf-8")
        assert b"$50 million" in encoded


class TestTableOfContentsSkipping:
    def test_table_of_contents_page_is_skipped(self):
        toc_text = (
            "TABLE OF CONTENTS\n"
            "Section I: General Overview ................................. 1\n"
            "Section II: Financial Performance (At 2011-12 prices) ...... 15\n"
            "Section III: Balance Sheet and Accounts .................... 45\n"
            "Appendix Tables ........................................... 120\n"
        )
        assert FactExtractionPipeline.is_table_of_contents_page(toc_text)
        facts, issues = FactExtractionPipeline.extract_from_page(
            doc_id="test_doc", page_number=1, page_text=toc_text
        )
        assert len(facts) == 0
        assert any(i["reason_code"] == "table_of_contents_or_index_navigation" for i in issues)

    def test_dot_leader_line_in_mixed_page_is_rejected(self):
        mixed_text = (
            "Operating revenue reached $150 million in 2024.\n"
            "Detailed Segment Analysis ................................. 45\n"
        )
        facts, _ = FactExtractionPipeline.extract_from_page(
            doc_id="test_doc", page_number=2, page_text=mixed_text
        )
        assert len(facts) == 1
        assert facts[0]["value_raw"] == "$150 million"
