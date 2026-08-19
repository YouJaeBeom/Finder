"""Tests for LLM relevance ranking logic."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from paper_digest.ranking import _parse_scores, rank_papers
from tests.conftest import make_paper


class TestParseScores:
    def test_parses_json_array_of_dicts(self):
        raw = '[{"id": "0", "score": 8}, {"id": "1", "score": 6}]'
        scores = _parse_scores(raw, 2)
        assert scores == [8.0, 6.0]

    def test_parses_array_of_numbers(self):
        raw = "[7, 5, 9]"
        scores = _parse_scores(raw, 3)
        assert scores == [7.0, 5.0, 9.0]

    def test_strips_markdown_fences(self):
        raw = "```json\n[{\"id\": \"0\", \"score\": 7}]\n```"
        scores = _parse_scores(raw, 1)
        assert scores == [7.0]

    def test_pads_short_response(self):
        raw = "[8]"
        scores = _parse_scores(raw, 3)
        assert len(scores) == 3
        assert scores[0] == 8.0
        assert scores[1] == 0.0

    def test_truncates_long_response(self):
        raw = "[8, 7, 6, 5]"
        scores = _parse_scores(raw, 2)
        assert scores == [8.0, 7.0]

    def test_invalid_json_returns_zeros(self):
        scores = _parse_scores("not json at all", 3)
        assert scores == [0.0, 0.0, 0.0]

    def test_extracts_embedded_json_array(self):
        raw = "Here are the scores: [8, 7] end."
        scores = _parse_scores(raw, 2)
        assert scores == [8.0, 7.0]


class TestRankPapers:
    def _make_mock_provider(self, scores_response: str):
        provider = MagicMock()
        provider.complete.return_value = scores_response
        return provider

    def _make_config(self, top_n: int = 10):
        cfg = MagicMock()
        cfg.research_profile = "LLM alignment research"
        cfg.max_papers_to_rank = 1500
        cfg.top_n = top_n
        cfg.llm.ranking_model = "claude-haiku-4-5"
        return cfg

    def test_returns_top_n_papers(self, sample_papers):
        # Return high scores for all papers
        scores = json.dumps([{"id": str(i), "score": 9 - i % 4} for i in range(20)])
        provider = self._make_mock_provider(scores)
        cfg = self._make_config(top_n=5)

        result = rank_papers(sample_papers, cfg, provider)

        assert len(result) <= 5
        assert len(result) >= 1

    def test_excludes_papers_without_abstract(self):
        papers = [
            make_paper(arxiv_id="1", abstract=None),
            make_paper(arxiv_id="2", abstract="This is a real LLM abstract"),
        ]
        provider = self._make_mock_provider('[{"id": "0", "score": 8}]')
        cfg = self._make_config()

        result = rank_papers(papers, cfg, provider)
        # Only paper with abstract should be eligible
        assert all(p.abstract is not None for p in result)

    def test_papers_sorted_by_score_descending(self, sample_papers):
        # Assign descending scores in reverse order to ensure sort works
        scores_list = [{"id": str(i), "score": i} for i in range(len(sample_papers))]
        provider = self._make_mock_provider(json.dumps(scores_list))
        cfg = self._make_config(top_n=10)

        result = rank_papers(sample_papers, cfg, provider)

        if len(result) >= 2:
            assert result[0].relevance_score >= result[1].relevance_score

    def test_scores_below_the_cutoff_are_a_quiet_week_not_a_fault(self):
        """The model read them and rated them low. That is an answer, not a fault.

        With a 30-day window and per-member deduplication, an ordinary week
        leaves one or two leftover candidates. One of them scoring a 3 must not
        turn the Actions run red — an alert that fires on ordinary weeks is an
        alert people learn to ignore.
        """
        papers = [make_paper(arxiv_id=str(i), abstract="LLM paper")
                  for i in range(3)]
        provider = self._make_mock_provider(
            '[{"id": "0", "score": 1}, {"id": "1", "score": 3}, '
            '{"id": "2", "score": 2}]'
        )

        assert rank_papers(papers, self._make_config(), provider) == []

    def test_an_all_zero_result_is_still_an_anomaly(self):
        """_parse_scores answers [0.0] * count for every failure it hits.

        Bad JSON, no array, an unexpected shape — all of them look like this, so
        an all-zero result means ranking did not happen rather than that the
        model judged nothing relevant.
        """
        papers = [make_paper(arxiv_id=str(i), abstract="LLM paper")
                  for i in range(2)]
        provider = self._make_mock_provider("not json at all")

        with pytest.raises(RuntimeError, match="scored 0.0"):
            rank_papers(papers, self._make_config(), provider)

    def test_a_genuine_all_zero_judgement_also_alerts(self):
        """Indistinguishable from a parse failure, and rare enough to be worth a look."""
        papers = [make_paper(arxiv_id="1", abstract="LLM paper")]
        provider = self._make_mock_provider('[{"id": "0", "score": 0}]')

        with pytest.raises(RuntimeError, match="scored 0.0"):
            rank_papers(papers, self._make_config(), provider)

    def test_empty_list_returns_empty(self):
        provider = self._make_mock_provider("[]")
        cfg = self._make_config()
        result = rank_papers([], cfg, provider)
        assert result == []

    def test_truncates_to_max_papers_to_rank(self):
        papers = [make_paper(arxiv_id=str(i), abstract="LLM paper") for i in range(100)]
        call_count = 0

        def counting_complete(prompt, model, max_tokens=512, system=None):
            nonlocal call_count
            call_count += 1
            # A score for every slot a full batch could hold.
            return json.dumps([{"id": str(j), "score": 8} for j in range(20)])

        provider = MagicMock()
        provider.complete.side_effect = counting_complete
        cfg = self._make_config()
        cfg.max_papers_to_rank = 30  # truncate to 30

        rank_papers(papers, cfg, provider)
        # Should have processed at most 30 papers (2 batches of 20 and 10)
        assert call_count <= 2
