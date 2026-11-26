"""
同時に読む (2026-11-27): reads that do not need each other's answers sent
at once, not one after another. A dock's reload read its tables in turn
(撮像予定 by place: the plans, the shots, the ledger, the groups; 観測記録:
the classes, the records around the image, the checked areas), so each
waited for the round trips of the ones before it: at 150 ms a round trip,
1.1–1.5 s where the longest alone takes 0.5.

Each read runs in a thread of its own, with what the thread that started
them carries (`carry`: qgis_io.tasks hands its task, so the reads' requests
stop when it is cancelled). At most LIMIT at once (the server's instances
and the plugin's network lines are shared). The answers come in the order
of the reads; when one raised, the first of those in order is raised again
once all have ended (as reading in turn would have, but the others were read).
"""
from __future__ import annotations

import threading
from typing import Any, Callable

#: At most this many reads at once.
LIMIT = 4

#: What a read's thread takes from the thread that started it: (take, put) pairs (put(None) when the read ends).
_CARRIED: list[tuple[Callable[[], Any], Callable[[Any], None]]] = []


def carry(take: Callable[[], Any], put: Callable[[Any], None]) -> None:
    """Has each read's thread `put` what `take` gave in the thread that started the reads (a thread-local)."""
    _CARRIED.append((take, put))


def read_together(*reads: Callable[[], Any], limit: int = LIMIT) -> list:
    """The answers of `reads` (each called with nothing), read at once (`limit` at a time)."""
    if len(reads) <= 1 or limit <= 1:
        return [read() for read in reads]
    carried = [(put, take()) for take, put in _CARRIED]
    answers: list = [None] * len(reads)
    errors: list = [None] * len(reads)
    gate = threading.BoundedSemaphore(limit)

    def run(i: int) -> None:
        for put, value in carried:
            put(value)
        try:
            with gate:
                answers[i] = reads[i]()
        except BaseException as error:  # noqa: BLE001 (raised again in the starting thread)
            errors[i] = error
        finally:
            for put, _value in carried:
                put(None)

    threads = [threading.Thread(target=run, args=(i,), name="MIKAGE read", daemon=True) for i in range(len(reads))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for error in errors:
        if error is not None:
            raise error
    return answers

