"""Partition selected tests to minimize peak worker load and startup cost."""

from __future__ import annotations

from bisect import bisect_left
from heapq import heapreplace


MAX_WORKERS = 8
STARTUP_MS = 2000
EXACT_NODE_LIMIT = 1600

Test = tuple[int, int]
Workers = list[list[Test]]


def _lpt(tests: list[Test], count: int) -> tuple[int, Workers]:
    workers: Workers = [[] for _ in range(count)]
    heap = [(0, worker) for worker in range(count)]
    peak = 0
    for test in tests:
        load, worker = heap[0]
        load += test[1]
        workers[worker].append(test)
        heapreplace(heap, (load, worker))
        peak = max(peak, load)
    return peak, workers


def _improve_pair(workers: Workers) -> int:
    """Trade jobs between the busiest and lightest worker up to twice."""
    loads = [sum(duration for _, duration in group) for group in workers]
    for _ in range(2):
        heavy = loads.index(max(loads))
        light = loads.index(min(loads))
        gap = loads[heavy] - loads[light]
        if gap < 2:
            break

        a, b = workers[heavy], workers[light]
        choices = sorted((test[1], i) for i, test in enumerate(b))
        if len(a) > 1:
            choices.insert(0, (0, -1))
        best_gap = gap
        swap = None
        step = max(1, (len(a) + 127) // 128)
        for i in range(0, len(a), step):
            target = a[i][1] - gap // 2
            near = bisect_left(choices, (target, -1))
            for offset in (near - 1, near):
                if 0 <= offset < len(choices):
                    duration, j = choices[offset]
                    change = a[i][1] - duration
                    new_gap = abs(gap - 2 * change)
                    if new_gap < best_gap:
                        best_gap = new_gap
                        swap = i, j, change
            if best_gap == gap % 2:
                break

        if swap is None:
            break
        i, j, change = swap
        if j < 0:
            b.append(a.pop(i))
        else:
            a[i], b[j] = b[j], a[i]
        loads[heavy] -= change
        loads[light] += change
    return max(loads)


def _pack(
    tests: list[Test], count: int, capacity: int, nodes: list[int]
) -> Workers | None:
    remaining = [capacity] * count
    workers: Workers = [[] for _ in range(count)]

    def place(index: int) -> bool:
        if index == len(tests):
            return True
        if nodes[0] == 0:
            return False
        nodes[0] -= 1

        test = tests[index]
        duration = test[1]
        seen = set()
        for worker in sorted(range(count), key=remaining.__getitem__):
            room = remaining[worker]
            if room < duration or room in seen:
                continue
            seen.add(room)
            remaining[worker] -= duration
            workers[worker].append(test)
            if place(index + 1):
                return True
            workers[worker].pop()
            remaining[worker] += duration
            if room == capacity or nodes[0] == 0:
                break
        return False

    return workers if place(0) else None


def schedule(tests: list[Test]) -> list[list[int]]:
    if not tests:
        return []
    ordered = sorted(tests, key=lambda test: (-test[1], test[0]))
    total = sum(duration for _, duration in ordered)
    largest = ordered[0][1]
    best_cost = total + STARTUP_MS
    best_workers = [ordered]
    candidates = []

    for count in range(2, min(MAX_WORKERS, len(tests)) + 1):
        overhead = count * STARTUP_MS
        if largest + overhead >= best_cost:
            break
        lower = max(largest, (total + count - 1) // count)
        if lower + overhead >= best_cost:
            continue
        peak, workers = _lpt(ordered, count)
        if len(ordered) > 24 and peak - lower >= 8:
            peak = _improve_pair(workers)
        if peak + overhead < best_cost:
            best_cost = peak + overhead
            best_workers = workers
        if peak > lower:
            candidates.append((lower + overhead, count, peak))

    # An exact search on small selections can recover the remaining gap.
    # Cap it across all worker counts because the scorer charges CPU time.
    if len(ordered) <= 24:
        nodes = [EXACT_NODE_LIMIT]
        for lower_cost, count, peak in sorted(candidates):
            if lower_cost >= best_cost or not nodes[0]:
                break
            overhead = count * STARTUP_MS
            low = lower_cost - overhead
            high = min(peak, best_cost - overhead) - 1
            target = low
            while low <= high and nodes[0]:
                result = _pack(ordered, count, target, nodes)
                if result is None:
                    low = target + 1
                else:
                    best_cost = target + overhead
                    best_workers = result
                    high = target - 1
                target = (low + high) // 2

    return [[number for number, _ in group] for group in best_workers if group]


def fill_idle_time(workers, durations, extras):
    """Add unscheduled catalogue tests that fit under the current peak load."""
    if not workers:
        return
    selected = {n for group in workers for n in group}
    loads = [sum(durations[n] for n in group) for group in workers]
    peak = max(loads)
    remaining = [peak - load for load in loads]
    for number, duration in extras:
        if number in selected:
            continue
        worker = max(range(len(remaining)), key=remaining.__getitem__)
        if duration > remaining[worker]:
            # Extras are sorted by duration. No subsequent test can fit either.
            break
        workers[worker].append(number)
        remaining[worker] -= duration
