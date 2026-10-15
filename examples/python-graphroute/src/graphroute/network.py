"""Undirected graph with breadth-first search (original variant with typical inefficiencies)."""


class Network:
    """Undirected graph; nodes are arbitrary hashable values."""

    def __init__(self) -> None:
        self.edges: list[tuple] = []

    def add_edge(self, a, b) -> None:
        """Add an undirected edge (duplicate edges are ignored)."""
        if (a, b) not in self.edges and (b, a) not in self.edges:
            self.edges.append((a, b))

    def neighbors(self, node) -> list:
        """Neighbours of `node` in the order in which the edges were added."""
        result = []
        for a, b in self.edges:  # searches all edges on every call: O(E)
            if a == node:
                result.append(b)
            elif b == node:
                result.append(a)
        return result

    def shortest_path(self, start, goal) -> list | None:
        """A shortest path from `start` to `goal` (including both), otherwise None.

        If there are several shortest paths, the one whose edges were added earlier wins
        (breadth-first search in neighbour order).
        """
        if start == goal:
            return [start]
        queue = [start]
        visited = [start]
        parent = {}
        while queue:
            node = queue.pop(0)  # O(n) for lists
            for nxt in self.neighbors(node):
                if nxt not in visited:  # membership check on a list
                    visited.append(nxt)
                    parent[nxt] = node
                    if nxt == goal:
                        path = [goal]
                        while path[-1] != start:
                            path.append(parent[path[-1]])
                        return path[::-1]
                    queue.append(nxt)
        return None

    def degree_histogram(self) -> dict[int, int]:
        """Number of nodes per degree."""
        nodes = []
        for a, b in self.edges:
            for n in (a, b):
                if n not in nodes:
                    nodes.append(n)
        hist: dict[int, int] = {}
        for n in nodes:
            d = len(self.neighbors(n))
            hist[d] = hist.get(d, 0) + 1
        return dict(sorted(hist.items()))
