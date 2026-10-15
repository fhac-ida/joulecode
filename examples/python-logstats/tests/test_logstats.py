from datetime import datetime

from logstats import endpoint_stats, parse_line, parse_log, percentile, slowest_requests, summary_table

LOG = """# example
2026-10-07T12:00:01 GET /api/users 200 12.5
2026-10-07T12:00:02 GET /api/users/17 200 30.0
2026-10-07T12:00:03 POST /api/users 500 80.0

2026-10-07T12:00:04 GET /api/users/18?full=1 200 30.0
2026-10-07T12:00:05 GET /health 200 1.0
"""


def test_parse_line():
    r = parse_line("2026-10-07T12:00:01 GET /api/users 200 12.5")
    assert r.time == datetime(2026, 10, 7, 12, 0, 1)
    assert (r.method, r.path, r.status, r.duration_ms) == ("GET", "/api/users", 200, 12.5)
    assert parse_line("   ") is None
    assert parse_line("# x") is None


def test_endpoint_normalization():
    assert [r.endpoint for r in parse_log(LOG)] == [
        "/api/users",
        "/api/users/:id",
        "/api/users",
        "/api/users/:id",
        "/health",
    ]


def test_percentile_nearest_rank():
    values = [15, 20, 35, 40, 50]
    assert percentile(values, 30) == 20
    assert percentile(values, 40) == 20
    assert percentile(values, 50) == 35
    assert percentile(values, 100) == 50
    assert percentile([], 50) == 0.0


def test_endpoint_stats():
    stats = endpoint_stats(parse_log(LOG))
    assert list(stats) == ["/api/users", "/api/users/:id", "/health"]
    assert stats["/api/users"] == {
        "count": 2,
        "error_rate": 0.5,
        "median_ms": 12.5,
        "p95_ms": 80.0,
        "max_ms": 80.0,
    }


def test_slowest_requests_tie_break():
    slow = slowest_requests(parse_log(LOG), 3)
    assert [r.time.second for r in slow] == [3, 2, 4]


def test_summary_table_lines():
    lines = summary_table(LOG).splitlines()
    assert lines[1].split() == ["/api/users", "2", "50.0%", "12.5", "80.0"]
    assert lines[-1].strip() == "2026-10-07T12:00:04 GET /api/users/18?full=1 30.0 ms"
