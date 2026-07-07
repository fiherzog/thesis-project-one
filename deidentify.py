import hashlib

doc = """
Shared de-identification helper for the export suite (build spec Section 13,
Phase 5): "Generate the de-identified copy (handles -> opaque ids) in the
same job." A plain, non-oTree-app root module (like `crosswave.py`) so every
app's `custom_export*` functions can produce the *same* opaque id for a given
(session, participant) pair without duplicating the hash logic four times.
"""


def opaque_id(session_code: str, participant_label: str) -> str:
    """Deterministic, one-way id for a participant within a given session.
    Same (session_code, participant_label) always hashes to the same id, so
    rows across the different export tables still join correctly after
    de-identification -- but the id itself reveals nothing about the
    participant's actual handle or room label. Not a general-purpose secure
    hash (no salt/pepper): the input space (room label file strings) is
    small and this is meant to keep a corpus file from casually displaying
    handles/labels, not to withstand a determined re-identification attempt.
    """
    raw = f'{session_code}:{participant_label or ""}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]
