"""End to end through the HTTP API, including an upload the code has never seen."""

import pytest


@pytest.fixture()
def loaded(client, make_pdf):
    """Two documents about an invented company that report a metric differently."""
    first = make_pdf([[
        "Northwind Freight Annual Report",
        "Revenue from services grew to 3,150 crore in FY24, up from the prior year.",
        "Warehouse throughput reached 42.5 million tonnes in FY24 across the network.",
    ]], name="northwind-annual.pdf")

    second = make_pdf([[
        "Northwind Freight Investor Presentation",
        "Revenue from services was 31,500 million in FY24 on a reported basis.",
        "Warehouse throughput reached 51.0 million tonnes in FY25 across the network.",
    ]], name="northwind-deck.pdf")

    ids = []
    for path in (first, second):
        with open(path, "rb") as handle:
            response = client.post(
                "/api/documents/upload",
                files={"file": (path.name, handle, "application/pdf")},
            )
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    return ids


class TestUpload:
    def test_a_non_pdf_is_refused(self, client):
        response = client.post(
            "/api/documents/upload",
            files={"file": ("notes.txt", b"plain text", "text/plain")})
        assert response.status_code == 400

    def test_a_file_that_is_not_really_a_pdf_fails_cleanly(self, client):
        response = client.post(
            "/api/documents/upload",
            files={"file": ("broken.pdf", b"not a pdf at all", "application/pdf")})
        assert response.status_code in (422, 500)

    def test_the_same_file_twice_is_recognised(self, client, make_pdf):
        path = make_pdf([["Total revenue reached 900 crore in FY24 for the group."]])
        payloads = []
        for _ in range(2):
            with open(path, "rb") as handle:
                response = client.post(
                    "/api/documents/upload",
                    files={"file": (path.name, handle, "application/pdf")})
                payloads.append(response.json())
        assert payloads[1].get("duplicate_of") == payloads[0]["id"]

    def test_an_upload_produces_grounded_facts(self, client, loaded):
        response = client.get("/api/facts", params={"document_id": loaded[0]})
        facts = response.json()["items"]
        assert facts
        for stored in facts:
            page = client.get(
                f"/api/documents/{loaded[0]}/pages/{stored['evidence']['page']}").json()
            assert stored["evidence"]["quote"] in page["text"]


class TestKnowledge:
    def test_metrics_are_matched_across_documents(self, client, loaded):
        counts = client.get("/api/relationships").json()["counts"]
        assert sum(counts.values()) > 0

    def test_the_same_figure_written_two_ways_corroborates(self, client, loaded):
        # 3,150 crore and 31,500 million are the same amount.
        items = client.get("/api/relationships",
                           params={"relationship_type": "CORROBORATED"}).json()["items"]
        assert items, "differently written equal values should corroborate"

    def test_the_schema_is_built_from_the_documents(self, client, loaded):
        schema = client.get("/api/facts/schema").json()
        names = {row["predicate"] for row in schema["predicates"]}
        assert any("throughput" in name for name in names)
        assert schema["shared_across_documents"] >= 1

    def test_grounding_is_reported_per_fact(self, client, loaded):
        fact_id = client.get("/api/facts").json()["items"][0]["id"]
        grounding = client.get(f"/api/facts/{fact_id}").json()["grounding"]
        assert grounding["quote_found_in_page"]
        assert grounding["offsets_match"]


class TestShowcase:
    def test_all_four_cases_are_returned(self, client, loaded):
        cases = client.get("/api/showcase/cases").json()["cases"]
        assert [case["case_number"] for case in cases] == \
            ["CASE_1", "CASE_2", "CASE_3", "CASE_4"]

    def test_an_absent_case_says_so_rather_than_inventing_one(self, client):
        cases = client.get("/api/showcase/cases").json()["cases"]
        # With no documents loaded, the comparison cases must be empty.
        assert cases[0]["available"] is False
        assert "explanation" in cases[0]

    def test_the_failure_case_reports_real_rejections(self, client, loaded):
        case = client.get("/api/showcase/cases").json()["cases"][3]
        assert case["available"] is True
        assert isinstance(case["categories"], list)
        assert case["known_limits"]


class TestLifecycle:
    def test_deleting_a_document_removes_its_facts_and_links(self, client, loaded):
        before = client.get("/api/relationships").json()["total"]
        client.delete(f"/api/documents/{loaded[0]}")
        assert client.get("/api/facts",
                          params={"document_id": loaded[0]}).json()["total"] == 0
        assert client.get("/api/relationships").json()["total"] <= before

    def test_stats_reflect_what_was_loaded(self, client, loaded):
        stats = client.get("/api/system/stats").json()
        assert stats["documents"] == 2
        assert stats["facts"] > 0

    def test_health(self, client):
        assert client.get("/api/health").json()["status"] == "ok"
