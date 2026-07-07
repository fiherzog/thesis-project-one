"""
Withdrawal tombstoning (build spec Section 11: "a `withdrawn` flag that
tombstones the record at export") and consent gating (Section 11: "block
export of un-consented records"). A plain, non-oTree-app root module (like
crosswave.py / deidentify.py) so every app's `custom_export*` function can
ask "should this participant's data be excluded from export?" without
duplicating the cross-session query.

Withdrawal is recorded once, on the Wave-2 Debrief page (see
debrief/__init__.py) -- but the real participant it names is identified by
`participant.label` within a Room, the same cross-wave join key
crosswave.py and every custom_export* function already use. Tombstoning by
label+room (not just the one session where the withdrawal was recorded)
means a withdrawn participant's Wave-1 rows are excluded too, since it's
the same real person's data being withdrawn -- not just their Wave-2
debrief questionnaire answers.

Ad-hoc/demo sessions (room_name == '') have no cross-session identity to
tombstone by -- there is nothing this module can safely match a withdrawal
against for those, so it treats every ad-hoc participant as "not
withdrawn" (never masks demo data it can't actually attribute).

debrief's own custom_export deliberately does NOT use this module -- that
table is the source of truth for *why* something is being tombstoned
elsewhere, so it must keep reporting withdrawn=True rows rather than
masking them.
"""


def withdrawn_labels(room_name: str) -> set:
    """Every participant.label within `room_name` who has withdrawn (per
    debrief.Player.withdrawn), across every Wave-2 session ever bound to
    that room."""
    if not room_name:
        return set()

    import debrief  # local import: avoids import-order coupling at app-package init time

    labels = set()
    for player in debrief.Player.objects_filter():
        if not player.withdrawn:
            continue
        if player.session.config.get('room_name') != room_name:
            continue
        label = player.participant.label
        if label:
            labels.add(label)
    return labels


def is_excluded(participant, room_name: str, withdrawn_cache: dict) -> bool:
    """True if `participant` should be excluded from export: never
    consented, or withdrew (in this room, at any wave). `withdrawn_cache`
    is a plain dict the caller reuses across calls within one export
    function so `withdrawn_labels()` (a full table scan) runs at most once
    per distinct room_name instead of once per row.
    """
    if not participant.vars.get('consented'):
        return True

    label = participant.label
    if not label:
        return False
    if room_name not in withdrawn_cache:
        withdrawn_cache[room_name] = withdrawn_labels(room_name)
    return label in withdrawn_cache[room_name]


def excluded_keys(players):
    """For a custom_export's `players` list: the set of excluded player.ids,
    and the set of excluded (session_id, id_in_group) pairs -- the latter
    because ExtraModels like formation.Tie/Message reference the *other*
    party in a tie/message only by id_in_group (a string), not player.id
    (see formation/__init__.py's Tie.dst_id / Message.recipient_id). Both
    forms are needed so a tombstoned participant disappears both as the
    row-owner and as anyone else's counterpart.
    """
    cache = {}
    excluded_ids = set()
    excluded_idg = set()
    for p in players:
        room_name = p.session.config.get('room_name', '')
        if is_excluded(p.participant, room_name, cache):
            excluded_ids.add(p.id)
            excluded_idg.add((p.session_id, p.id_in_group))
    return excluded_ids, excluded_idg
