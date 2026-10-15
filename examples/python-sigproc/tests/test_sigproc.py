import numpy as np
import pytest

from sigproc import moving_average, pairwise_distances, peaks, zscore


def test_moving_average():
    np.testing.assert_allclose(moving_average(np.array([1.0, 2.0, 3.0, 4.0]), 2), [1.5, 2.5, 3.5])
    np.testing.assert_allclose(moving_average(np.array([5.0, 7.0]), 2), [6.0])
    with pytest.raises(ValueError):
        moving_average(np.array([1.0]), 2)


def test_zscore():
    z = zscore(np.array([1.0, 2.0, 3.0]))
    np.testing.assert_allclose(z, [-1.224744871391589, 0.0, 1.224744871391589])
    np.testing.assert_array_equal(zscore(np.array([4.0, 4.0])), [0.0, 0.0])


def test_pairwise_distances():
    d = pairwise_distances(np.array([[0.0, 0.0], [3.0, 4.0], [0.0, 1.0]]))
    np.testing.assert_allclose(d, [[0, 5, 1], [5, 0, np.sqrt(18)], [1, np.sqrt(18), 0]])


def test_peaks():
    assert peaks(np.array([0.0, 2.0, 1.0, 3.0, 3.0, 1.0, 5.0, 0.0]), 1.5) == [1, 6]
