"""Tests for generalized numerical counts, semantic facts, and cross-document reconciliation.

Verifies that the knowledge layer handles arbitrary documents without relying on
hard-coded domains, extracting both numerical metrics and semantic assertions while
strictly grounding each fact.
"""

from app.pipeline.comparator import (
    CONTEXTUALLY_DIFFERENT,
    CONTRADICTED,
    CORROBORATED,
    FactComparator,
)
from app.pipeline.extraction import DocumentProfiler, FactExtractionPipeline
from app.pipeline.ingestion import canonicalize_page_text


def extract(text: str, title: str = "Research Project Synopsis"):
    page = canonicalize_page_text(text)
    profile = DocumentProfiler.profile_pages([{"text": page}], title=title)
    facts, issues = FactExtractionPipeline.extract_from_page("doc_test", 1, page, profile)
    return page, facts, issues


class TestGeneralCountExtraction:
    def test_extracts_software_and_benchmark_counts(self):
        page, facts, _ = extract(
            "Evaluation was conducted on a benchmark of 2,294 real GitHub issues "
            "across 12 Python repositories."
        )
        assert len(facts) >= 2
        values = {f["value_numeric"] for f in facts}
        assert 2294.0 in values
        assert 12.0 in values

        # Verify grounding
        for f in facts:
            assert f["evidence_quote"] in page
            assert page[f["char_start"]:f["char_end"]] == f["evidence_quote"]

    def test_extracts_schedule_and_duration_metrics(self):
        page, facts, _ = extract(
            "The experimental plan covers 18 working weeks. The critical path requires 22.34 weeks."
        )
        values = {f["value_numeric"] for f in facts}
        assert 18.0 in values
        assert 22.34 in values


class TestSectionNumberFiltering:
    def test_section_numbering_is_not_extracted_as_count(self):
        # Section numbers like "6.2 Agents and Architecture" must not be parsed as "6.2 Agents"
        _, facts, issues = extract("6.2 Agents and Architecture for the proposed system.")
        assert not any(f.get("value_numeric") == 6.2 for f in facts)
        assert any(i.get("reason_code") == "section_number_not_a_measure" for i in issues)

    def test_table_of_contents_numbering_is_not_extracted_as_count(self):
        _, facts, _ = extract(
            "1. Introduction 3  2. Related Work 5  6.2 Agents and Architecture 9"
        )
        assert not any(f.get("value_numeric") == 6.2 for f in facts)


class TestSemanticFactExtraction:
    def test_extracts_labeled_key_values(self):
        page, facts, _ = extract(
            "Project Title: Autonomous Code Repair Agent\n"
            "Supervisor: Dr. Evelyn Vance\n"
            "Dataset: SWE-bench Lite Benchmark"
        )
        preds = {f["predicate"] for f in facts}
        assert "supervisor" in preds or any("supervisor" in p for p in preds)
        assert any("vance" in f["value_raw"].lower() for f in facts)

        for f in facts:
            assert f["evidence_quote"] in page
            assert page[f["char_start"]:f["char_end"]] == f["evidence_quote"]

    def test_extracts_degree_and_credential_facts(self):
        page, facts, _ = extract(
            "Submitted in partial fulfillment of the requirements for the degree of "
            "Bachelor of Technology in Computer Science and Engineering."
        )
        assert any("degree" in f["predicate"] for f in facts)
        degree_fact = next(f for f in facts if "degree" in f["predicate"])
        assert "bachelor of technology" in degree_fact["value_raw"].lower()

    def test_extracts_system_architecture_relations(self):
        page, facts, _ = extract(
            "The system utilizes Abstract Syntax Tree analysis and multi-agent coordination."
        )
        assert any(f.get("unit_family") == "semantic" for f in facts)
        arch_fact = next(f for f in facts if f.get("unit_family") == "semantic")
        assert "abstract syntax tree" in arch_fact["value_raw"].lower()


