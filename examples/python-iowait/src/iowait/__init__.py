"""Simulated service client (special case: the run time consists almost only of waiting)."""

from iowait.client import fetch_price, total_price

__all__ = ["fetch_price", "total_price"]
