"""Collecting papers from the venue allowlist, via Semantic Scholar.

This is the only paper source. Everything here guards the same property: a
paper is filed under a venue somebody actually asked for, or it is dropped.
"""
from __future__ import annotations

import requests
from unittest.mock import MagicMock, patch

from paper_digest.collectors.semantic_scholar import (
    _label_for,
    _paper_url,
    _to_paper,
    collect_venue_papers,
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
        paper = _to_paper(_item(), "ACL", "2026-08-15", "conference")
        assert paper.venue == "ACL"
        assert paper.venue_status == "published"

    def test_the_doi_becomes_the_link(self):
        paper = _to_paper(_item(), "ACL", "2026-08-15", "conference")
        assert paper.url == "https://doi.org/10.18653/v1/2026.acl-long.1"

    def test_publication_date_is_the_papers_own_date(self):
        paper = _to_paper(_item(), "ACL", "2026-08-15", "conference")
        assert paper.published_at == "2026-08-12"
        assert paper.collection_date == "2026-08-15"

    def test_a_paper_without_an_abstract_is_dropped(self):
        """Same rule as everywhere else — a title cannot be ranked."""
        assert _to_paper(_item(abstract=""), "ACL", "2026-08-15", "conference") is None
        assert _to_paper(_item(abstract=None), "ACL", "2026-08-15", "conference") is None

    def test_identifiers_carry_doi_and_arxiv_for_dedup(self):
        paper = _to_paper(_item(externalIds={"DOI": "10.1/x", "ArXiv": "2408.1"}),
                          "ACL", "2026-08-15", "conference")
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
    def _response(self, items, status: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": items}
        return resp

    def test_venues_are_batched_into_few_requests(self):
        """One request per venue would be 250 requests and minutes of waiting."""
        venues = {f"V{i}": f"V{i}" for i in range(60)}
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._response([])) as get,
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            collect_venue_papers(venues, days_back=7)

        assert get.call_count == 3, "60 venues should be 3 batches of 25"

    def test_a_failing_batch_does_not_lose_the_others(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 5:  # first batch exhausts its retries
                raise requests.ConnectionError("network down")
            # Venue from the second batch (V25–V29), so it is one we asked for.
            return self._response([_item(venue="V25")])

        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get", side_effect=flaky),
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            papers = collect_venue_papers({f"V{i}": f"V{i}" for i in range(30)},
                                               days_back=7)

        assert len(papers) == 1, "the second batch still contributed"

    def test_no_venues_means_no_requests(self):
        with patch("paper_digest.collectors.semantic_scholar.requests.get") as get:
            assert collect_venue_papers({}, days_back=7) == []
        get.assert_not_called()

    def test_the_date_range_covers_days_back(self):
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._response([])) as get,
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            collect_venue_papers({"ACL": "ACL"}, days_back=7)

        sent = get.call_args.kwargs["params"]["publicationDateOrYear"]
        start, end = sent.split(":")
        from datetime import date
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 7


class TestRateLimiting:
    """A year-long backfill lost three of four batches to 429s, taking ACL,
    NeurIPS, CHI, SIGIR and CVPR with them. A 429 means "later", not "never"."""

    def _resp(self, status: int, items=()) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": list(items)}
        return resp

    def test_a_429_is_retried_rather_than_dropped(self):
        replies = [self._resp(429), self._resp(429), self._resp(200, [_item()])]

        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  side_effect=replies) as get,
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            papers = collect_venue_papers({"ACL": "ACL"}, days_back=7)

        assert get.call_count == 3
        assert len(papers) == 1, "the batch survived the throttling"

    def test_server_errors_are_retried_too(self):
        replies = [self._resp(503), self._resp(200, [_item()])]
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  side_effect=replies),
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            assert len(collect_venue_papers({"ACL": "ACL"}, days_back=7)) == 1

    def test_it_gives_up_eventually_without_taking_the_run_down(self):
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._resp(429)) as get,
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            assert collect_venue_papers({"ACL": "ACL"}, days_back=7) == []
        assert get.call_count == 5, "bounded retries, not an infinite loop"

    def test_backoff_grows_between_attempts(self):
        waits = []
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._resp(429)),
            patch("paper_digest.collectors.semantic_scholar.time.sleep",
                  side_effect=waits.append),
        ):
            collect_venue_papers({"ACL": "ACL"}, days_back=7)

        retry_waits = [w for w in waits if w >= 5.0]
        assert retry_waits == sorted(retry_waits) and len(set(retry_waits)) > 1


class TestPagination:
    """One response carries at most 1,000 papers. A year of the twelve
    best-known venues alone is 13,544."""

    def _page(self, items, token=None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        body = {"data": list(items)}
        if token:
            body["token"] = token
        resp.json.return_value = body
        return resp

    def test_it_follows_the_continuation_token(self):
        pages = [self._page([_item()], token="NEXT"),
                 self._page([_item()], token="NEXT2"),
                 self._page([_item()])]
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  side_effect=pages) as get,
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            papers = collect_venue_papers({"ACL": "ACL"}, days_back=365)

        assert len(papers) == 3
        assert get.call_count == 3
        assert get.call_args_list[1].kwargs["params"]["token"] == "NEXT"

    def test_it_stops_when_the_token_runs_out(self):
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._page([_item()])) as get,
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            collect_venue_papers({"ACL": "ACL"}, days_back=365)
        assert get.call_count == 1

    def test_max_results_bounds_the_paging(self):
        endless = self._page([_item()] * 10, token="MORE")
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=endless),
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
        ):
            papers = collect_venue_papers({"ACL": "ACL"}, days_back=365,
                                               max_results=25)
        assert len(papers) == 25


