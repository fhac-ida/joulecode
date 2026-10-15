"""Key figures per endpoint."""

from logstats.parser import Request


def percentile(values: list[float], p: float) -> float:
    """p-th percentile by the nearest-rank method (p in 0..100); an empty list yields 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = int(p / 100 * len(ordered) + 0.999999999)  # aufrunden
    rank = min(max(rank, 1), len(ordered))
    return ordered[rank - 1]


def endpoint_stats(requests: list[Request]) -> dict[str, dict]:
    """Per endpoint: count, error rate (status >= 500), median, p95 and maximum of the response time.

    The endpoints appear in the order of their first occurrence.
    """
    endpoints = []
    for r in requests:
        if r.endpoint not in endpoints:
            endpoints.append(r.endpoint)
    stats = {}
    for ep in endpoints:
        durations = [r.duration_ms for r in requests if r.endpoint == ep]  # all requests per endpoint
        errors = len([r for r in requests if r.endpoint == ep and r.status >= 500])
        stats[ep] = {
            "count": len(durations),
            "error_rate": errors / len(durations),
            "median_ms": percentile(durations, 50),
            "p95_ms": percentile(durations, 95),
            "max_ms": max(durations),
        }
    return stats


def slowest_requests(requests: list[Request], n: int = 5) -> list[Request]:
    """The n slowest requests, in descending order; on a tie the earlier one first."""
    remaining = list(requests)
    result = []
    while remaining and len(result) < n:
        slowest = remaining[0]
        for r in remaining:
            if r.duration_ms > slowest.duration_ms:
                slowest = r
        result.append(slowest)
        remaining.remove(slowest)
    return result
