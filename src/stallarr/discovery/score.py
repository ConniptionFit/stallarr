"""Rank tracker listings by how much upload they are likely to earn.

The question is "upload earned per GB of disk, over the months we keep it". Size
mostly cancels out of that: a bigger torrent earns more from each leecher and
costs proportionally more disk. What is left is how many people will want it
from us, and what the download costs our ratio. So the score is

    (demand * w_demand + log2(1 + snatches/day) * w_velocity)
      * age curve * upload multiplier * competition
      / (1 + paid fraction * paid_penalty)

Pure functions only, so it can be tested and tuned without a network.

Unknown is never zero. A site that omits `leechers` gets no demand credit but
is not excluded; one that omits `free` is priced at full cost, which is the
conservative reading for a ratio builder.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from ..config import DiscoveryConfig
from ..models import TrackerListing
from ..utils import normalise_release

GB = 1024**3
DAY = 86400

#: Under this age a swarm is still in its first rush: big payoff, short.
BURST_DAYS = 3
#: Past this age, steady snatches mean demand that will still be there next
#: month, which is what a long-term seed lives on.
LONG_TAIL_DAYS = 30

#: Age curve per strategy. Long-tail is weighted up because the point of the
#: feature is seeds that keep paying; the middle band is weighted down because
#: its rush is fading and nothing yet says demand will settle rather than stop.
AGE_FACTORS = {"burst": 1.0, "cooling": 0.75, "long-tail": 1.25, "unknown": 1.0}


@dataclass(slots=True)
class Scored:
    listing: TrackerListing
    score: float
    strategy: str
    #: Every factor that went into the score, for the UI's breakdown.
    parts: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        lst = self.listing
        return {
            "tracker": lst.tracker,
            "torrent_id": lst.torrent_id,
            "name": lst.name,
            "size": lst.size,
            "seeders": lst.seeders,
            "leechers": lst.leechers,
            "times_completed": lst.times_completed,
            "created_at": lst.created_at,
            "free_pct": lst.free_pct,
            "double_upload": lst.double_upload,
            "featured": lst.featured,
            "highspeed": lst.highspeed,
            "category": lst.category,
            "score": self.score,
            "strategy": self.strategy,
            "parts": self.parts,
            # download_url is deliberately absent: it carries the rsskey.
        }


def strategy_for(age_days: float | None) -> str:
    if age_days is None:
        return "unknown"
    if age_days < BURST_DAYS:
        return "burst"
    if age_days < LONG_TAIL_DAYS:
        return "cooling"
    return "long-tail"


def score(listing: TrackerListing, cfg: DiscoveryConfig, now: float | None = None) -> Scored:
    now = time.time() if now is None else now
    weights = cfg.weights
    seeders = listing.seeders or 0
    leechers = listing.leechers or 0

    age_days = None
    if listing.created_at:
        age_days = max((now - listing.created_at) / DAY, 0.0)

    demand = leechers / (seeders + 1)
    per_day = 0.0
    if listing.times_completed is not None and age_days is not None:
        # Floor at one day so a torrent an hour old does not read as
        # thousands of snatches a day.
        per_day = listing.times_completed / max(age_days, 1.0)
    velocity = math.log2(1 + per_day)

    strategy = strategy_for(age_days)
    age_factor = AGE_FACTORS[strategy]
    upload_mult = 2.0 if listing.double_upload else 1.0

    competition = 1.0
    if weights.crowded_seeders > 0 and seeders > weights.crowded_seeders:
        competition = weights.crowded_seeders / seeders
    if listing.highspeed:
        competition *= 0.5

    free = listing.free_pct if listing.free_pct is not None else 0
    paid = 1 - free / 100
    cost = 1 + paid * weights.paid_penalty

    raw = (demand * weights.demand + velocity * weights.velocity) * age_factor
    total = raw * upload_mult * competition / cost

    return Scored(
        listing=listing,
        score=round(total, 3),
        strategy=strategy,
        parts={
            "demand": round(demand, 3),
            "snatches_per_day": round(per_day, 2),
            "velocity": round(velocity, 3),
            "age_days": round(age_days, 1) if age_days is not None else -1,
            "age_factor": age_factor,
            "upload_mult": upload_mult,
            "competition": round(competition, 3),
            "cost": round(cost, 3),
        },
    )


def exclusion(
    listing: TrackerListing,
    cfg: DiscoveryConfig,
    *,
    held_hashes: set[str],
    held_names: set[str],
    grabbed: set[tuple[str, str]],
) -> str | None:
    """Why this listing is not a candidate at all, or None if it is."""
    if (listing.tracker, listing.torrent_id) in grabbed:
        return "already grabbed"
    if listing.infohash and listing.infohash in held_hashes:
        return "already in the client"
    if normalise_release(listing.name) in held_names:
        return "already in the client"
    if listing.seeders is not None and listing.seeders == 0:
        # Nobody to download it from; it would sit at 0% using a slot.
        return "no seeders"
    if cfg.freeleech_only and (listing.free_pct or 0) < 100:
        return "not freeleech"
    if listing.size is None:
        # The disk budget cannot be enforced against an unknown size.
        return "size unknown"
    if listing.size < cfg.min_size_gb * GB:
        return "too small"
    if cfg.max_size_gb > 0 and listing.size > cfg.max_size_gb * GB:
        return "too large"
    return None


def rank(
    listings: list[TrackerListing],
    cfg: DiscoveryConfig,
    *,
    held_hashes: set[str],
    held_names: set[str],
    grabbed: set[tuple[str, str]],
    now: float | None = None,
) -> tuple[list[Scored], dict[str, int]]:
    """Score every eligible listing, best first, plus a count of exclusions."""
    excluded: dict[str, int] = {}
    scored: list[Scored] = []
    seen: set[tuple[str, str]] = set()
    for listing in listings:
        key = (listing.tracker, listing.torrent_id)
        if key in seen:
            continue
        seen.add(key)
        reason = exclusion(
            listing, cfg, held_hashes=held_hashes, held_names=held_names, grabbed=grabbed
        )
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        scored.append(score(listing, cfg, now))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[: max(cfg.max_candidates, 0)], excluded
