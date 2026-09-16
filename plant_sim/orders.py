"""
Work-in-progress batches and the queues that sit in front of each station.

A Batch is a slice of m^2 of a single product class that all entered the
factory at the same hour. Batches are consumed oldest-ORDER-first as
downstream stations process them, and shrink in place rather than being
copied, so partial consumption ("only some of this batch was processed this
hour") just reduces .qty.

"Oldest order first" rather than plain first-in-first-out matters for one
thing: remakes. A remade part keeps its ORIGINAL order date, so when it
re-enters the line it goes to the front of every queue - which is exactly
how a real supervisor treats a part that's already late. For fresh intake
the two orderings are identical (new orders are always the newest).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

# Anything smaller than this many m2 is treated as "nothing" - it stops
# floating-point crumbs (0.0000000001 m2) keeping a batch alive forever.
EPS_M2 = 1e-9


@dataclass
class Batch:
    created_hour: float   # simulation hour the *original order* was placed
    qty: float             # m^2 remaining in this batch
    product_class: str     # e.g. "S1", "Melamine"
    is_remake: bool = False   # True once this work has been through the remake loop
    is_special: bool = False  # True for special (non-standard) orders - routed to C6 at the Thermo CNCs

    def slice(self, qty: float) -> "Batch":
        """A piece of this batch with the same identity flags."""
        return Batch(self.created_hour, qty, self.product_class, self.is_remake, self.is_special)


class StationQueue:
    """A buffer of batches waiting in front of one station, kept sorted by
    original order date (oldest first)."""

    def __init__(self):
        self.batches: list[Batch] = []
        # Parallel list of created_hour values, kept in step with .batches,
        # so inserting a remake into the right place is a binary search.
        self._keys: list[float] = []

    def __len__(self) -> int:
        return len(self.batches)

    def add(self, batch: Batch) -> None:
        if batch.qty <= EPS_M2:
            return
        if not self._keys or batch.created_hour >= self._keys[-1]:
            # Common case (fresh intake, or anything not older than what's
            # already here): straight onto the back.
            self.batches.append(batch)
            self._keys.append(batch.created_hour)
            return
        # Older than something already waiting (a remake): slot it in AFTER
        # any batch with the same order date, so same-age work stays FIFO.
        i = bisect_right(self._keys, batch.created_hour)
        self.batches.insert(i, batch)
        self._keys.insert(i, batch.created_hour)

    def total_m2(self) -> float:
        return sum(b.qty for b in self.batches)

    def headroom(self, cap: float) -> float:
        return max(0.0, cap - self.total_m2())

    def _pop_front(self) -> None:
        self.batches.pop(0)
        self._keys.pop(0)

    def consume_flat_rate(self, capacity_m2: float) -> list[Batch]:
        """
        Pull up to `capacity_m2` worth of m^2 off the front of the queue,
        oldest order first, regardless of product class. Used by stations
        whose processing rate doesn't depend on the class (Sanding, Edging,
        Press, Packing, Edge Band/Drilling, Optimising).
        """
        remaining = capacity_m2
        out: list[Batch] = []
        while self.batches and remaining > EPS_M2:
            head = self.batches[0]
            if head.qty <= EPS_M2:
                self._pop_front()
                continue
            take = min(remaining, head.qty)
            out.append(head.slice(take))
            head.qty -= take
            remaining -= take
            if head.qty <= EPS_M2:
                self._pop_front()
        return out

    def _pop_at(self, i: int) -> None:
        self.batches.pop(i)
        self._keys.pop(i)

    def consume_cnc(self, available_minutes: float, room_m2: float,
                     min_per_m2_by_class: dict[str, float],
                     wants=None) -> list[Batch]:
        """
        CNC-style consumption: capacity is machine-*minutes*, and the minutes
        cost per m^2 depends on the product class (Series 3 boards take far
        longer to cut than Series 1). Also respects downstream headroom.

        `wants(batch) -> bool` restricts which batches this machine lane will
        take (e.g. C6 wants remakes and specials); batches it doesn't want are
        left in place, in order, for another lane. None = take anything.
        """
        remaining_min = available_minutes
        remaining_room = room_m2
        out: list[Batch] = []
        i = 0
        while i < len(self.batches) and remaining_min > EPS_M2 and remaining_room > EPS_M2:
            head = self.batches[i]
            if head.qty <= EPS_M2:
                self._pop_at(i)
                continue
            if wants is not None and not wants(head):
                i += 1
                continue
            min_per_m2 = min_per_m2_by_class[head.product_class]
            # A zero cut time means "costs no machine time" (only downstream
            # room limits it). A CNC that is DOWN is expressed by the caller
            # passing zero available_minutes, not by a zero cut time.
            max_by_time = remaining_min / min_per_m2 if min_per_m2 > 0 else head.qty
            take = min(head.qty, max_by_time, remaining_room)
            if take <= EPS_M2:
                break
            out.append(head.slice(take))
            head.qty -= take
            remaining_min -= take * min_per_m2
            remaining_room -= take
            if head.qty <= EPS_M2:
                self._pop_at(i)
            else:
                i += 1
        return out
