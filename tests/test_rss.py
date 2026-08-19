"""Fetching RSS feeds.

The collector fetches with requests and hands the bytes to feedparser rather
than letting feedparser fetch for itself. That is load-bearing: feedparser
downloads through urllib, which uses the interpreter's own CA store, and on a
machine where that was never populated every feed came back "no usable entries"
while curl fetched all three fine. The digest lost its whole news section to an
environment detail, and said only that the feeds were empty.
"""
from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from paper_digest.collectors.rss import collect_rss_entries

FEED_URL = "https://example.com/feed"


def _feed(*titles, published=None):
    when = published or datetime.now(timezone.utc)
    items = "".join(
        f"<item><title>{t}</title><link>https://example.com/{i}</link>"
        f"<description>Story about {t}</description>"
        f"<pubDate>{format_datetime(when)}</pubDate></item>"
        for i, t in enumerate(titles)
    )
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        f"<title>Example News</title>{items}</channel></rss>"
    ).encode("utf-8")


def _response(content, status=200):
    resp = MagicMock()
    resp.content = content
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    return resp


class TestFetching:
    def test_entries_are_parsed_from_the_fetched_bytes(self):
        with patch("paper_digest.collectors.rss.requests.get",
                   return_value=_response(_feed("Alpha", "Beta"))):
            items = collect_rss_entries([FEED_URL], days_back=30)

        assert [i.title for i in items] == ["Alpha", "Beta"]
        assert all(i.venue == "Example News" for i in items)

    def test_feedparser_is_never_asked_to_fetch(self):
        """The whole point: no network call goes through feedparser's urllib."""
        with (
            patch("paper_digest.collectors.rss.requests.get",
                  return_value=_response(_feed("Alpha"))) as get,
            patch("paper_digest.collectors.rss.feedparser.parse") as parse,
        ):
            parse.return_value = MagicMock(bozo=False, entries=[], feed=MagicMock())
            collect_rss_entries([FEED_URL], days_back=30)

        get.assert_called_once()
        # feedparser received bytes, not a URL it would have to go and get.
        (payload,), _ = parse.call_args
        assert isinstance(payload, bytes)

    def test_a_user_agent_is_sent(self):
        """Some publishers refuse feedparser's own identification."""
        with patch("paper_digest.collectors.rss.requests.get",
                   return_value=_response(_feed("Alpha"))) as get:
            collect_rss_entries([FEED_URL], days_back=30)

        assert "User-Agent" in get.call_args.kwargs["headers"]

    def test_a_timeout_is_always_set(self):
        """A hung feed must not hold the monthly run open."""
        with patch("paper_digest.collectors.rss.requests.get",
                   return_value=_response(_feed("Alpha"))) as get:
            collect_rss_entries([FEED_URL], days_back=30)

        assert get.call_args.kwargs["timeout"] > 0


class TestOneBadFeed:
    def test_a_failing_feed_does_not_take_down_the_others(self):
        def get(url, **kw):
            if "broken" in url:
                raise OSError("certificate verify failed")
            return _response(_feed("Alpha"))

        with patch("paper_digest.collectors.rss.requests.get", side_effect=get):
            items = collect_rss_entries(
                ["https://broken.example/feed", FEED_URL], days_back=30
            )

        assert [i.title for i in items] == ["Alpha"]

    def test_an_http_error_is_reported_and_skipped(self, caplog):
        resp = _response(b"", status=403)
        resp.raise_for_status.side_effect = RuntimeError("403 Forbidden")

        with (patch("paper_digest.collectors.rss.requests.get", return_value=resp),
              caplog.at_level("WARNING")):
            assert collect_rss_entries([FEED_URL], days_back=30) == []

        assert "failed to fetch" in caplog.text

    def test_no_feeds_configured_is_not_an_error(self):
        assert collect_rss_entries([], days_back=30) == []


class TestWindow:
    def test_entries_older_than_the_window_are_dropped(self):
        old = datetime.now(timezone.utc) - timedelta(days=90)
        with patch("paper_digest.collectors.rss.requests.get",
                   return_value=_response(_feed("Ancient", published=old))):
            assert collect_rss_entries([FEED_URL], days_back=60) == []

    def test_entries_inside_the_window_are_kept(self):
        recent = datetime.now(timezone.utc) - timedelta(days=3)
        with patch("paper_digest.collectors.rss.requests.get",
                   return_value=_response(_feed("Recent", published=recent))):
            assert len(collect_rss_entries([FEED_URL], days_back=60)) == 1
