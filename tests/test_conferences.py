"""Collecting from top conferences.

OpenAlex cannot supply these — its ACL records carry `source: null` and the
conference sources it does have are per-year fragments, so a venue filter there
would drop exactly the venues worth reading. Semantic Scholar has the same
papers with their venue and abstract.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from paper_digest.collectors.conferences import (
    _label_for,
    _paper_url,
    _to_paper,
    collect_conference_papers,
)


def _item(**over) -> dict:
    item = {
        "paperId": "abc123",
        "title": "Measuring Political Bias in LLMs",
        "abstract": "We benchmark political bias across languages.",
        "venue": "Annual Meeting of the Association for Computational Linguistics",
        "publicationDate": "2026-08-12",
        "externalIds": {"DOI": "10.18653/v1/2026.acl-long.1"},
        "authors": [{"name": "Alice Smith"}],
    }
    item.update(over)
    return item


class TestRecordConversion:
    def test_a_proceedings_paper_is_published_not_a_preprint(self):
        paper = _to_paper(_item(), "ACL", "2026-08-15")
        assert paper.venue == "ACL"
        assert paper.venue_status == "published"

    def test_the_doi_becomes_the_link(self):
        paper = _to_paper(_item(), "ACL", "2026-08-15")
        assert paper.url == "https://doi.org/10.18653/v1/2026.acl-long.1"

    def test_publication_date_is_the_papers_own_date(self):
        paper = _to_paper(_item(), "ACL", "2026-08-15")
        assert paper.published_at == "2026-08-12"
        assert paper.collection_date == "2026-08-15"

    def test_a_paper_without_an_abstract_is_dropped(self):
        """Same rule as everywhere else — a title cannot be ranked."""
        assert _to_paper(_item(abstract=""), "ACL", "2026-08-15") is None
        assert _to_paper(_item(abstract=None), "ACL", "2026-08-15") is None

    def test_identifiers_carry_doi_and_arxiv_for_dedup(self):
        paper = _to_paper(_item(externalIds={"DOI": "10.1/x", "ArXiv": "2408.1"}),
                          "ACL", "2026-08-15")
        assert paper.identifiers.doi == "10.1/x"
        assert paper.identifiers.arxiv_id == "2408.1"


class TestLinkFallbacks:
    def test_arxiv_when_there_is_no_doi(self):
        assert _paper_url({"ArXiv": "2408.01234"}, "p1") == \
            "https://arxiv.org/abs/2408.01234"

    def test_semantic_scholar_as_the_last_resort(self):
        assert _paper_url({}, "p1") == "https://www.semanticscholar.org/paper/p1"


class TestVenueLabelling:
    """A batch asks for many venues at once; the reply names each in full."""

    def test_the_abbreviation_is_recovered_from_the_full_name(self):
        assert _label_for(
            "Annual International ACM SIGIR Conference on Research and "
            "Development in Information Retrieval",
            {"ACL": "ACL", "SIGIR": "SIGIR", "CIKM": "CIKM"},
        ) == "SIGIR"

    def test_the_search_name_differs_from_the_abbreviation(self):
        """Semantic Scholar answers to "IEEE Symposium on Security and
        Privacy", not to "S&P" — the column should still read S&P."""
        assert _label_for("IEEE Symposium on Security and Privacy",
                          {"IEEE Symposium on Security and Privacy": "S&P"}) == "S&P"

    def test_a_venue_we_never_asked_for_is_dropped(self):
        """Semantic Scholar's venue filter matches loosely — a batch of
        conferences came back carrying papers from a journal called
        "Languages"."""
        assert _label_for("Languages", {"ACL": "ACL"}) is None
        assert _label_for("", {"ACL": "ACL"}) is None

    def test_the_full_registered_name_maps_to_the_abbreviation(self):
        """"Annual Meeting of the Association for Computational Linguistics"
        does not contain the letters "acl", so a substring test is not enough."""
        assert _label_for(
            "Annual Meeting of the Association for Computational Linguistics",
            {"ACL": "ACL"}) == "ACL"


class TestCollection:
    def _response(self, items) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": items}
        return resp

    def test_venues_are_batched_into_few_requests(self):
        """One request per venue would be 250 requests and minutes of waiting."""
        venues = {f"V{i}": f"V{i}" for i in range(60)}
        with (
            patch("paper_digest.collectors.conferences.requests.get",
                  return_value=self._response([])) as get,
            patch("paper_digest.collectors.conferences.time.sleep"),
        ):
            collect_conference_papers(venues, days_back=7)

        assert get.call_count == 3, "60 venues should be 3 batches of 25"

    def test_a_failing_batch_does_not_lose_the_others(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("500")
            # Venue from the second batch (V25–V29), so it is one we asked for.
            return self._response([_item(venue="V25")])

        with (
            patch("paper_digest.collectors.conferences.requests.get", side_effect=flaky),
            patch("paper_digest.collectors.conferences.time.sleep"),
        ):
            papers = collect_conference_papers({f"V{i}": f"V{i}" for i in range(30)},
                                               days_back=7)

        assert len(papers) == 1, "the second batch still contributed"

    def test_no_venues_means_no_requests(self):
        with patch("paper_digest.collectors.conferences.requests.get") as get:
            assert collect_conference_papers({}, days_back=7) == []
        get.assert_not_called()

    def test_the_date_range_covers_days_back(self):
        with (
            patch("paper_digest.collectors.conferences.requests.get",
                  return_value=self._response([])) as get,
            patch("paper_digest.collectors.conferences.time.sleep"),
        ):
            collect_conference_papers({"ACL": "ACL"}, days_back=7)

        sent = get.call_args.kwargs["params"]["publicationDateOrYear"]
        start, end = sent.split(":")
        from datetime import date
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 7
