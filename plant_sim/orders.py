"""
Work-in-progress batches and the FIFO queues that sit in front of each station.

A Batch is a slice of m^2 of a single product class that all entered the
factory at the same hour. Batches are consumed oldest-first (FIFO) as
downstream stations process them, and shrink in place rather than being
copied, so partial consumption ("only some of this batch was processed this
hour") just reduces .qty.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Batch:
    created_hour: float   # simulation hour the *original order* was placed
    qty: float             # m^2 remaining in this batch
    product_class: str     # e.g. "S1", "Melamine"


class StationQueue:
    """A FIFO buffer of batches waiting in front of one station."""

    def __init__(self):
        self.batches: list[Batch] = []

    def add(self, batch: Batch) -> None:
        if batch.qty > 1e-9:
            self.batches.append(batch)

    def total_m2(self) -> float:
        return sum(b.qty for b in self.batches)

    def headroom(self, cap: float) -> float:
        return max(0.0, cap - self.total_m2())

    def consume_flat_rate(self, capacity_m2: float) -> list[Batch]:
        """
        Pull up to `capacity_m2` worth of m^2 off the front of the queue,
        oldest batch first, regardless of product class. Used by stations
        whose processing rate doesn't depend on the class (Sanding, Edging,
        Press, Packing, Edge Band/Drilling, Optimising).
        """
        remaining = capacity_m2
        out: list[Batch] = []
        while self.batches and remaining > 1e-9:
            head = self.batches[0]
            if head.qty <= 1e-9:
                self.batches.pop(0)
                continue
            take = min(remaining, head.qty)
            out.append(Batch(head.created_hour, take, head.product_class))
            head.qty -= take
            remaining -= take
            if head.qty <= 1e-9:
                self.batches.pop(0)
        return out

    def consume_cnc(self, available_minutes: float, room_m2: float,
                     min_per_m2_by_class: dict[str, float]) -> list[Batch]:
        """
        CNC-style consumption: capacity is machine-*minutes*, and the minutes
        cost per m^2 depends on the product class (Series 3 boards take far
        longer to cut than Series 1). Also respects downstream headroom.
        """
        remaining_min = available_minutes
        remaining_room = room_m2
        out: list[Batch] = []
        while self.batches and remaining_min > 1e-9 and remaining_room > 1e-9:
            head = self.batches[0]
            if head.qty <= 1e-9:
                self.batches.pop(0)
                continue
            min_per_m2 = min_per_m2_by_class[head.product_class]
            max_by_time = remaining_min / min_per_m2 if min_per_m2 > 0 else head.qty
            take = min(head.qty, max_by_time, remaining_room)
            if take <= 1e-9:
                break
            out.append(Batch(head.created_hour, take, head.product_class))
            head.qty -= take
            remaining_min -= take * min_per_m2
            remaining_room -= take
            if head.qty <= 1e-9:
                self.batches.pop(0)
        return out
