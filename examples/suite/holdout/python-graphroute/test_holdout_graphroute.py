"""Holdout tests for graphroute: behaviour the visible tests do not check.

joule:code does not see these tests. If a candidate passes the visible tests but
not these, the test gate let a change in behaviour through.
"""

from graphroute import Network


def test_edges_added_after_a_query_are_seen():
    # catches stale caches (e.g. lru_cache on neighbors/shortest_path)
    net = Network()
    net.add_edge(1, 2)
    net.add_edge(2, 3)
    assert net.shortest_path(1, 3) == [1, 2, 3]
    assert net.neighbors(1) == [2]
    net.add_edge(1, 3)
    assert net.neighbors(1) == [2, 3]
    assert net.shortest_path(1, 3) == [1, 3]


def test_neighbors_returns_a_fresh_list():
    net = Network()
    net.add_edge("a", "b")
    net.neighbors("a").append("x")
    assert net.neighbors("a") == ["b"]
    assert net.shortest_path("a", "x") is None


def test_edges_attribute_keeps_insertion_order():
    net = Network()
    for a, b in [(3, 1), (1, 2), (2, 3), (1, 3)]:
        net.add_edge(a, b)
    assert net.edges == [(3, 1), (1, 2), (2, 3)]


def test_mixed_node_types_need_no_ordering():
    net = Network()
    net.add_edge(1, "one")
    net.add_edge("one", (1, 1))
    net.add_edge(1, None)
    assert net.shortest_path(1, (1, 1)) == [1, "one", (1, 1)]
    assert net.shortest_path(None, (1, 1)) == [None, 1, "one", (1, 1)]


def test_unknown_nodes():
    net = Network()
    net.add_edge(1, 2)
    assert net.shortest_path(1, 99) is None
    assert net.shortest_path(99, 1) is None
    assert net.neighbors(99) == []


def test_self_loop_counts_once():
    net = Network()
    net.add_edge(1, 1)
    net.add_edge(1, 2)
    assert net.neighbors(1) == [1, 2]
    assert net.degree_histogram() == {1: 1, 2: 1}


def test_tie_break_deeper_in_the_graph():
    net = Network()
    for a, b in [(0, 1), (0, 2), (2, 3), (1, 3), (3, 4), (2, 4)]:
        net.add_edge(a, b)
    assert net.shortest_path(0, 4) == [0, 2, 4]
    assert net.shortest_path(1, 2) == [1, 0, 2]
