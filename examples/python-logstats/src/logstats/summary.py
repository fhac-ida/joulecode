"""Text report on all endpoints."""

from logstats.aggregate import endpoint_stats, slowest_requests
from logstats.parser import parse_log


def summary_table(text: str) -> str:
    """Table per endpoint plus the three slowest requests."""
    requests = parse_log(text)
    out = f"{'Endpoint':<24}{'n':>6}{'Errors':>8}{'Median':>9}{'p95':>9}\n"
    for ep, s in endpoint_stats(requests).items():
        out += f"{ep:<24}{s['count']:>6}{s['error_rate']:>8.1%}{s['median_ms']:>9.1f}{s['p95_ms']:>9.1f}\n"
    out += "Slowest:\n"
    for r in slowest_requests(requests, 3):
        out += f"  {r.time.isoformat()} {r.method} {r.path} {r.duration_ms:.1f} ms\n"
    return out
