"""The pipeline on documents from domains it was never built against.

Every document here is invented, and none of them is about logistics, India or
anything the starter dataset covers. They exist to catch the failure mode the
system is most exposed to: a rule that works because it was written while
looking at one particular set of PDFs.

A rule that only holds for reports about companies would pass the rest of the
suite and fail here, which is the point.
"""

import pytest

from app.pipeline.extraction import DocumentProfiler, FactExtractionPipeline
from app.pipeline.ingestion import PDFIngestionPipeline
from app.pipeline.comparator import CONTRADICTED, CORROBORATED, FactComparator


HOSPITAL = [[
    "Northfield Regional Health System",
    "Annual Quality and Safety Report 2024",
    "",
    "1.1 The System recorded 412,600 outpatient visits in 2024, an increase of 7.2 per cent",
    "over the 384,900 visits recorded in 2023.",
    "1.2 The average length of stay for inpatient admissions declined to 4.3 days in 2024",
    "from 4.8 days in 2023.",
    "1.3 The 30-day readmission rate stood at 11.4 per cent in 2024.",
    "2.1 Net patient service revenue grew to $1,402 million in 2024 from $1,318 million in 2023.",
]]

REGULATOR = [[
    "State Health Commission",
    "Independent Audit of Northfield Regional Health System, 2024",
    "",
    "The 30-day readmission rate for Northfield Regional Health System was 12.8 per cent",
    "in 2024 on an audited basis.",
    "Average length of stay was 4.3 days in 2024.",
]]

ENERGY = [[
    "GridCo Renewables plc",
    "Operating Review for the year ended 31 March 2025",
    "",
    "Total renewable generation reached 14.8 TWh in FY2024-25.",
    "Installed wind capacity stood at 3,240 MW as at 31 March 2025.",
    "Revenue rose by 18.4 per cent to EUR 2,940 million in FY2024-25.",
]]

UNIVERSITY = [[
    "Ashcombe University",
    "Enrolment and Outcomes Report, Academic Year 2024-25",
    "",
    "Total enrolment reached 27,450 students in 2024-25.",
    "The first-year retention rate was 91.2 per cent in 2024-25.",
    "Research income totalled GBP 148.6 million in 2024-25.",
]]


def read(path):
    """Run one PDF through ingestion, profiling and extraction."""
    info = PDFIngestionPipeline.ingest_pdf(str(path))
    title = info.get("pdf_title") or path.stem.replace("-", " ")
    profile = DocumentProfiler.profile_pages(info["pages"], title=title)
    facts = []
    for page in info["pages"]:
        found, _ = FactExtractionPipeline.extract_from_page(
            path.name, page["page_number"], page["text"], profile, title)
        facts.extend(found)
    return profile, facts


def value_for(facts, needle):
    """The normalized value of the fact whose quote contains ``needle``."""
    for fact in facts:
        if needle in fact["evidence_quote"] and needle in fact["value_raw"]:
            return fact["value_numeric"]
    return None


class TestUnitsAreReadFromShapeNotFromAList:
    """A document may count anything; nothing may need to be listed first."""

    @pytest.mark.parametrize("pages, quantity, expected", [
        (HOSPITAL, "412,600", 412600.0),      # a counted noun no report has used before
        (HOSPITAL, "4.3", 4.3),               # a duration
        (ENERGY, "14.8", 14.8),               # an SI symbol
        (ENERGY, "3,240", 3240.0),
        (UNIVERSITY, "27,450", 27450.0),
    ])
    def test_a_counted_quantity_becomes_a_fact(self, make_pdf, pages, quantity, expected):
        _, facts = read(make_pdf(pages))
        assert value_for(facts, quantity) == expected


class TestTheSubjectIsFoundInTheDocument:
    @pytest.mark.parametrize("pages, expected", [
        (HOSPITAL, "Northfield Regional Health System"),
        (ENERGY, "GridCo Renewables"),
        (UNIVERSITY, "Ashcombe University"),
    ])
    def test_a_multi_word_name_is_the_subject(self, make_pdf, pages, expected):
        profile, _ = read(make_pdf(pages))
        assert expected in profile.default_subject

    def test_two_documents_about_one_subject_agree_on_it(self, make_pdf):
        first, _ = read(make_pdf(HOSPITAL, name="northfield-quality.pdf"))
        second, _ = read(make_pdf(REGULATOR, name="northfield-audit.pdf"))
        # The audit is published by the Commission but is about the System, and
        # its figures cannot be compared with the System's own unless the two
        # documents are filed under the same subject.
        assert "Northfield" in first.default_subject
        assert "Northfield" in second.default_subject


class TestEvidenceStaysInsideItsSentence:
    def test_a_quote_is_a_whole_sentence(self, make_pdf):
        _, facts = read(make_pdf(HOSPITAL))
        for fact in facts:
            quote = fact["evidence_quote"].strip()
            assert not quote.startswith((",", "-", "and ", "of ")), quote

    def test_a_period_is_not_taken_from_the_figure_it_is_compared_with(self, make_pdf):
        _, facts = read(make_pdf(HOSPITAL))
        for fact in facts:
            # "grew to $1,402 million in 2024 from $1,318 million in 2023"
            if fact["value_raw"].replace(" ", "").endswith("1,318million"):
                assert fact["time_period_normalized"] == "2023"


class TestComparisonWorksOnUnseenSubjects:
    def test_a_conflict_and_an_agreement_are_both_found(self, make_pdf):
        _, hospital = read(make_pdf(HOSPITAL, name="northfield-quality.pdf"))
        _, audit = read(make_pdf(REGULATOR, name="northfield-audit.pdf"))
        verdicts = {relationship["relationship_type"]
                    for relationship in FactComparator.compare_all(hospital + audit)}
        # 11.4 per cent against 12.8 per cent for the same metric and period.
        assert CONTRADICTED in verdicts
        # 4.3 days in both documents.
        assert CORROBORATED in verdicts

    def test_documents_about_different_subjects_are_not_linked(self, make_pdf):
        _, energy = read(make_pdf(ENERGY, name="gridco.pdf"))
        _, university = read(make_pdf(UNIVERSITY, name="ashcombe.pdf"))
        assert FactComparator.compare_all(energy + university) == []
