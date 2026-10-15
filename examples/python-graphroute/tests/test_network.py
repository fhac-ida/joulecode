from graphroute import Network


def square_with_diagonal() -> Network:
    net = Network()
    for a, b in [("a", "b"), ("b", "d"), ("a", "c"), ("c", "d"), ("d", "e")]:
        net.add_edge(a, b)
    return net


def test_neighbors_in_insertion_order():
    net = square_with_diagonal()
    assert net.neighbors("d") == ["b", "c", "e"]
    assert net.neighbors("x") == []


def test_shortest_path_prefers_earlier_edges_on_ties():
    # a-b-d and a-c-d have the same length; a-b was added first.
    assert square_with_diagonal().shortest_path("a", "e") == ["a", "b", "d", "e"]


def test_shortest_path_trivial_and_unreachable():
    net = square_with_diagonal()
    assert net.shortest_path("a", "a") == ["a"]
    net.add_edge("x", "y")
    assert net.shortest_path("a", "y") is None


def test_duplicate_edges_ignored():
    net = Network()
    net.add_edge(1, 2)
    net.add_edge(2, 1)
    assert net.neighbors(1) == [2]


def test_degree_histogram():
    assert square_with_diagonal().degree_histogram() == {1: 1, 2: 3, 3: 1}
