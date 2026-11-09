"""Deferred execution of side effects until the owning transaction commits.

Why this exists
---------------
Order placement used to push onto ``queue:orders`` and publish WS events
BEFORE the DB transaction that created the order row was committed. The
matching worker would then BRPOP the action and read the taker row as
``MISSING`` → "Match refused" → the order was stranded in ``new`` with its
funds locked forever (reproduced under load, orders 10159/11158/12156).
Likewise, pub/sub is fire-and-forget: an event published inside a
transaction that later rolls back is a ghost the clients can never un-see.

Fix: register every Redis side effect (queue push, order HASH, open-orders
index, book removals, pub/sub publish) as a factory and run it strictly
after the real COMMIT. A rollback therefore produces no side effects at
all, and the worker can never observe an action that references an
uncommitted row.

Guarantees / limits
-------------------
* Deferred effects run in registration order (one sequential task).
* Effects are best-effort: if one raises (e.g. Redis blip), the error is
  logged and the remaining effects still run. A fully closed window
  (crash between commit and effect) needs a transactional outbox —
  tracked as a follow-up, not solved here.
* The hook attaches to the underlying sync session, so it works for
  FastAPI request sessions AND the worker's ``session.begin()`` blocks.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Coroutine

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_INFO_KEY = "post_commit_effects"
_HOOKED_KEY = "post_commit_hooked"


def defer_after_commit(session: AsyncSession, factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    """Schedule an awaitable factory to run after `session`'s commit.

    The factory takes no arguments; use functools.partial or a closure
    that captures only IMMUTABLE VALUES (ids, dicts, scalars), never ORM
    objects you intend to mutate later.
    """
    sync = session.sync_session
    bag = sync.info.setdefault(_INFO_KEY, [])
    if not sync.info.get(_HOOKED_KEY):
        sync.info[_HOOKED_KEY] = True
        event.listen(sync, "after_commit", _fire)
    bag.append(factory)


def _fire(sync_session: Any, *_args: Any) -> None:
    pend = sync_session.info.pop(_INFO_KEY, [])
    if pend:
        # We are inside an awaited commit in an event loop, so a loop exists.
        asyncio.ensure_future(_run(pend))


async def _run(pend: list[Callable[[], Awaitable[Any]]]) -> None:
    for factory in pend:
        try:
            await factory()
        except Exception:
            logger.exception("post-commit side effect failed; remaining effects continue")


__all__ = ["defer_after_commit"]
