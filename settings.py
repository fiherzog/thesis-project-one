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
# Phase 3 status: all four wave-1 conditions are now wired up (cond1 =
# control, cond2 = AI-assist/no disclosure UI, cond3 = AI-assist/disclosed,
# cond4 = AI-assist/undisclosed -- see formation app for the badge logic).
# Wave 2 still needs the `recontact`/`survey2`/`debrief` apps (Phase 4/6);
# adding a SESSION_CONFIGS entry for an app_sequence that doesn't exist yet
# would break devserver startup, so those configs are added once Phase 4
# lands.
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
)

SESSION_CONFIGS = [
    dict(
        name='cond1_wave1',
        display_name='Condition 1 (control) -- Wave 1',
        app_sequence=['intro', 'formation', 'survey1'],
        num_demo_participants=6,
        condition=1,
        wave=1,
    ),
    dict(
        name='cond2_wave1',
        display_name='Condition 2 (AI-assist, no disclosure) -- Wave 1',
        app_sequence=['intro', 'formation', 'survey1'],
        num_demo_participants=6,
        condition=2,
        wave=1,
    ),
    dict(
        name='cond3_wave1',
        display_name='Condition 3 (AI-assist, disclosed) -- Wave 1',
        app_sequence=['intro', 'formation', 'survey1'],
        num_demo_participants=6,
        condition=3,
        wave=1,
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
        # sender_disclosure_cue left at its default (False): the badge
        # never shows in cond4, so there's nothing to cue the sender about.
    ),
]

PARTICIPANT_FIELDS = [
    # set during `intro`, read by `formation`/`survey1` via participant.vars
    # (see intro/__init__.py Tutorial.before_next_page).
    'handle',
    'avatar_preset',
    'interest_tags',
    'consented',
    'consented_at',
]
SESSION_FIELDS = []

LANGUAGE_CODE = 'en'

REAL_WORLD_CURRENCY_CODE = 'USD'
USE_POINTS = True

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = environ.get('OTREE_ADMIN_PASSWORD')

DEMO_PAGE_INTRO_HTML = """ """

SECRET_KEY = environ.get('OTREE_SECRET_KEY', '5922942480427')
