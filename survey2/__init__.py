import json
import time

from otree.api import *

import formation as formation_app

doc = """
Wave 2 post-session survey (build spec Section 11, Phase 4d).

Mirrors survey1's pattern exactly (ordinary Pages with form fields, each
answer ALSO written to a `SurveyResponse` ExtraModel row, partner-piping,
per-item latency) -- this is Wave 2's re-measurement of the same two
placeholder instruments survey1 hardcodes, so the two waves can be
compared longitudinally. As with survey1: "don't build a general survey
engine -- hardcode the ~6 instruments as pages"; the exact validated scale
wording for all instruments (and any Wave-2-only items, e.g. about the
diffusion item or the recontact experience) should replace these
placeholders once pulled from the lit review, following this same
page/logging pattern.

SurveyResponse is defined locally in this app (not shared/imported from
survey1) -- same per-app-ExtraModel convention survey1 itself follows, and
each row already carries a `wave` field so wave1/wave2 rows both End up as
long-format rows distinguishable at export/analysis time even though they
live in two different apps' tables.
"""


LIKERT_7 = [(i, str(i)) for i in range(1, 8)]


class C(BaseConstants):
    NAME_IN_URL = 'survey2'
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 1


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    # -- Instrument 1 (placeholder): closeness / network ties, Wave-2 re-measure --
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

    # -- Instrument 2 (placeholder): trust in AI-assisted communication, Wave-2 re-measure --
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
    """Same cross-app partner-piping helper as survey1's -- see that
    module's docstring for why both tie directions need to be checked."""
    try:
        f_player = formation_app.Player.objects_get(
            participant=player.participant, round_number=1
        )
    except Exception:
        return ''
    wave = get_wave(player)
    active_field = 'active_wave1' if wave == 1 else 'active_wave2'

    for tie in formation_app.Tie.filter(player=f_player):
        if tie.kind == 'explicit' and getattr(tie, active_field):
            peer = f_player.group.get_player_by_id(int(tie.dst_id))
            return formation_app.player_handle(peer)

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
    form_fields = ['trust_ai_1', 'trust_ai_2', 'item_latencies_json']

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        log_responses(
            player,
            instrument='trust_in_ai_v1_PLACEHOLDER',
            item_keys=['trust_ai_1', 'trust_ai_2'],
        )


page_sequence = [NetworkCloseness, TrustInAI]
