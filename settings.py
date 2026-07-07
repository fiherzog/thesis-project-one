from os import environ

# ---------------------------------------------------------------------------
# Provenance Network Platform -- oTree settings
#
# Build invariants (see build spec, restated here so they aren't lost across
# phases):
#   - oTree >= 6.0, async live pages.
#   - The `formation` app is ONE live Page for the whole session, not a page
#     sequence. live_method branches must be implemented as async
#     generators (`async def ...: yield {...}`), not plain coroutines that
#     `return` -- oTree 6.0.15 raises LiveMethodBadReturnValue otherwise.
#   - `condition` and `wave` come from session config, never from the client.
#   - The Anthropic API key is read server-side only (see formation app).
#
# Phase 4 status: all four Wave-1 conditions (cond1 = control, cond2 =
# AI-assist/no disclosure UI, cond3 = AI-assist/disclosed, cond4 =
# AI-assist/undisclosed -- see formation app for the badge logic) now each
# have a matching Wave-2 config (`cond{1..4}_wave2`, app_sequence =
# [recontact, formation, survey2, debrief]). Each cond's wave1/wave2 pair
# shares one oTree Room (`room_name` config key + ROOMS below) so the same
# real participant keeps the same `participant.label` across both waves --
# that label is the join key into the `crosswave` JSON store (see
# formation app's before_next_page snapshot hook and recontact app).
# ---------------------------------------------------------------------------

SESSION_CONFIG_DEFAULTS = dict(
    real_world_currency_per_point=1.00,
    participation_fee=0.00,
    doc="",
    # --- study-wide knobs (Decision register, spec Section 16) ---
    discovery_mode='open',  # Decision C: open discovery by default
    cohort_size=6,
    session_seconds=60 * 20,
    assist_model='claude-haiku-4-5',
    assist_rate_limit=10,  # max ai_* calls per participant per session
    assist_cost_ceiling_usd=0.50,  # per-participant spend cap
    diffusion_item='badge',  # Decision E: generic badge path first
    diffusion_seeds=1,
    diffusion_seed_time_s=60 * 5,
    sender_disclosure_cue=False,
    # Which oTree Room (see ROOMS below) this session's cohort is bound to,
    # so Wave-1 and Wave-2 sessions for the same cohort can be matched up
    # via the `crosswave` store. '' means "no Room" (e.g. ad-hoc/demo
    # sessions) -- formation/recontact both treat a falsy room_name as
    # "nothing to snapshot/recover".
    room_name='',
)

SESSION_CONFIGS = [
    dict(
        name='cond1_wave1',
        display_name='Condition 1 (control) -- Wave 1',
        app_sequence=['intro', 'formation', 'survey1'],
        num_demo_participants=6,
        condition=1,
        wave=1,
        room_name='room_cond1',
    ),
    dict(
        name='cond2_wave1',
        display_name='Condition 2 (AI-assist, no disclosure) -- Wave 1',
        app_sequence=['intro', 'formation', 'survey1'],
        num_demo_participants=6,
        condition=2,
        wave=1,
        room_name='room_cond2',
    ),
    dict(
        name='cond3_wave1',
        display_name='Condition 3 (AI-assist, disclosed) -- Wave 1',
        app_sequence=['intro', 'formation', 'survey1'],
        num_demo_participants=6,
        condition=3,
        wave=1,
        room_name='room_cond3',
        # Sender-side half of the cond3-vs-cond4 manipulation (spec
        # Section 7 / Decision G): cond3 senders are told their
        # AI-assisted messages will be labeled to the recipient.
        sender_disclosure_cue=True,
    ),
    dict(
        name='cond4_wave1',
        display_name='Condition 4 (AI-assist, undisclosed) -- Wave 1',
        app_sequence=['intro', 'formation', 'survey1'],
        num_demo_participants=6,
        condition=4,
        wave=1,
        room_name='room_cond4',
        # sender_disclosure_cue left at its default (False): the badge
        # never shows in cond4, so there's nothing to cue the sender about.
    ),
    dict(
        name='cond1_wave2',
        display_name='Condition 1 (control) -- Wave 2',
        app_sequence=['recontact', 'formation', 'survey2', 'debrief'],
        num_demo_participants=6,
        condition=1,
        wave=2,
        room_name='room_cond1',
    ),
    dict(
        name='cond2_wave2',
        display_name='Condition 2 (AI-assist, no disclosure) -- Wave 2',
        app_sequence=['recontact', 'formation', 'survey2', 'debrief'],
        num_demo_participants=6,
        condition=2,
        wave=2,
        room_name='room_cond2',
    ),
    dict(
        name='cond3_wave2',
        display_name='Condition 3 (AI-assist, disclosed) -- Wave 2',
        app_sequence=['recontact', 'formation', 'survey2', 'debrief'],
        num_demo_participants=6,
        condition=3,
        wave=2,
        room_name='room_cond3',
        sender_disclosure_cue=True,
    ),
    dict(
        name='cond4_wave2',
        display_name='Condition 4 (AI-assist, undisclosed) -- Wave 2',
        app_sequence=['recontact', 'formation', 'survey2', 'debrief'],
        num_demo_participants=6,
        condition=4,
        wave=2,
        room_name='room_cond4',
    ),
]

# oTree Rooms (spec Section 10): one Room per cohort/condition so the same
# real participant keeps the same participant.label across that cohort's
# Wave-1 and Wave-2 sessions. All four share one label file here since Room
# namespaces are independent -- the same label string in two different
# Rooms refers to two different people, so there's no collision.
ROOMS = [
    dict(
        name='room_cond1',
        display_name='Room -- Condition 1 (control)',
        participant_label_file='_rooms/test_labels.txt',
    ),
    dict(
        name='room_cond2',
        display_name='Room -- Condition 2 (AI-assist, no disclosure)',
        participant_label_file='_rooms/test_labels.txt',
    ),
    dict(
        name='room_cond3',
        display_name='Room -- Condition 3 (AI-assist, disclosed)',
        participant_label_file='_rooms/test_labels.txt',
    ),
    dict(
        name='room_cond4',
        display_name='Room -- Condition 4 (AI-assist, undisclosed)',
        participant_label_file='_rooms/test_labels.txt',
    ),
]

PARTICIPANT_FIELDS = [
    # set during `intro` (Wave 1) or `recontact` (Wave 2), read by
    # `formation`/`survey1`/`survey2` via participant.vars (see
    # intro/__init__.py Tutorial.before_next_page and
    # recontact/__init__.py Recontact.before_next_page).
    'handle',
    'avatar_preset',
    'interest_tags',
    'consented',
    'consented_at',
    # Wave-2 only: the participant's Wave-1 ties, as recovered from the
    # crosswave store by recontact -- not otherwise used by formation
    # (which reads live Tie rows), kept here mainly so downstream pages/
    # exports can see what recontact recovered without re-querying the
    # crosswave store.
    'prior_ties',
]
SESSION_FIELDS = []

LANGUAGE_CODE = 'en'

REAL_WORLD_CURRENCY_CODE = 'USD'
USE_POINTS = True

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = environ.get('OTREE_ADMIN_PASSWORD')

DEMO_PAGE_INTRO_HTML = """ """

SECRET_KEY = environ.get('OTREE_SECRET_KEY', '5922942480427')
