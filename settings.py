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
# Phase 1 status: only the condition-1 (control) x wave-1 config is wired up,
# because conditions 2-4 need the AI-assist (Phase 2) and disclosure
# (Phase 3) apps, and wave 2 needs the `recontact`/`survey2`/`debrief` apps
# (Phase 4/6). Adding a SESSION_CONFIGS entry for an app_sequence that
# doesn't exist yet would break devserver startup, so configs are added
# incrementally as each phase's apps land.
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
