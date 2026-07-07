import hashlib
import time

from otree.api import *

import formation as formation_app
from deidentify import opaque_id

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


# ---------------------------------------------------------------------------
# Custom export (spec Section 13, Phase 5): withdrawal/integrity table --
# one row per Wave-2 participant who reached debrief. See formation's
# PHASE 5 NOTES for the shared design decisions (config stamp, etc).
# ---------------------------------------------------------------------------

def _frozen_prompt_hash() -> str:
    return hashlib.sha256(formation_app.C.FROZEN_SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:12]


def custom_export(players):
    yield [
        'session_code', 'room_name', 'condition', 'wave', 'assist_model', 'frozen_prompt_hash',
        'participant_label', 'opaque_id', 'withdrawn', 'withdrawn_at',
        'was_undisclosed_ai', 'had_ai_assist',
    ]

    for p in players:
        session = p.session
        cfg = session.config
        label = p.participant.label or ''
        condition = cfg.get('condition')
        yield [
            session.code,
            cfg.get('room_name', ''),
            condition,
            cfg.get('wave'),
            cfg.get('assist_model', ''),
            _frozen_prompt_hash(),
            label,
            opaque_id(session.code, label or p.participant.code),
            p.withdrawn,
            p.withdrawn_at,
            condition == 4,
            condition in (2, 3, 4),
        ]


# ---------------------------------------------------------------------------
# Out-of-band debrief delivery (build spec Section 11/15, Phase 6c):
# participants who withdraw or otherwise never reach this last page in the
# Wave-2 app_sequence still need the debrief content delivered somehow (e.g.
# by email) -- this study never collects an email address itself, so this
# tooling only generates the text content and lists who needs it; actually
# sending it is a manual/out-of-band step outside this codebase.
# ---------------------------------------------------------------------------


def debrief_text(had_ai_assist: bool, was_undisclosed_ai: bool) -> str:
    """Plain-text mirror of Debrief.html's conditional copy, for manual/
    out-of-band delivery to participants who never reached this page."""
    lines = [
        "PLACEHOLDER DEBRIEF TEXT -- not IRB-approved wording, see debrief/__init__.py docstring.",
        "",
        "Thank you for participating in both sessions of this study. This research "
        "looked at how people form connections and share information in online "
        "groups, and how AI writing assistance affects that process.",
    ]
    if had_ai_assist:
        lines.append("")
        lines.append(
            "In this study, some participants had access to an AI-assist tool that "
            "could help draft messages."
        )
        if was_undisclosed_ai:
            lines.append("")
            lines.append(
                "In your session, when a message was AI-assisted, this was not shown "
                "to the recipient. We did not tell you this in advance because it was "
                "part of what we were studying: whether undisclosed AI assistance "
                "changes how people build trust and connection. We're telling you now, "
                "at the end of the study."
            )
    lines.append("")
    lines.append(
        "If, now that you know this, you would prefer that your data not be used in "
        "this research, please contact the research team. Withdrawing will not "
        "affect your compensation."
    )
    return "\n".join(lines)


def custom_export_noncompleters(players):
    """One row per Wave-2 participant who actually started the Wave-2
    app_sequence but never finished it (so never saw this page's
    disclosure/withdrawal offer), with the debrief text they still need
    delivered out-of-band. Participant slots nobody ever showed up for
    (`_index_in_pages == 0`) are skipped -- they were never real study
    participants, so there's nothing to debrief them about."""
    yield [
        'session_code', 'room_name', 'condition', 'wave',
        'participant_label', 'opaque_id', 'debrief_text',
    ]

    for p in players:
        participant = p.participant
        if participant._index_in_pages == 0:
            continue  # never started; not an actual participant
        if participant._index_in_pages > participant._max_page_index:
            continue  # finished the app_sequence; reached this page normally

        session = p.session
        cfg = session.config
        label = participant.label or ''
        condition = cfg.get('condition')
        had_ai_assist = condition in (2, 3, 4)
        was_undisclosed_ai = condition == 4
        yield [
            session.code,
            cfg.get('room_name', ''),
            condition,
            cfg.get('wave'),
            label,
            opaque_id(session.code, label or participant.code),
            debrief_text(had_ai_assist, was_undisclosed_ai),
        ]
