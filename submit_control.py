"""Short, process-local admission tickets for outgoing writes.

Policy and network work happen outside this lock. A successful commit is the
logical start of a write: a later pause stops subsequent admissions, but cannot
recall a request already admitted. Epochs prevent pause/resume from reviving an
old ticket; Page automation epochs leave manual writes independent.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import threading


_lock = threading.Lock()
_global_epoch = 0
_page_epochs = {}
_automation_epochs = {}
_profile_epochs = {}
_profile_pending = {}
_page_pending = {}
_automation_pending = {}
_global_pending = 0


@dataclass
class Ticket:
    page_id: str
    global_epoch: int
    page_epoch: int
    automation_epoch: int | None
    profile_id: str | None = None
    profile_epoch: int | None = None
    committed: bool = False


def snapshot(page_id, *, automatic=False, profile_id=None):
    page_id = str(page_id)
    profile_id = str(profile_id) if profile_id is not None else None
    with _lock:
        return Ticket(page_id, _global_epoch, _page_epochs.get(page_id, 0),
                      _automation_epochs.get(page_id, 0) if automatic else None,
                      profile_id, _profile_epochs.get(profile_id, 0) if profile_id is not None else None)


def invalidate_all():
    global _global_epoch
    with _lock:
        _global_epoch += 1


def invalidate_page(page_id):
    page_id = str(page_id)
    with _lock:
        _page_epochs[page_id] = _page_epochs.get(page_id, 0) + 1


def invalidate_automation(page_id):
    page_id = str(page_id)
    with _lock:
        _automation_epochs[page_id] = _automation_epochs.get(page_id, 0) + 1


def invalidate_profile(profile_id):
    profile_id = str(profile_id)
    with _lock:
        _profile_epochs[profile_id] = _profile_epochs.get(profile_id, 0) + 1


@contextmanager
def profile_mutation(profile_id):
    """Publish an in-memory profile stop atomically with admission tickets."""
    profile_id = str(profile_id)
    with _lock:
        _profile_epochs[profile_id] = _profile_epochs.get(profile_id, 0) + 1
        _profile_pending[profile_id] = _profile_pending.get(profile_id, 0) + 1
    try:
        yield
    finally:
        with _lock:
            _profile_epochs[profile_id] = _profile_epochs.get(profile_id, 0) + 1
            _profile_pending[profile_id] -= 1
            if not _profile_pending[profile_id]:
                del _profile_pending[profile_id]


@contextmanager
def mutation(page_id=None, *, automatic=False):
    """Invalidate old tickets and reject admissions until a mutation finishes."""
    global _global_epoch, _global_pending
    page_id = str(page_id) if page_id is not None else None
    epochs = _automation_epochs if automatic else _page_epochs
    pending = _automation_pending if automatic else _page_pending
    with _lock:
        if page_id is None:
            _global_epoch += 1
            _global_pending += 1
        else:
            epochs[page_id] = epochs.get(page_id, 0) + 1
            pending[page_id] = pending.get(page_id, 0) + 1
    try:
        yield
    finally:
        with _lock:
            if page_id is None:
                _global_epoch += 1
                _global_pending -= 1
            else:
                epochs[page_id] = epochs.get(page_id, 0) + 1
                pending[page_id] -= 1
                if not pending[page_id]:
                    del pending[page_id]


def commit(ticket, paused):
    """Admit once against current epochs and an in-memory pause predicate.

    ``paused`` must only read in-memory state; never query DB/network here.
    """
    with _lock:
        if (ticket.committed or paused() or _global_pending or _page_pending.get(ticket.page_id, 0)
                or (ticket.automation_epoch is not None and _automation_pending.get(ticket.page_id, 0))
                or ticket.global_epoch != _global_epoch
                or ticket.page_epoch != _page_epochs.get(ticket.page_id, 0)
                or (ticket.profile_id is not None
                    and (_profile_pending.get(ticket.profile_id, 0)
                         or ticket.profile_epoch != _profile_epochs.get(ticket.profile_id, 0)))
                or (ticket.automation_epoch is not None
                    and ticket.automation_epoch != _automation_epochs.get(ticket.page_id, 0))):
            return False
        ticket.committed = True
        return True
