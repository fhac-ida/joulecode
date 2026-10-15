"""Benchmark: fixed, deterministic amount of work (one fleet, one operating day).

BENCH_ROUNDS scales the run time (on the measurement machine, choose it so that a run takes
>= 20 s). BENCH_VEHICLES / BENCH_POINTS change the amount of data per round.
"""

import math
import os
import random
import time
from datetime import datetime, timedelta

from fleetlog import Geofence, analyze, render

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "3"))
VEHICLES = int(os.environ.get("BENCH_VEHICLES", "600"))
POINTS_PER_VEHICLE = int(os.environ.get("BENCH_POINTS", "16"))

DEPOTS = {"Aachen": (50.776, 6.084), "Köln": (50.937, 6.960), "Düren": (50.804, 6.492), "Jülich": (50.922, 6.361)}


def square(name: str, kind: str, lat: float, lon: float, size: float) -> Geofence:
    return Geofence(name, kind, ((lat - size, lon - size), (lat - size, lon + size), (lat + size, lon + size), (lat + size, lon - size)))


def make_fences(rng: random.Random) -> list[Geofence]:
    fences = [square(name, "depot", lat, lon, 0.01) for name, (lat, lon) in DEPOTS.items()]
    for i in range(8):
        fences.append(square(f"Customer{i}", "customer", 50.75 + rng.random() * 0.25, 6.05 + rng.random() * 0.9, 0.02))
    fences.append(square("Factory site", "restricted", 50.86, 6.70, 0.015))
    return fences


def make_lines(rng: random.Random) -> list[str]:
    lines = ["# Fleet export operating day 2026-05-04"]
    kinds = ["car", "van", "truck"]
    day = datetime(2026, 5, 4, 5, 0)
    depots = list(DEPOTS)
    for v in range(VEHICLES):
        kind = kinds[v % 3]
        tank = {"car": 55, "van": 80, "truck": 400}[kind]
        active = "0" if v % 37 == 36 else "1"
        lines.append(f"VEH;V{v:04d};AC-GC {v:04d};{depots[v % 4]};{kind};{tank};{active}")
    gps, fuel = [], []
    for v in range(VEHICLES):
        vid = f"V{v:04d}"
        lat, lon = DEPOTS[depots[v % 4]]
        ts = day + timedelta(minutes=rng.randrange(0, 600))
        level = 60 + rng.random() * 40
        heading = rng.random() * 2 * math.pi
        fuel.append(f"FUEL;{vid};{(ts - timedelta(minutes=5)).isoformat()};level;{level:.1f}")
        for i in range(POINTS_PER_VEHICLE):
            moving = i % 12 not in (0, 11)
            speed = 0.0 if not moving else 30 + rng.random() * (110 if v % 3 != 2 else 60)
            if moving:
                step = speed / 3600 * 60 / 111
                heading += rng.uniform(-0.4, 0.4)
                lat += step * math.cos(heading)
                lon += step * math.sin(heading) * 1.6
            ignition = "0" if (i % 12 == 11 and i + 1 < POINTS_PER_VEHICLE) else "1"
            gps.append(f"GPS;{vid};{ts.isoformat()};{lat:.6f};{lon:.6f};{speed:.1f};{ignition}")
            if rng.random() < 0.05:  # the box sends again after a dead zone
                gps.append(gps[-1])
            if i % 4 == 3:
                level = max(3.0, level - rng.random() * 3)
                fuel.append(f"FUEL;{vid};{(ts + timedelta(seconds=30)).isoformat()};level;{level:.1f}")
            ts += timedelta(minutes=1 if moving else 12)
        if rng.random() < 0.2:
            fuel.append(f"FUEL;{vid};{ts.isoformat()};refuel;{tank * rng.uniform(0.2, 0.9):.1f}")
        fuel.append(f"FUEL;{vid};{(ts + timedelta(minutes=2)).isoformat()};level;{level:.1f}")
    # Messages arrive via different gateways – not sorted by time.
    rng.shuffle(gps)
    return lines + gps + fuel


rng = random.Random(1234)
LINES = make_lines(rng)
FENCES = make_fences(rng)

start = time.perf_counter()
checksum = 0
for _ in range(ROUNDS):
    result = analyze(LINES, FENCES, merge_breaks=timedelta(minutes=15))
    out = render(result, FENCES)
    checksum += len(result.trips) + len(result.alerts) + result.duplicate_points
    checksum += len(out["report"]) + len(out["csv"]) + out["visits"]
    checksum += int(sum(out["consumption"].values()) * 100)
print(f"rounds={ROUNDS} checksum={checksum} seconds={time.perf_counter() - start:.2f}")
