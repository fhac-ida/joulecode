"""Read the lines of an access log.

Format: `2026-10-07T12:00:01 GET /api/users 200 12.5`
(timestamp, method, path, status code, response time in ms)
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Request:
    time: datetime
    method: str
    path: str
    status: int
    duration_ms: float

    @property
    def endpoint(self) -> str:
        """Path without query string and without numeric IDs (`/api/users/17?x=1` → `/api/users/:id`)."""
        path = self.path.split("?")[0]
        parts = []
        for part in path.split("/"):
            if part.isdigit():
                parts.append(":id")
            else:
                parts.append(part)
        return "/".join(parts)


def parse_line(line: str) -> Request | None:
    """Read one line; empty lines and comments (#) yield None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    stamp, method, path, status, duration = line.split()
    return Request(
        time=datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S"),
        method=method,
        path=path,
        status=int(status),
        duration_ms=float(duration),
    )


def parse_log(text: str) -> list[Request]:
    """All requests of a log in file order."""
    requests = []
    for line in text.splitlines():
        req = parse_line(line)
        if req is not None:
            requests = requests + [req]  # rebuilds the list every time
    return requests
