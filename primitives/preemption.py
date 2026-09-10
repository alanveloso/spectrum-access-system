"""Class-versus-class preemption (D12). Not availability expiry, not Core apply."""

from __future__ import annotations

from primitives.access import OrderedAccess


def class_preempts(
    access: OrderedAccess, actor_class_id: str, target_class_id: str
) -> bool:
    """True only when actor ranks strictly above a preemptible target.

    Equal priority (including the same class) does not preempt.
    Unknown class ids fail closed via ``OrderedAccess.get``.
    """
    if actor_class_id == target_class_id:
        return False
    actor = access.get(actor_class_id)
    target = access.get(target_class_id)
    if actor.priority <= target.priority:
        return False
    return target.preemptible
