"""Murmur3-based normalized hashing for gradual rollout and variant selection.

The embedded engine uses ``mmh3`` (already an SDK dependency) to compute a
deterministic, normalized hash in the inclusive range ``[1, normalizer]``.

The formula mirrors every other Unleash client SDK:

    mmh3.hash(f"{group}:{identifier}", seed, signed=False) % normalizer + 1

Two different seeds are used across the engine:

* Gradual-rollout / flexibleRollout strategies use ``seed=0`` (the default).
* Variant distribution uses ``VARIANT_HASH_SEED`` (``86028157``).

Implementers MUST verify both seeds against the client-specification suite
(``08-strategy-constraints`` / gradual-rollout specs and the variant specs)
rather than trusting these docstrings alone.
"""

from __future__ import annotations

#: Seed used exclusively for variant weight distribution. Gradual-rollout
#: strategies use the default seed of ``0``.
VARIANT_HASH_SEED: int = 86028157


def normalized_hash(
    group: str,
    identifier: str,
    normalizer: int = 100,
    seed: int = 0,
) -> int:
    """Compute a normalized murmur3 hash in the inclusive range ``[1, normalizer]``.

    :param group: The activation group (e.g. ``groupId`` or feature name).
    :param identifier: The stickiness identifier (e.g. ``userId``).
    :param normalizer: Upper bound of the normalized range (default ``100``).
    :param seed: Murmur3 seed. ``0`` for rollout, ``VARIANT_HASH_SEED`` for variants.
    :return: An integer in ``[1, normalizer]``.
    """
    raise NotImplementedError
