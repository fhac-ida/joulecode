"""Signal processing with numpy – but in Python loops (original variant)."""

import math

import numpy as np


def moving_average(signal: np.ndarray, window: int) -> np.ndarray:
    """Moving average over `window` values; the result has len(signal) - window + 1 values."""
    if window < 1 or window > len(signal):
        raise ValueError("window must be between 1 and len(signal)")
    out = np.empty(len(signal) - window + 1)
    for i in range(len(out)):
        total = 0.0
        for j in range(window):
            total += signal[i + j]
        out[i] = total / window
    return out


def zscore(signal: np.ndarray) -> np.ndarray:
    """Standardisation to mean 0 and standard deviation 1 (population std)."""
    n = len(signal)
    mean = 0.0
    for x in signal:
        mean += x
    mean /= n
    var = 0.0
    for x in signal:
        var += (x - mean) ** 2
    std = math.sqrt(var / n)
    if std == 0:
        return np.zeros(n)
    return np.array([(x - mean) / std for x in signal])


def pairwise_distances(points: np.ndarray) -> np.ndarray:
    """Euclidean distance matrix for points of shape (n, d)."""
    n = len(points)
    dist = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            s = 0.0
            for k in range(points.shape[1]):
                s += (points[i, k] - points[j, k]) ** 2
            dist[i, j] = math.sqrt(s)
    return dist


def peaks(signal: np.ndarray, threshold: float) -> list[int]:
    """Indices of local maxima (strictly greater than both neighbours) above `threshold`."""
    result = []
    for i in range(1, len(signal) - 1):
        if signal[i] > threshold and signal[i] > signal[i - 1] and signal[i] > signal[i + 1]:
            result.append(i)
    return result
