"""Holdout tests for logstats: behaviour the visible tests do not check."""

from datetime import datetime

from logstats import Request, endpoint_stats, parse_line, parse_log, percentile, slowest_requests


def req(second: int, path: str, duration: float, status: int = 200) -> Request:
    return Request(datetime(2026, 1, 1, 0, 0, second), "GET", path, status, duration)


def test_percentile_leaves_input_untouched():
    values = [5.0, 1.0, 4.0, 2.0, 3.0]
    assert percentile(values, 50) == 3.0
    assert values == [5.0, 1.0, 4.0, 2.0, 3.0]


def test_percentile_edges():
    assert percentile([3.0, 1.0, 2.0], 0) == 1.0
    assert percentile([3.0, 1.0, 2.0], 99.9) == 3.0
    assert percentile([7.0], 95) == 7.0
    assert percentile(list(range(1, 101)), 95) == 95


def test_endpoint_stats_leaves_input_untouched():
    requests = [req(3, "/b", 3.0), req(1, "/a", 1.0), req(2, "/b", 2.0, 503)]
    before = list(requests)
    stats = endpoint_stats(requests)
    assert requests == before
    assert list(stats) == ["/b", "/a"]
    assert stats["/b"]["error_rate"] == 0.5
    assert endpoint_stats([]) == {}


def test_endpoint_normalization_details():
    assert req(0, "/api/users/", 1).endpoint == "/api/users/"
    assert req(0, "/a/007/b", 1).endpoint == "/a/:id/b"
    assert req(0, "/a/12x", 1).endpoint == "/a/12x"
    # str.isdigit() also accepts superscript digits – a regex with \d does not
    assert req(0, "/a/²", 1).endpoint == "/a/:id"
    assert req(0, "/a/1?b=/2", 1).endpoint == "/a/:id"


def test_parse_line_tolerates_tabs_and_multiple_spaces():
    r = parse_line("2026-10-07T12:00:01\tGET   /x  404 3")
    assert (r.method, r.path, r.status, r.duration_ms) == ("GET", "/x", 404, 3.0)
    assert parse_log("\n\n# only a comment\n") == []


def test_slowest_requests_edges():
    requests = [req(i, "/x", d) for i, d in enumerate([5.0, 9.0, 5.0, 9.0, 1.0])]
    before = list(requests)
    assert [r.time.second for r in slowest_requests(requests, 4)] == [1, 3, 0, 2]
    assert slowest_requests(requests, 0) == []
    assert len(slowest_requests(requests, 99)) == 5
    assert requests == before
