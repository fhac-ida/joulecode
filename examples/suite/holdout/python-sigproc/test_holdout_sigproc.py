"""Holdout tests for sigproc: behaviour the visible tests do not check."""

import numpy as np
import pytest

from sigproc import moving_average, pairwise_distances, peaks, zscore


def test_moving_average_edges_and_dtype():
    x = np.array([1, 2, 3, 4, 6])  # integers
    out = moving_average(x, 5)
    assert out.dtype == np.float64
    np.testing.assert_allclose(out, [3.2])
    np.testing.assert_allclose(moving_average(x, 1), x)
    for bad in (0, -1, 6):
        with pytest.raises(ValueError):
            moving_average(x, bad)


def test_moving_average_matches_reference_on_a_long_signal():
    rng = np.random.default_rng(1)
    x = np.cumsum(rng.normal(size=5000)) + 1e4  # large offset: checks the precision
    expected = np.array([x[i : i + 30].sum() / 30 for i in range(len(x) - 29)])
    np.testing.assert_allclose(moving_average(x, 30), expected, rtol=1e-9)


def test_inputs_are_not_modified():
    x = np.array([3.0, 1.0, 2.0, 5.0])
    pts = np.array([[0.0, 1.0], [2.0, 3.0]])
    before_x, before_p = x.copy(), pts.copy()
    moving_average(x, 2)
    zscore(x)
    peaks(x, 0.0)
    pairwise_distances(pts)
    np.testing.assert_array_equal(x, before_x)
    np.testing.assert_array_equal(pts, before_p)


def test_zscore_integers_and_constant():
    z = zscore(np.array([1, 2, 3, 4]))
    assert z.dtype == np.float64
    np.testing.assert_allclose(z.mean(), 0.0, atol=1e-12)
    np.testing.assert_allclose(z.std(), 1.0)
    np.testing.assert_array_equal(zscore(np.array([7.0])), [0.0])


def test_pairwise_distances_properties():
    rng = np.random.default_rng(2)
    pts = rng.normal(size=(12, 4))
    d = pairwise_distances(pts)
    assert d.shape == (12, 12)
    np.testing.assert_array_equal(np.diag(d), np.zeros(12))
    np.testing.assert_allclose(d, d.T)
    np.testing.assert_allclose(d[3, 7], np.linalg.norm(pts[3] - pts[7]))
    np.testing.assert_allclose(pairwise_distances(np.array([[1.0], [4.0]])), [[0, 3], [3, 0]])


def test_peaks_edges():
    assert peaks(np.array([1.0, 2.0]), 0.0) == []
    assert peaks(np.array([0.0, 2.0, 2.0, 0.0]), 0.0) == []  # a plateau is not a maximum
    assert peaks(np.array([0.0, 2.0, 0.0]), 2.0) == []  # strictly above the threshold
    result = peaks(np.array([0.0, 3.0, 0.0, 4.0, 0.0]), 1.0)
    assert result == [1, 3]
    assert isinstance(result, list)
