import time

from otree.api import *

doc = """
Wave 2 debrief (build spec Section 11, Phase 4d placeholder).

Last page of the Wave-2 app_sequence. Explains the manipulation (including,
critically, condition 4's undisclosed AI-assist -- this is the first and
only point in the study where those participants are told AI assistance
was ever involved) and offers withdrawal via the `withdrawn` field.

THIS IS A PLACEHOLDER, explicitly deferred to Phase 6 per the roadmap
("Phase 6 -- Hardening: ... debrief, withdrawal, integrity flags"). Still
missing, to be added in Phase 6:
  - Gating logic for participants who exit early / never reach this page
    (the spec requires debrief info reach them out-of-band, e.g. email,
    since a withdrawn/incomplete participant may never load this page).
  - Tombstoning a withdrawn participant's data per the study's data
    retention policy (right now `withdrawn=True` is recorded but nothing
    downstream acts on it).
  - Any integrity flags (e.g. flagging sessions with anomalous timing/
    survey patterns) mentioned in spec Section 14 -- not implemented yet.
The debrief copy below is placeholder wording, not the IRB-approved text.
"""


class C(BaseConstants):
    NAME_IN_URL = 'debrief'
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 1


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    withdrawn = models.BooleanField(
        label='I would like to withdraw my data from this study.',
        widget=widgets.RadioSelectHorizontal,
        initial=False,
    )
    withdrawn_at = models.FloatField(initial=0)


def now_ms():
    return time.time() * 1000


def get_condition(player: Player) -> int:
    return player.session.config['condition']


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class Debrief(Page):
    form_model = 'player'
    form_fields = ['withdrawn']

    @staticmethod
    def vars_for_template(player: Player):
        condition = get_condition(player)
        return {
            # Condition 4 (AI-assist, undisclosed) is the only condition
            # where this page is the first time the participant learns
            # some of their partners' messages may have been AI-assisted
            # without being labeled as such -- see module docstring.
            'was_undisclosed_ai': condition == 4,
            'had_ai_assist': condition in (2, 3, 4),
        }

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        if player.withdrawn:
            player.withdrawn_at = now_ms()


page_sequence = [Debrief]
