"""Shared disposal detection for the case parsers.

A disposed case has no future hearing, yet the eCourts / consumer / SC / DRT
portals frequently echo the disposal or last-listing date into the "next date"
field. Reading that as ``next_hearing_date`` makes the web/WhatsApp UI render a
phantom "Next Hearing (OVERDUE)" at the disposal date. Every parser that builds
a :class:`~ecourts_client.models.Case` should null ``next_hearing_date`` when
:func:`reads_as_disposed` is true.

Detection uses EXACT normalised token matching (never substring), so active
listing purposes — ``"Final Disposal Misc."``, an interim ``"Disposed of IA
No.5"``, a ``"Disposal Hearing"`` listed for a future date — never trip it.
"""
from __future__ import annotations

from datetime import date
from typing import Any

# ``nature_of_disposal`` values that are placeholders, NOT a real verdict.
_EMPTY_VERDICT = {"", "-", "na", "n/a", "pending", "contested", "uncontested"}
# Leading status/stage token ⇒ disposed (SC/tribunal/consumer normalise a
# compound status like "DISPOSED (…)" / "DISMISSED FOR NON-PROSECUTION (…)").
_DISPOSE_VERBS = {"disposed", "dismissed", "allowed", "decided", "abated", "withdrawn"}
# EXACT (normalised) most-recent hearing purpose ⇒ the case is finished.
_TERMINAL_PURPOSE = {
    "disposed", "disposed off", "disposed of", "case disposed",
    "judgement", "judgment", "order pronounced", "orders pronounced", "decree",
    "dismissed", "allowed",
}


def _norm(s: str | None) -> str:
    """Lower-case, trim, and collapse internal whitespace for token matching."""
    return " ".join((s or "").split()).lower()


def reads_as_disposed(
    *,
    stage: str | None = None,
    hearings: list[Any] | None = None,
    next_hearing_date: date | None = None,
    decision_date: date | None = None,
    nature_of_disposal: str | None = None,
) -> bool:
    """Best-effort "is this case disposed?" across portal shapes.

    Signals (any one is sufficient):

      A. a ``decision_date`` exists, or ``nature_of_disposal`` is a real verdict
         (consumer e-Jagriti ``dateOfDisposal`` / nature); history-independent,
         strongest.
      B. the status/stage LEADING token is a disposal verb ("DISPOSED (…)",
         "DISMISSED FOR NON-PROSECUTION (…)"), or an SC embedded disposal marker.
      C. the case's MOST RECENT hearing was a terminal listing ("Disposed",
         "Judgement", …) AND there is no genuine future next hearing — the
         load-bearing signal for district/HC, where the stage falls back to the
         case-type and the disposal order lands under interim (not final) orders,
         leaving the last hearing row's purpose the only reliable signal.

    ``hearings`` items must expose ``.hearing_date`` and ``.purpose``.
    """
    # A. structured signals.
    if decision_date is not None:
        return True
    if _norm(nature_of_disposal) not in _EMPTY_VERDICT:
        return True
    # B. coarse status / stage — LEADING token only, never substring.
    stat = _norm(stage)
    if stat.split(" ")[0] in _DISPOSE_VERBS:
        return True
    if "(disposal date:" in stat or "disp.type" in stat:  # SC embedded marker
        return True
    # C. terminal most-recent hearing purpose, only when "next" isn't future.
    future_listing = next_hearing_date is not None and next_hearing_date > date.today()
    if hearings and not future_listing:
        latest = max(hearings, key=lambda h: h.hearing_date)
        if _norm(latest.purpose) in _TERMINAL_PURPOSE:
            return True
    return False