class TestSemanticComparator:
    def _make_fact(self, fact_id, doc_id, predicate, value_raw, value_text, period="2024", scope=None):
        return {
            "id": fact_id,
            "document_id": doc_id,
            "subject": "Acme System",
            "subject_normalized": "acme_system",
            "predicate": predicate,
            "predicate_label": predicate.replace("_", " ").title(),
            "value_raw": value_raw,
            "value_numeric": None,
            "value_text": value_text,
            "unit": None,
            "unit_family": "semantic",
            "time_period": period,
            "time_period_normalized": period,
            "scope": scope,
            "qualifier": None,
            "confidence": 0.85,
            "evidence": {
                "document": f"{doc_id}.pdf",
                "page": 1,
                "quote": value_raw,
                "char_start": 0,
                "char_end": len(value_raw),
            },
            "extraction_metadata": {},
        }

    def test_corroborates_identical_or_paraphrased_semantic_assertions(self):
        f1 = self._make_fact(
            "f1", "doc1", "core_architecture",
            "Multi-agent system with localized AST navigation",
            "multi agent system with localized ast navigation"
        )
        f2 = self._make_fact(
            "f2", "doc2", "core_architecture",
            "Multi-agent architecture using AST navigation",
            "multi agent architecture using ast navigation"
        )
        rel = FactComparator.compare_facts(f1, f2)
        assert rel is not None
        assert rel["relationship_type"] == CORROBORATED
        assert "corroborat" in rel["reasoning"].lower() or "agree" in rel["reasoning"].lower()

    def test_detects_contradiction_for_mutually_exclusive_assertions(self):
        f1 = self._make_fact(
            "f1", "doc1", "system_mode",
            "The system is fully autonomous with no human intervention",
            "fully autonomous without human intervention"
        )
        f2 = self._make_fact(
            "f2", "doc2", "system_mode",
            "The system requires mandatory human review and approval",
            "mandatory human review and approval"
        )
        rel = FactComparator.compare_facts(f1, f2)
        assert rel is not None
        assert rel["relationship_type"] == CONTRADICTED

    def test_identifies_contextual_difference_across_periods_or_scopes(self):
        f1 = self._make_fact(
            "f1", "doc1", "supported_runtimes",
            "Python 3.10 and 3.11",
            "python 3 10 and 3 11",
            period="2023"
        )
        f2 = self._make_fact(
            "f2", "doc2", "supported_runtimes",
            "Python 3.11 and 3.12",
            "python 3 11 and 3 12",
            period="2024"
        )
        rel = FactComparator.compare_facts(f1, f2)
        assert rel is not None
        assert rel["relationship_type"] == CONTEXTUALLY_DIFFERENT


class TestMultilineAndTableGeneralization:
    def test_multiline_unit_extraction_and_grounding(self):
        page, facts, _ = extract(
            "Recommended Hardware Requirements\n"
            "Apple M2 RAM\n"
            "16\n"
            "GB, which helps when indexing larger repositories\n"
            "Storage\n"
            "512\n"
            "GB SSD\n"
            "The embedding model is small (about\n"
            "90\n"
            "MB) and runs fine on CPU."
        )
        assert len(facts) >= 3
        ram_fact = next(f for f in facts if f["unit"] == "gb" and f["value_numeric"] == 16.0)
        assert "RAM" in ram_fact["predicate_label"]
        assert ram_fact["evidence_quote"] in page
        assert page[ram_fact["char_start"]:ram_fact["char_end"]] == ram_fact["evidence_quote"]

        storage_fact = next(f for f in facts if f["unit"] == "gb" and f["value_numeric"] == 512.0)
        assert "Storage" in storage_fact["predicate_label"]
        assert storage_fact["evidence_quote"] in page
        assert page[storage_fact["char_start"]:storage_fact["char_end"]] == storage_fact["evidence_quote"]

        emb_fact = next(f for f in facts if f["unit"] == "mb" and f["value_numeric"] == 90.0)
        assert emb_fact["evidence_quote"] in page
        assert page[emb_fact["char_start"]:emb_fact["char_end"]] == emb_fact["evidence_quote"]

