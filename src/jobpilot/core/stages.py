"""Stages of an application, shown and edited on the site; the hh worker only suggests them."""
from __future__ import annotations

SENT = "sent"
VIEWED = "viewed"
INVITATION = "invitation"
INTERVIEW = "interview"
OFFER = "offer"
REJECTED = "rejected"
GHOSTED = "ghosted"

# Labels for the site; a stage set by hand may also be any other text.
LABELS = {
    SENT: "отправлен",
    VIEWED: "просмотрен",
    INVITATION: "приглашение",
    INTERVIEW: "собеседование",
    OFFER: "оффер",
    REJECTED: "отказ",
    GHOSTED: "загостили",
}

# The system only moves an application forward along this line (or to "rejected").
_ORDER = (SENT, VIEWED, INVITATION, INTERVIEW, OFFER)
FINAL = (REJECTED, OFFER)


def stage_for_status(status: str) -> str | None:
    """Stage for what the apply worker recorded; None for vacancies we did not apply to."""
    if status in ("applied", "already"):
        return SENT
    if status.startswith("answered:"):
        return stage_for_hh_state(status.removeprefix("answered:"))
    return None


def stage_for_hh_state(state: str) -> str | None:
    """Stage for the tag of a response on hh.ru/applicant/negotiations (not-viewed, viewed, discard, ...)."""
    if state == "not-viewed":
        return SENT
    if state == "viewed":
        return VIEWED
    if "discard" in state or "reject" in state or "deny" in state:
        return REJECTED
    if "invit" in state or "interview" in state:
        return INVITATION
    return None


def system_may_move(current: str | None, new: str) -> bool:
    """Whether an automatic update may replace `current` with `new` (manual stages are checked by the caller)."""
    if current == new or current in FINAL:
        return False
    if current is None or new == REJECTED:
        return True
    if current not in _ORDER or new not in _ORDER:
        return False
    return _ORDER.index(new) > _ORDER.index(current)
