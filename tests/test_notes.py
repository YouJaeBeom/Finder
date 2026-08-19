"""Tests for Korean research note generation and structure."""
from __future__ import annotations

import time

import json
from unittest.mock import MagicMock


from paper_digest.notes import _parse_note, generate_note, generate_notes
from paper_digest.models import ResearchNote
from tests.conftest import make_paper

SAMPLE_NOTE_JSON = {
    "one_line_summary": "이 논문은 RLHF를 통해 LLM 정렬을 향상시키는 새로운 방법을 제안합니다.",
    "key_contributions": [
        "새로운 보상 모델 설계 방법 제안",
        "기존 RLHF 대비 성능 20% 향상",
        "다양한 벤치마크에서 최고 성능 달성",
    ],
    "method": "제안된 방법은 인간 선호 데이터를 사용하여 보상 모델을 학습하고, "
              "PPO 알고리즘으로 언어 모델을 파인튜닝합니다.",
    "relevance_to_profile": "이 논문은 내 연구의 핵심 주제인 LLM 정렬과 직접적으로 관련됩니다. "
                            "특히 RLHF 기법의 개선은 내 연구에서 탐구하는 안전한 AI 시스템 구축에 "
                            "중요한 통찰을 제공합니다.",
}


class TestParseNote:
    def test_parses_valid_json(self):
        raw = json.dumps(SAMPLE_NOTE_JSON)
        note = _parse_note(raw)
        assert note is not None
        assert note.one_line_summary == SAMPLE_NOTE_JSON["one_line_summary"]
        assert len(note.key_contributions) == 3
        assert note.method == SAMPLE_NOTE_JSON["method"]
        assert note.relevance_to_profile == SAMPLE_NOTE_JSON["relevance_to_profile"]

    def test_strips_markdown_fences(self):
        raw = f"```json\n{json.dumps(SAMPLE_NOTE_JSON)}\n```"
        note = _parse_note(raw)
        assert note is not None
        assert note.one_line_summary

    def test_pads_key_contributions_to_3(self):
        data = dict(SAMPLE_NOTE_JSON)
        data["key_contributions"] = ["Only one contribution"]
        note = _parse_note(json.dumps(data))
        assert note is not None
        assert len(note.key_contributions) == 3

    def test_truncates_key_contributions_to_3(self):
        data = dict(SAMPLE_NOTE_JSON)
        data["key_contributions"] = ["a", "b", "c", "d", "e"]
        note = _parse_note(json.dumps(data))
        assert note is not None
        assert len(note.key_contributions) == 3

    def test_extracts_json_from_surrounding_text(self):
        raw = f"Here is the note:\n{json.dumps(SAMPLE_NOTE_JSON)}\nThat's it."
        note = _parse_note(raw)
        assert note is not None

    def test_returns_none_on_invalid_json(self):
        note = _parse_note("not json at all !!!")
        assert note is None


class TestResearchNoteSectionsFilledCount:
    def test_all_four_sections(self):
        note = ResearchNote(
            one_line_summary="요약",
            key_contributions=["기여1", "기여2", "기여3"],
            method="방법",
            relevance_to_profile="연결점",
        )
        assert note.sections_filled_count() == 4

    def test_missing_section(self):
        note = ResearchNote(
            one_line_summary="요약",
            key_contributions=[],  # empty — doesn't count
            method="방법",
            relevance_to_profile="연결점",
        )
        assert note.sections_filled_count() == 3

    def test_empty_note(self):
        note = ResearchNote()
        assert note.sections_filled_count() == 0


class TestGenerateNote:
    def test_generates_note_with_four_sections(self):
        paper = make_paper(arxiv_id="2401.00001")
        cfg = MagicMock()
        cfg.research_profile = "LLM alignment"
        cfg.llm.notes_model = "claude-opus-5"

        provider = MagicMock()
        provider.complete.return_value = json.dumps(SAMPLE_NOTE_JSON)

        note = generate_note(paper, cfg, provider)
        assert note.sections_filled_count() == 4

    def test_uses_fallback_on_parse_failure(self):
        paper = make_paper(arxiv_id="2401.00001")
        cfg = MagicMock()
        cfg.research_profile = "LLM alignment"
        cfg.llm.notes_model = "claude-opus-5"

        provider = MagicMock()
        provider.complete.return_value = "garbage response"

        note = generate_note(paper, cfg, provider)
        # Fallback note still has all 4 sections filled
        assert note.sections_filled_count() == 4


class TestConcurrentNoteGeneration:
    """Notes are written side by side. Each has to land on its own paper.

    This is where a run spends nearly all its time — one call per paper at ~25
    seconds — so serial generation put a full lab past the workflow timeout.
    """

    def _cfg(self, concurrency):
        cfg = MagicMock()
        cfg.research_profile = "LLM bias research"
        cfg.llm.notes_model = "m"
        cfg.llm.concurrency = concurrency
        return cfg

    def _note_for(self, title):
        return json.dumps({
            "one_line_summary": f"summary of {title}",
            "key_contributions": ["a", "b", "c"],
            "method": "method",
            "relevance_to_profile": "relevance",
        })

    def test_each_note_lands_on_the_paper_it_describes(self):
        papers = [make_paper(arxiv_id=str(i), title=f"Paper {i}",
                             abstract=f"Abstract {i}") for i in range(24)]

        def reply(prompt, model, max_tokens=512, system=None):
            title = prompt.split("제목: ")[1].split("\n")[0].strip()
            time.sleep(0.01 * (int(title.split()[-1]) % 3))  # out of order
            return self._note_for(title)

        provider = MagicMock()
        provider.complete.side_effect = reply

        generate_notes(papers, self._cfg(concurrency=6), provider)

        for paper in papers:
            assert paper.research_note is not None
            assert paper.title in paper.research_note.one_line_summary, (
                f"{paper.title!r} got a note about something else"
            )

    def test_one_failure_does_not_cost_the_others(self):
        papers = [make_paper(arxiv_id=str(i), title=f"Paper {i}",
                             abstract=f"Abstract {i}") for i in range(12)]

        def reply(prompt, model, max_tokens=512, system=None):
            title = prompt.split("제목: ")[1].split("\n")[0].strip()
            if title == "Paper 5":
                raise RuntimeError("refused")
            return self._note_for(title)

        provider = MagicMock()
        provider.complete.side_effect = reply

        generate_notes(papers, self._cfg(concurrency=4), provider)

        assert all(p.research_note is not None for p in papers)
        failed = [p for p in papers if p.title == "Paper 5"][0]
        assert "실패" in failed.research_note.one_line_summary
        others = [p for p in papers if p.title != "Paper 5"]
        assert all(p.title in p.research_note.one_line_summary for p in others)

    def test_every_paper_is_asked_for_exactly_once(self):
        papers = [make_paper(arxiv_id=str(i), title=f"Paper {i}",
                             abstract=f"Abstract {i}") for i in range(30)]
        provider = MagicMock()
        provider.complete.side_effect = lambda prompt, **kw: self._note_for(
            prompt.split("제목: ")[1].split("\n")[0].strip()
        )

        generate_notes(papers, self._cfg(concurrency=8), provider)

        assert provider.complete.call_count == 30

    def test_an_empty_list_does_no_work(self):
        provider = MagicMock()
        generate_notes([], self._cfg(concurrency=8), provider)
        provider.complete.assert_not_called()