class TestJournalsNeedAnExactVenue:
    """Semantic Scholar's venue filter matches loosely, and for journals that
    is dangerous in both directions.

    Asking for "Big Data & Society" comes back carrying a different journal
    called "Big Data". Asking for "Artificial Intelligence" would file every
    paper from "Artificial Intelligence Review" under it. Both were observed
    against the live API. Journals answer with their exact registered name, so
    requiring one costs nothing and removes the whole class of mislabelling.
    """

    JOURNALS = {
        "Big Data & Society": "BDS",
        "Artificial Intelligence": "AIJ",
    }

    def test_the_exact_name_is_accepted(self):
        assert _label_for("Big Data & Society", self.JOURNALS, exact=True) == "BDS"

    def test_case_does_not_matter(self):
        assert _label_for("big data & society", self.JOURNALS, exact=True) == "BDS"

    def test_a_different_journal_with_a_containing_name_is_dropped(self):
        """"Big Data" is not "Big Data & Society" — it is another journal."""
        assert _label_for("Big Data", self.JOURNALS, exact=True) is None

    def test_a_different_journal_with_an_extended_name_is_dropped(self):
        assert _label_for("Artificial Intelligence Review", self.JOURNALS,
                          exact=True) is None

    def test_conferences_still_match_loosely(self):
        """Their returned names carry an ordinal, a year and a host city, so an
        exact match would drop every one of them."""
        conferences = {"ACL": "ACL"}
        loose = "Proceedings of the 62nd Annual Meeting of the ACL (Bangkok)"
        assert _label_for(loose, conferences, exact=False) == "ACL"
        assert _label_for(loose, conferences, exact=True) is None


class TestSourceLabel:
    def test_the_label_lands_on_the_paper(self):
        """Reports and dedup both read Paper.source; a journal filed as a
        conference makes the run report lie about where its papers came from."""
        assert _to_paper(_item(), "TOIS", "2026-08-15", "journal").source == ["journal"]
        assert _to_paper(_item(), "ACL", "2026-08-15", "conference").source == ["conference"]


class TestCoverageLossIsNamed:
    """A run that could not reach a venue has to say which one.

    The venue allowlist *is* the coverage, so a batch dropping out means a whole
    conference contributed nothing. The old log line said "gave up on SIGGRAPH…"
    and hid the other 24 venues in the request — which is exactly what someone
    staring at a thin week needs to know.
    """

    def _fail(self, status: int = 404) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
        return resp

    def _ok(self, items=()) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": list(items), "token": None}
        return resp

    def test_the_lost_venues_are_listed_by_name(self, caplog):
        venues = {"Conference A": "A", "Conference B": "B"}
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._fail()),
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
            caplog.at_level("WARNING"),
        ):
            papers = collect_venue_papers(venues, days_back=7)

        assert papers == []
        assert "2 of 2 venues could not be fetched" in caplog.text
        assert "A, B" in caplog.text

    def test_a_successful_run_says_nothing_about_unreachable_venues(self, caplog):
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._ok([_item()])),
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
            caplog.at_level("WARNING"),
        ):
            papers = collect_venue_papers({"ACL": "ACL"}, days_back=7)

        assert len(papers) == 1
        assert "could not be fetched" not in caplog.text

    def test_the_summary_counts_only_the_venues_that_answered(self, caplog):
        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  return_value=self._fail()),
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
            caplog.at_level("INFO"),
        ):
            collect_venue_papers({"A": "A", "B": "B"}, days_back=7)

        assert "from 0 of 2 venues" in caplog.text, (
            "reporting 2 venues when neither answered reads as a quiet week"
        )

    def test_a_failure_partway_through_pagination_keeps_what_arrived(self, caplog):
        """Different loss from losing the batch, and said differently."""
        first = self._ok([_item()])
        first.json.return_value = {"data": [_item()], "token": "next-page"}
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            return first if calls["n"] == 1 else self._fail()

        with (
            patch("paper_digest.collectors.semantic_scholar.requests.get",
                  side_effect=flaky),
            patch("paper_digest.collectors.semantic_scholar.time.sleep"),
            caplog.at_level("WARNING"),
        ):
            papers = collect_venue_papers({"ACL": "ACL"}, days_back=7)

        assert len(papers) == 1, "the first page must survive"
        assert "stopped after 1 page(s)" in caplog.text
        assert "could not be fetched" not in caplog.text


class TestBatchLabel:
    def test_a_small_batch_is_listed_in_full(self):
        from paper_digest.collectors.semantic_scholar import _batch_label

        assert _batch_label(["ACL", "EMNLP"]) == "ACL, EMNLP"

    def test_a_large_batch_shows_its_range_and_size(self):
        from paper_digest.collectors.semantic_scholar import _batch_label

        label = _batch_label([f"V{i}" for i in range(25)])
        assert label == "V0 … V24 (25 venues)"
