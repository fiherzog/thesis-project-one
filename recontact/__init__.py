import time

from otree.api import *

import crosswave

doc = """
Wave 2, step 1: welcome back (build spec Section 10, Phase 4c).

Per Section 2, Wave 2's app_sequence is [recontact, formation, survey2,
debrief] -- this app replaces `intro` for returning participants. Its job
is to bridge continuity across the wave gap: look up this participant's
Wave-1 snapshot (keyed by Room name + participant.label -- see the
`crosswave` module) and, if found, carry their prior handle/avatar/
interests straight into `participant.vars` (same bridging pattern `intro`
uses) without re-asking, and surface their prior ties so they start Wave
2's `formation` knowing who they already know.

If no snapshot is found (most commonly: this session isn't actually
running in an oTree Room, e.g. an ad-hoc test session, so there's no
stable participant.label to key on) this app falls back to the same
profile fields `intro` collects in Wave 1, so `formation`/`survey2` never
see an unset handle either way.
"""


class C(BaseConstants):
    NAME_IN_URL = 'recontact'
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 1

    AVATAR_PRESETS = ['Blue', 'Green', 'Purple', 'Orange', 'Teal', 'Amber']


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    # Only used on the no-snapshot-found fallback path (see module
    # docstring) -- mirrors intro.Player's profile fields exactly.
    handle = models.StringField(
        label='Choose a display handle (only visible to other participants in this session):',
        blank=True,
    )
    avatar_preset = models.StringField(
        label='Choose an avatar color:',
        choices=C.AVATAR_PRESETS,
        widget=widgets.RadioSelect,
        blank=True,
    )
    interest_tags = models.StringField(
        label='List 1-3 interests, separated by commas (e.g. "hiking, jazz, sci-fi"):',
        blank=True,
    )


def now_ms():
    return time.time() * 1000


_SNAPSHOT_CACHE = {}


def get_snapshot(player: Player):
    """Cached per-player lookup so vars_for_template / get_form_fields /
    before_next_page don't each hit the JSON store separately. Keyed by
    player.id (not the player object itself, and not set as an attribute on
    it) since oTree's model classes reject arbitrary, non-field attributes
    on their instances."""
    if player.id not in _SNAPSHOT_CACHE:
        room_name = player.session.config.get('room_name')
        label = player.participant.label
        _SNAPSHOT_CACHE[player.id] = crosswave.load_wave1(room_name, label)
    return _SNAPSHOT_CACHE[player.id]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class Recontact(Page):
    form_model = 'player'

    @staticmethod
    def get_form_fields(player: Player):
        # Snapshot found -> nothing to ask, this becomes a read-only
        # welcome-back/continue page. No snapshot -> same profile
        # questions intro asks fresh Wave-1 participants.
        if get_snapshot(player):
            return []
        return ['handle', 'avatar_preset', 'interest_tags']

    @staticmethod
    def vars_for_template(player: Player):
        snap = get_snapshot(player)
        return {
            'has_snapshot': snap is not None,
            'prior_handle': snap['handle'] if snap else None,
            'prior_ties': snap['ties'] if snap else [],
            'prior_adopted': snap['adopted'] if snap else False,
            'diffusion_item': snap['diffusion_item'] if snap else None,
        }

    @staticmethod
    def error_message(player: Player, values):
        if get_snapshot(player):
            return
        handle = (values.get('handle') or '').strip()
        if not handle:
            return 'Please choose a handle.'
        tags = [t.strip() for t in (values.get('interest_tags') or '').split(',') if t.strip()]
        if not (1 <= len(tags) <= 3):
            return 'Please list between 1 and 3 interests, separated by commas.'

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        # Bridge identity into participant.vars for this (Wave-2) session,
        # same role intro/Tutorial.before_next_page plays in Wave 1 --
        # formation/survey2's Player rows already exist by the time these
        # pages run, so this is the only channel they can read it through.
        snap = get_snapshot(player)
        if snap:
            player.participant.vars.update(
                handle=snap['handle'],
                avatar_preset=snap.get('avatar_preset'),
                interest_tags=snap.get('interest_tags'),
                # Wave 2 participants already consented in Wave 1; there is
                # no re-consent page in this build (see module docstring --
                # Phase 6 hardening may revisit re-consent/withdrawal).
                consented=True,
                consented_at=player.participant.vars.get('consented_at', 0),
                prior_ties=snap['ties'],
            )
        else:
            player.participant.vars.update(
                handle=player.handle,
                avatar_preset=player.avatar_preset,
                interest_tags=player.interest_tags,
                consented=True,
                consented_at=now_ms(),
                prior_ties=[],
            )


page_sequence = [Recontact]


# ---------------------------------------------------------------------------
# Custom export (spec Section 15, Phase 6d): attrition/over-recruit report.
# One row per distinct room_name seen among the `players` passed in (usually
# just one, since this is normally downloaded for a single Wave-2 session's
# admin Data page) -- see crosswave.attrition_report for the actual query.
# ---------------------------------------------------------------------------


def custom_export_attrition(players):
    yield [
        'room_name', 'wave1_completers_n', 'wave2_started_n', 'not_yet_returned_n',
        'not_yet_returned_labels',
    ]

    room_names = sorted({p.session.config.get('room_name', '') for p in players})
    for room_name in room_names:
        if not room_name:
            continue
        report = crosswave.attrition_report(room_name)
        yield [
            room_name,
            len(report['wave1_completers']),
            len(report['wave2_started']),
            len(report['not_yet_returned']),
            ', '.join(report['not_yet_returned']),
        ]
