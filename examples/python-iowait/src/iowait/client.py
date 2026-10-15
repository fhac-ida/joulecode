"""Simulated client for a slow price service.

The run time consists almost only of waiting for the (here simulated) answer. The
CPU hardly does anything – code optimisation can change little about that. joule:code should
not find any noteworthy hotspots.
"""

import time

LATENCY_S = 0.05


def fetch_price(item: str) -> float:
    """Price of an item in euros – simulated: waiting time plus a deterministic value."""
    time.sleep(LATENCY_S)
    return round(sum(ord(c) for c in item) / 10, 2)


def total_price(items: list[str]) -> float:
    """Sum of the prices of all items (one request per item, one after another)."""
    total = 0.0
    for item in items:
        total += fetch_price(item)
    return round(total, 2)
