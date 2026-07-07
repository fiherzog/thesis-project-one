"""
Cross-wave store (spec Section 10, Phase 4b).

oTree `participant.vars` do NOT survive across separate sessions, and
Wave-1 and Wave-2 for a given cohort are two different oTree sessions
(joined via the same oTree Room so the same real person keeps the same
`participant.label` in both). So anything that needs to survive the
5-7 day gap between waves -- the tie set formed in Wave-1 `formation`,
and each participant's adoption state for the diffusion item -- has to be
snapshotted somewhere external to any single session.

Per spec Section 10 ("a dedicated table or a JSON file on the server"),
this build uses a single JSON file on disk, keyed by (room_name,
participant_label). `room_name` is stored explicitly as a session-config
field on each wave1/wave2 SESSION_CONFIGS entry (see settings.py) rather
than introspected from oTree's internal Room-to-session binding -- the
explicit value is simpler and more robust than reverse-engineering which
Room a given session was created in.

This is intentionally a plain, standalone module (not an oTree app) since
it's just a small shared utility imported by both `formation` (writes, at
the end of Wave-1) and `recontact` (reads, at the start of Wave-2).

Concurrency note: multiple participants in the same Wave-1 group may
finish `formation` at roughly the same time, each triggering a snapshot
write. Reads-modify-writes are serialized with a POSIX advisory file lock
(`fcntl.flock`) around the whole read-merge-write cycle so concurrent
snapshots for different participants can't clobber each other.
"""

import fcntl
import json
import os

STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_cross_wave_store.json')
LOCK_PATH = STORE_PATH + '.lock'


def _key(room_name: str, participant_label: str) -> str:
    return f'{room_name}::{participant_label}'


def _load_all() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    with open(STORE_PATH, 'r') as f:
        content = f.read().strip()
    if not content:
        return {}
    return json.loads(content)


def _save_all(data: dict) -> None:
    tmp_path = STORE_PATH + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, STORE_PATH)


def _with_lock(fn):
    """Run fn() while holding an exclusive advisory lock on LOCK_PATH, so
    concurrent snapshot_wave1() calls from different participants in the
    same Wave-1 group can't interleave their read-modify-write cycles."""
    with open(LOCK_PATH, 'w') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def snapshot_wave1(room_name: str, participant_label: str, data: dict) -> None:
    """Persist a Wave-1 end-of-formation snapshot for one participant.

    `data` is a plain JSON-serializable dict -- see formation/__init__.py's
    `Formation.before_next_page` snapshot hook for the exact shape (handle,
    avatar_preset, interest_tags, ties, adopted, diffusion_item, ts).
    Overwrites any prior snapshot for the same (room_name, participant_label)
    -- there should only ever be one Wave-1 session per room per label.
    """
    if not room_name or not participant_label:
        return

    def _do():
        store = _load_all()
        store[_key(room_name, participant_label)] = data
        _save_all(store)

    _with_lock(_do)


def load_wave1(room_name: str, participant_label: str):
    """Return the Wave-1 snapshot dict for (room_name, participant_label),
    or None if no snapshot exists (e.g. this participant never completed
    Wave-1 formation, or Wave-2 is being tested without a Room set up)."""
    if not room_name or not participant_label:
        return None
    store = _load_all()
    return store.get(_key(room_name, participant_label))


def attrition_report(room_name: str) -> dict:
    """Cross-wave attrition summary for `room_name` (build spec Section 15,
    Phase 6d: "attrition over-recruit tooling"): which Wave-1 completers
    (participants who finished `formation` and got a snapshot -- see
    `snapshot_wave1`) have shown up for Wave 2 so far, and which haven't
    yet -- so the researcher running Wave 2 can see how many no-shows there
    are and decide how many replacements to over-recruit. Returns raw
    label lists/counts only; this module doesn't invent an over-recruit
    formula.

    "Started Wave 2" means a Participant in a session whose config has
    `room_name == room_name` and `wave == 2`, whose `participant.label`
    matches a Wave-1 completer, and who has progressed past the very first
    page (`_index_in_pages > 0`) -- i.e. actually opened their Room link,
    not just an unused session slot oTree pre-allocated.
    """
    if not room_name:
        return {'wave1_completers': [], 'wave2_started': [], 'not_yet_returned': []}

    store = _load_all()
    prefix = f'{room_name}::'
    wave1_completers = sorted(
        key[len(prefix):] for key in store if key.startswith(prefix)
    )

    import otree.models  # local import: avoids import-order coupling at plain-module import time

    wave2_started = set()
    for participant in otree.models.Participant.objects_filter():
        cfg = participant.session.config
        if cfg.get('room_name') != room_name or cfg.get('wave') != 2:
            continue
        if participant.label and participant._index_in_pages > 0:
            wave2_started.add(participant.label)

    not_yet_returned = sorted(set(wave1_completers) - wave2_started)
    return {
        'wave1_completers': wave1_completers,
        'wave2_started': sorted(wave2_started),
        'not_yet_returned': not_yet_returned,
    }
