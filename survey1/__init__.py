import hashlib
import json
import time

from otree.api import *

import formation as formation_app
import tombstone
from deidentify import opaque_id

doc = """
Wave 1 post-session survey (build spec Section 11).

Per spec: ordinary oTree Pages with form fields; each answer is ALSO written
to a `SurveyResponse` ExtraModel row (not just the Player field) so it's
event-linked and long-format for export; partner-piping via
`vars_for_template`; per-item latency captured via JS timestamps.

"Don't build a general survey engine -- hardcode the ~6 instruments as
pages" -- this Phase 1 build hardcodes 2 representative instruments
(closeness, trust-in-AI) to demonstrate the full pattern (form fields +
SurveyResponse logging + partner-piping + per-item latency). The exact
validated scale wording for all 6 instruments should replace these
placeholders once pulled from the lit review; follow the same page/logging
pattern for the rest.
"""


LIKERT_7 = [(i, str(i)) for i in range(1, 8)]


class C(BaseConstants):
    NAME_IN_URL = 'survey1'
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 1


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    # -- Instrument 1 (placeholder): closeness / network ties --
    closeness_1 = models.IntegerField(
        choices=LIKERT_7,
        widget=widgets.RadioSelectHorizontal,
        label='PLACEHOLDER ITEM -- replace with validated wording. '
        '1 = strongly disagree, 7 = strongly agree. '
        '"I felt a sense of connection with the other participants in this session."',
    )
    closeness_partner = models.IntegerField(
        choices=LIKERT_7,
        widget=widgets.RadioSelectHorizontal,
        label='PLACEHOLDER ITEM (partner-piped) -- replace with validated wording.',
    )

    # -- Instrument 2 (placeholder): trust in AI-assisted communication --
    trust_ai_1 = models.IntegerField(
        choices=LIKERT_7,
        widget=widgets.RadioSelectHorizontal,
        label='PLACEHOLDER ITEM -- replace with validated wording. '
        '"I would trust a message more if I knew whether AI had helped write it."',
    )
    trust_ai_2 = models.IntegerField(
        choices=LIKERT_7,
        widget=widgets.RadioSelectHorizontal,
        label='PLACEHOLDER ITEM -- replace with validated wording. '
        '"Knowing a message was AI-assisted would change how genuine it feels to me."',
    )

    # Attention check (spec Section 12 integrity flags): a covariate/
    # exclusion flag for analysis, never a blocker -- recorded regardless of
    # the answer given, same as honor_check in intro/__init__.py.
    attention_check = models.IntegerField(
        choices=LIKERT_7,
        widget=widgets.RadioSelectHorizontal,
        label='To confirm you are reading carefully, please select the number 4 for this item.',
    )

    item_latencies_json = models.LongStringField(blank=True)


class SurveyResponse(ExtraModel):
    player = models.Link(Player)
    wave = models.IntegerField()
    instrument = models.StringField()
    target_handle = models.StringField()
    item_key = models.StringField()
    value = models.StringField()
    item_latency_ms = models.IntegerField()
    ts = models.FloatField()


def now_ms():
    return time.time() * 1000


def get_wave(player: Player) -> int:
    return player.session.config['wave']


def first_connected_partner_handle(player: Player) -> str:
    """Partner-piping helper: looks up the participant's formation.Player row
    (a different app's Player model -- cross-app ExtraModel access is fine,
    see spec Section 11) and returns the handle of the first active explicit
    tie, or '' if none exists yet.

    formation.Tie rows are directed edges (Tie.player is whoever clicked
    Connect -- see handle_connect in formation/__init__.py), so a
    participant who only *received* a connect request has no outgoing Tie
    of their own. To answer "who am I connected to" regardless of who
    initiated, check both directions: outgoing ties from this player, and
    incoming ties from other group members whose dst_id points back here.
    """
    try:
        f_player = formation_app.Player.objects_get(
            participant=player.participant, round_number=1
        )
    except Exception:
        return ''
    wave = get_wave(player)
    active_field = 'active_wave1' if wave == 1 else 'active_wave2'

    # Outgoing: ties this player initiated.
    for tie in formation_app.Tie.filter(player=f_player):
        if tie.kind == 'explicit' and getattr(tie, active_field):
            peer = f_player.group.get_player_by_id(int(tie.dst_id))
            return formation_app.player_handle(peer)

    # Incoming: ties initiated by other group members that point at this player.
    my_id = str(f_player.id_in_group)
    for other in f_player.get_others_in_group():
        for tie in formation_app.Tie.filter(player=other):
            if (
                tie.kind == 'explicit'
                and getattr(tie, active_field)
                and tie.dst_id == my_id
            ):
                return formation_app.player_handle(other)
    return ''


def log_responses(player: Player, instrument: str, item_keys, target_handle=''):
    latencies = {}
    if player.item_latencies_json:
        try:
            latencies = json.loads(player.item_latencies_json)
        except (ValueError, TypeError):
            latencies = {}
    wave = get_wave(player)
    for key in item_keys:
        SurveyResponse.create(
            player=player,
            wave=wave,
            instrument=instrument,
            target_handle=target_handle,
            item_key=key,
            value=str(getattr(player, key)),
            item_latency_ms=int(latencies.get(key, 0)),
            ts=now_ms(),
        )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class NetworkCloseness(Page):
    form_model = 'player'
    form_fields = ['closeness_1', 'closeness_partner', 'item_latencies_json']

    @staticmethod
    def vars_for_template(player: Player):
        return {'target_handle': first_connected_partner_handle(player) or 'a participant you connected with'}

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        log_responses(
            player,
            instrument='network_closeness_v1_PLACEHOLDER',
            item_keys=['closeness_1', 'closeness_partner'],
            target_handle=first_connected_partner_handle(player),
        )


class TrustInAI(Page):
    form_model = 'player'
    form_fields = ['trust_ai_1', 'trust_ai_2', 'attention_check', 'item_latencies_json']

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        log_responses(
            player,
            instrument='trust_in_ai_v1_PLACEHOLDER',
            item_keys=['trust_ai_1', 'trust_ai_2', 'attention_check'],
        )


page_sequence = [NetworkCloseness, TrustInAI]


# ---------------------------------------------------------------------------
# Custom export (spec Section 13, Phase 5): survey long table. One row per
# (participant, item) -- see formation/__init__.py PHASE 5 NOTES for the
# shared design decisions (config stamp, zero-args ExtraModel.filter(), etc).
# ---------------------------------------------------------------------------

def _frozen_prompt_hash() -> str:
    return hashlib.sha256(formation_app.C.FROZEN_SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:12]


def custom_export(players):
    yield [
        'session_code', 'room_name', 'condition', 'wave', 'assist_model', 'frozen_prompt_hash',
        'participant_label', 'opaque_id', 'instrument', 'target_handle', 'item_key', 'value',
        'item_latency_ms', 'ts',
    ]

    excluded_ids, _ = tombstone.excluded_keys(players)
    player_ids = {p.id for p in players if p.id not in excluded_ids}
    responses = [r for r in SurveyResponse.filter() if r.player_id in player_ids]

    for r in responses:
        player = r.player
        session = player.session
        cfg = session.config
        label = player.participant.label or ''
        yield [
            session.code,
            cfg.get('room_name', ''),
            cfg.get('condition'),
            cfg.get('wave'),
            cfg.get('assist_model', ''),
            _frozen_prompt_hash(),
            label,
            opaque_id(session.code, label or player.participant.code),
            r.instrument,
            r.target_handle,
            r.item_key,
            r.value,
            r.item_latency_ms,
            r.ts,
        ]
