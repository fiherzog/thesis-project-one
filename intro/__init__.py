import time

from otree.api import *

doc = """
Wave 1, step 1: consent -> profile creation -> tutorial.

Per spec Section 11: consent language is uniform across conditions and
does not reveal the participant's own condition assignment. Per Section 2,
this app only runs in the Wave 1 session (`app_sequence = [intro, formation,
survey1]`); Wave 2 uses `recontact` instead (Phase 4).

Values entered here (handle, avatar, interest tags, consent) are copied into
`participant.vars` at the end of the Tutorial page, which is how the
`formation` and `survey1` apps read them -- NOT via cross-app Player field
access, since oTree creates all apps' Player rows for the whole session
before any page is played, so formation's Player row does not exist with
useful defaults at the time this app's pages run.
"""


class C(BaseConstants):
    NAME_IN_URL = 'intro'
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 1

    AVATAR_PRESETS = ['Blue', 'Green', 'Purple', 'Orange', 'Teal', 'Amber']


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    consented = models.BooleanField(
        label='I have read and understood the information above, and I consent to participate in this study.',
        widget=widgets.RadioSelectHorizontal,
    )
    consented_at = models.FloatField(initial=0)

    handle = models.StringField(
        label='Choose a display handle (only visible to other participants in this session):'
    )
    avatar_preset = models.StringField(
        label='Choose an avatar color:',
        choices=C.AVATAR_PRESETS,
        widget=widgets.RadioSelect,
    )
    interest_tags = models.StringField(
        label='List 1-3 interests, separated by commas (e.g. "hiking, jazz, sci-fi"):'
    )


def now_ms():
    return time.time() * 1000


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class Consent(Page):
    form_model = 'player'
    form_fields = ['consented']

    @staticmethod
    def error_message(player: Player, values):
        if not values['consented']:
            return 'You must indicate consent to continue with the study.'

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        player.consented_at = now_ms()


class Profile(Page):
    form_model = 'player'
    form_fields = ['handle', 'avatar_preset', 'interest_tags']

    @staticmethod
    def error_message(player: Player, values):
        handle = (values.get('handle') or '').strip()
        if not handle:
            return 'Please choose a handle.'
        tags = [t.strip() for t in (values.get('interest_tags') or '').split(',') if t.strip()]
        if not (1 <= len(tags) <= 3):
            return 'Please list between 1 and 3 interests, separated by commas.'


class Tutorial(Page):
    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        # Bridge intro-collected identity into participant.vars so the
        # formation/survey1 apps (created before these pages were answered)
        # can read it. See module docstring.
        player.participant.vars.update(
            handle=player.handle,
            avatar_preset=player.avatar_preset,
            interest_tags=player.interest_tags,
            consented=player.consented,
            consented_at=player.consented_at,
        )


page_sequence = [Consent, Profile, Tutorial]
