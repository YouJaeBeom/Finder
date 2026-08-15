"""Pushing the keyword filter into the OpenAlex query.

Fetching by concept and date and filtering locally means downloading 930,000
works for a one-year window. Letting OpenAlex search first cuts that to around
9,500.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from paper_digest.collectors.openalex import (
    _TERMS_PER_REQUEST,
    collect_openalex_papers,
    search_terms_from,
)


class TestSearchTermSelection:
    def test_multi_word_phrases_are_sent_to_the_api(self):
        terms = search_terms_from(["political bias", "filter bubble"])
        assert terms == ["filter bubble", "political bias"]

    def test_single_words_are_left_to_the_local_filter(self):
        """"bias" or "dataset" match most of the corpus — sending them undoes
        the point of searching."""
        assert search_terms_from(["bias", "dataset", "LLM"]) == []

    def test_phrases_inside_rules_are_found_too(self):
        terms = search_terms_from([
            {"all": [["large language model", "LLM"], ["political bias"]]},
        ])
        assert terms == ["large language model", "political bias"]

    def test_duplicates_across_rules_are_collapsed(self):
        terms = search_terms_from([
            "political bias",
            {"any": ["political bias", "filter bubble"]},
        ])
        assert terms == ["filter bubble", "political bias"]


class TestRequestShape:
    def _resp(self, results=(), cursor=None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": list(results),
                                  "meta": {"next_cursor": cursor}}
        return resp

    def _collect(self, keywords, **kw):
        with (
            patch("paper_digest.collectors.openalex.requests.get",
                  return_value=self._resp()) as get,
            patch("paper_digest.collectors.openalex.time.sleep"),
        ):
            collect_openalex_papers(keywords, days_back=7, **kw)
        return get

    def test_terms_are_or_ed_into_few_requests(self):
        """One request per phrase is ~95 requests and gets the caller throttled."""
        keywords = [f"phrase number {i}" for i in range(_TERMS_PER_REQUEST * 3)]
        get = self._collect(keywords)
        assert get.call_count == 3

    def test_the_search_filter_carries_the_terms(self):
        get = self._collect(["political bias", "filter bubble"])
        flt = get.call_args.kwargs["params"]["filter"]
        assert "title_and_abstract.search:filter bubble|political bias" in flt

    def test_weekly_asks_by_index_date_not_publication_date(self):
        """A journal issue published in May but indexed today is new to us; a
        publication-date window would miss it permanently."""
        get = self._collect(["political bias"], by_index_date=True)
        assert "from_created_date:" in get.call_args.kwargs["params"]["filter"]

    def test_backfill_asks_by_publication_date(self):
        get = self._collect(["political bias"], by_index_date=False)
        assert "from_publication_date:" in get.call_args.kwargs["params"]["filter"]

    def test_core_sources_only(self):
        get = self._collect(["political bias"])
        assert "primary_location.source.is_core:true" in \
            get.call_args.kwargs["params"]["filter"]

    def test_the_contact_address_is_sent(self):
        """OpenAlex's polite pool gives a far higher rate limit."""
        get = self._collect(["political bias"], mailto="me@lab.ac.kr")
        assert get.call_args.kwargs["params"]["mailto"] == "me@lab.ac.kr"
        assert "me@lab.ac.kr" in get.call_args.kwargs["headers"]["User-Agent"]


class TestRateLimiting:
    def _resp(self, status) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": [], "meta": {}}
        return resp

    def test_a_429_is_retried(self):
        replies = [self._resp(429), self._resp(429), self._resp(200)]
        with (
            patch("paper_digest.collectors.openalex.requests.get",
                  side_effect=replies) as get,
            patch("paper_digest.collectors.openalex.time.sleep"),
        ):
            collect_openalex_papers(["political bias"], days_back=7)
        assert get.call_count == 3

    def test_it_gives_up_without_taking_the_run_down(self):
        with (
            patch("paper_digest.collectors.openalex.requests.get",
                  return_value=self._resp(429)),
            patch("paper_digest.collectors.openalex.time.sleep"),
        ):
            assert collect_openalex_papers(["political bias"], days_back=7) == []
