import hashlib
import json
import os
import time

from otree.api import *

import crosswave
import tombstone
from deidentify import opaque_id

try:
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover
    AsyncAnthropic = None


doc = """
The formation session as a single live page (build spec Section 4).

CORE INVARIANTS (restated per phase so they don't drift):
  - This whole live session is ONE Page. Do not turn it into a page
    sequence.
  - `live_method` branches are async generators: `async def
    live_method(player, data): ... yield {...}`. A plain coroutine that
    `return`s raises LiveMethodBadReturnValue on oTree 6.0.15 -- confirmed
    in the Phase 0 spike.
  - `condition` and `wave` are read from `player.session.config`, never
    trusted from the client.
  - Every live_method branch logs an Event row before returning (spec
    Section 3: "nothing happens without an Event row").
  - This app is wave-agnostic: `WAVE = session.config['wave']` is stamped
    on every Event/Message/Tie row so the same code runs in both Wave 1
    and Wave 2 sessions (Phase 4 adds the Wave 2 session type; no
    formation code should need to change for that).

PHASE STATUS: this is the Phase 4 (diffusion + Wave 2) version. All three
conditions exist, both waves run through this same app, and the
diffusion mechanic is wired in.
  - Messaging + persistent threads: done (Phase 1).
  - Explicit tie tracking (connect / connect_remove): done (Phase 1).
    Behavioral ties (>=3 messages each way) are NOT computed live -- per
    spec Section 9 they are derived offline from the Message table at
    analysis time.
  - AI-assist (ai_draft branch, AIEvent, provenance snapshot fields on
    Message): done Phase 2. Condition gating: conditions 2-3 have
    AI-assist enabled, condition 1 (control) does not -- see
    ASSIST_ENABLED_CONDITIONS below. Valid `condition` values are exactly
    {1, 2, 3}; `creating_session` below rejects anything else at
    session-creation time.
  - Disclosure banner (ai_badge_shown, general recipient-visible cue,
    conversation-level not per-message): done Phase 3 -- see PHASE 3 NOTES
    below.
  - Diffusion mechanic (Exposure/Adoption, seeding, adopt surface): done
    this phase -- see PHASE 4 NOTES below.
  - Wave 2 (Rooms, cross-wave store, recontact app): done this phase --
    this app itself needed NO changes for Wave 2 beyond what Phase 1
    already built (it was wave-agnostic from the start, per the
    CORE INVARIANTS above); the new work lives in the `crosswave` module
    and the new `recontact`/`survey2`/`debrief` apps.
  - Export suite (custom_export_*, spec Section 13): done Phase 5 -- see
    PHASE 5 NOTES below.

PHASE 5 NOTES (export suite, spec Section 13):
  - Every function below named `custom_export_*` is picked up automatically
    by oTree's admin "Data" page (any callable starting with
    `custom_export` in an app's models module is listed as a separate
    downloadable CSV -- see `otree.export.get_custom_export_functions`).
    Since this app owns every diffusion/messaging/tie ExtraModel, most of
    the spec's six export tables live here rather than being split across
    apps.
  - `.filter()` with *no* kwargs at all is a special case oTree allows only
    for custom_export (`ExtraModel.filter`'s own comment: "this allows
    querying .filter() without any args for custom_export") -- it returns
    every row of that model ever created, across all sessions. Every
    function below uses that, then narrows to the current export's scope
    by intersecting on `players` (which oTree already scopes to one
    session when the admin exports a single session, or to everything
    when exporting globally) rather than re-deriving that filtering logic
    itself.
  - Every row is stamped with `assist_model` + `frozen_prompt_hash` (spec:
    "Stamp every export with the session config snapshot ... for
    reproducibility") so a single downloaded CSV is self-describing even
    in isolation, not just when the whole suite is exported together.
  - `custom_export_corpus` is the one de-identified table (spec: "handles
    -> opaque ids") -- it's the only export that carries `sent_text`, and
    it deliberately omits `participant_label`/`handle` in favor of
    `deidentify.opaque_id`. The other tables keep plain labels/handles
    (this is server-side research data, not a public release) but also
    include `opaque_id` alongside them so analysts can join a de-identified
    working copy against the corpus without re-deriving the hash.
  - `custom_export_edges` emits both tie definitions per spec Section 9
    ("build both definitions"): `kind='explicit'` rows come straight from
    the `Tie` table; `kind='behavioral'` rows are derived here, at export
    time, from `Message` counts (>=3 each way, per the spec's own example
    threshold) -- behavioral ties are intentionally never stored live.
  - No "authenticity_score" column (loosely gestured at as an example in
    spec Section 13's node-table bullet) is fabricated here -- its inputs
    (paste_detected, edit_distance, pct_retained, compose_time_ms) are all
    in `custom_export_messages`, and Section 6 is explicit that any
    composite dose/authenticity score is an analysis-layer computation,
    not something oTree itself should produce.

PHASE 2 NOTES (AI-assist):
  - `run_ai_draft` proxies the Anthropic API server-side only -- the API
    key never reaches the client (spec Section 5). Same graceful-degrade
    pattern validated in the Phase 0 spike: if the `anthropic` package or
    ANTHROPIC_API_KEY isn't available, the branch returns ok=False with a
    reason instead of raising.
  - Every AI call (successful or not) writes one AIEvent row (spec
    Section 3/14 cost controls) so per-participant rate limiting
    (`assist_rate_limit`) and spend ceiling (`assist_cost_ceiling_usd`)
    can be enforced by counting/summing that participant's prior AIEvent
    rows -- both checked server-side *before* calling the API.
  - `PRICING_PER_MTOK` is a rough placeholder cost table (dollars per
    million tokens) for estimating `AIEvent.cost_usd` client-session-side;
    it is NOT guaranteed to match Anthropic's current published pricing
    and should be reconciled against the billing dashboard before running
    real sessions with a spend cap that matters.
  - Provenance snapshot on Message: when the participant sends a message,
    the client includes whatever AI draft text (if any) was last shown to
    them for that compose action. The server diffs the sent text against
    that draft to classify provenance/acceptance and compute edit_distance
    / pct_retained -- see `compute_provenance`.

PHASE 3 NOTES (disclosure badge, spec Section 7):
  - The disclosure is a general, conversation-level notice ("AI may be
    used to help write messages"), not a per-message tag -- it does NOT
    depend on whether any particular message actually used AI-assist, or
    even on whether AI-assist was used at all in that thread. It is
    presentation-only and recomputed server-side, every time, from
    `condition` alone -- the client is never trusted to say whether a
    recipient should see it. `ai_badge_for(recipient)` is the single
    source of truth: True iff `recipient`'s own condition is 3
    (disclosed); condition 2 (available/unlabeled, no disclosure UI at
    all) never shows it, and it is always True for every condition-3
    recipient regardless of the peer or the message's provenance.
  - `Message.ai_badge_shown` is still stamped on every message at push
    time (in the 'send' branch) so the export layer has a per-row record
    of whether the general banner was active for that message's
    recipient -- it is a recipient-level constant within a session (every
    message to the same condition-3 recipient stores the same value), not
    a per-message decision, but keeping it on the row avoids re-deriving
    it from `condition` at analysis time.
  - The condition-2-vs-3 manipulation also has a sender-side component
    (spec Decision G / Section 7): does the sender know in advance that
    their AI-assisted messages will be labeled to the recipient? That is
    carried by the `sender_disclosure_cue` boolean session-config flag
    (see settings.py; True for condition 3 only, False elsewhere), passed
    to the template via vars_for_template and rendered as a single line
    of copy near the AI-assist button -- kept deliberately minimal per the
    spec's "keep the difference minimal" instruction, since the cue itself
    is not supposed to be a second, uncontrolled manipulation.

PHASE 4 NOTES (diffusion mechanic, spec Section 8):
  - Seeding: at `diffusion_seed_time_s` into the session, `diffusion_seeds`
    players (chosen deterministically by lowest id_in_group, simplest
    reproducible rule) are marked adopted. There's no background task
    runner in this build, so -- per the spec's own suggestion -- seeding
    is triggered by a timestamp check that runs on every live_method call
    and fires (once) on whichever call happens to land after the seed
    time. The session "start" reference and a one-shot "already seeded"
    flag both live in `session.vars` (in-memory, session-scoped, and
    exactly the kind of shared-across-players state `session.vars` is
    for -- unlike `participant.vars`, which is per-participant only).
  - Exposure counting: an Exposure row is the record of ego having been
    shown an already-adopted alter. Logged from two places, per spec:
    server-side, automatically, whenever an adopted alter is pushed into
    ego's `directory` listing or an `incoming` message payload; and
    client-side, via an explicit `seen` event the client can send for
    anything else it rendered (e.g. scrolling a thread history that
    contains an adopted alter). Both paths call the same
    `log_exposure_if_new`, which de-duplicates by (ego, alter) so
    `Adoption.exposure_count_at_adoption` counts *distinct* adopted
    neighbors, not raw impressions -- that de-duplication is what makes
    the exposure-threshold distribution meaningful (spec: "the
    simple-vs-complex signal").
  - Adoption: the `adopt` live_method branch (`handle_adoption`) snapshots
    every alter ego has been exposed to at the moment of adoption into
    `Adoption.adopted_neighbor_ids` (JSON list) and its count into
    `exposure_count_at_adoption`. This build implements the generic
    "badge" diffusion item (spec: "build the generic badge path first");
    the linguistic-marker variant is an explicitly-flagged add-on, not
    built here.
  - This build does NOT re-seed or otherwise touch diffusion state in
    Wave 2 -- seeding is a Wave-1-only concept per the spec's framing of
    diffusion as something that plays out *during* a session; Wave 2's
    `formation` page will simply carry over whatever Adoption rows a
    returning participant already has (Adoption is keyed by the *player*
    row for a given round, so a Wave-2 session's fresh Player rows start
    with no Adoption row of their own -- diffusion in Wave 2, if wanted,
    would need its own seed/adopt cycle, which is out of scope here
    unless the study design calls for it).
"""


class C(BaseConstants):
    NAME_IN_URL = 'formation'
    PLAYERS_PER_GROUP = None  # one group spanning every player in the session
    NUM_ROUNDS = 1

    # Conditions with AI-assist turned on. Valid `condition` values are
    # exactly {1, 2, 3} -- see VALID_CONDITIONS / creating_session below.
    ASSIST_ENABLED_CONDITIONS = {2, 3}

    VALID_CONDITIONS = {1, 2, 3}

    # Frozen system prompt (spec Section 5/14: identical on every call so
    # it's eligible for prompt caching). Placeholder wording -- swap in the
    # study's real instructions before running actual sessions.
    FROZEN_SYSTEM_PROMPT = (
        "You are helping a participant in a social-science study draft a "
        "short, casual chat message to another participant they are just "
        "getting to know. Keep it under 40 words, warm, and low-key. "
        "Return only the message text, no preamble."
    )

    # Rough, approximate USD-per-million-token pricing used only to
    # estimate AIEvent.cost_usd for the spend-ceiling check. NOT guaranteed
    # to match Anthropic's current published pricing -- reconcile against
    # the billing dashboard before relying on this for a real spend cap.
    PRICING_PER_MTOK = {
        'claude-haiku-4-5': {'input': 1.00, 'output': 5.00},
        'claude-sonnet-4-5': {'input': 3.00, 'output': 15.00},
    }


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    pass


def creating_session(subsession):
    """Server-side gate on `condition` (spec invariant: valid values are
    exactly {1, 2, 3}). SESSION_CONFIGS in settings.py only ever defines
    those three, but that's just the known-good set -- a hand-edited
    config or a scripted call to oTree's REST /api/sessions endpoint could
    still pass anything, so this is enforced here too, at the point oTree
    actually creates the session, rather than trusted to stay in sync with
    settings.py."""
    condition = subsession.session.config.get('condition')
    if condition not in C.VALID_CONDITIONS:
        raise ValueError(
            f"Invalid condition {condition!r}: must be one of {sorted(C.VALID_CONDITIONS)}."
        )


# ---------------------------------------------------------------------------
# Append-only data spine (spec Section 3).
# ---------------------------------------------------------------------------


class Event(ExtraModel):
    player = models.Link(Player)
    wave = models.IntegerField()
    actor_id = models.StringField()
    target_id = models.StringField()  # optional, '' if not applicable
    type = models.StringField()
    payload = models.LongStringField()
    ts = models.FloatField()


class Message(ExtraModel):
    player = models.Link(Player)  # sender
    wave = models.IntegerField()
    recipient_id = models.StringField()
    thread_id = models.StringField()
    ordinal_in_thread = models.IntegerField()
    sent_text = models.LongStringField()
    char_count = models.IntegerField()
    word_count = models.IntegerField()
    token_count = models.IntegerField()
    # AI-assist provenance snapshot -- populated starting Phase 2. Phase 1
    # always writes provenance='human_only' since no AI exists yet.
    provenance = models.StringField()
    ai_output_final = models.LongStringField()
    edit_distance = models.IntegerField()
    pct_retained = models.FloatField()
    acceptance = models.StringField()
    compose_time_ms = models.IntegerField()
    paste_detected = models.BooleanField()
    # spec Section 7; see ai_badge_for() -- a recipient-level constant (was
    # the general AI-disclosure banner active for this message's
    # recipient?), not a per-message decision.
    ai_badge_shown = models.BooleanField()
    ts = models.FloatField()


class Tie(ExtraModel):
    player = models.Link(Player)  # src
    dst_id = models.StringField()
    kind = models.StringField()  # explicit | behavioral
    formed_wave = models.IntegerField()
    active_wave1 = models.BooleanField()
    active_wave2 = models.BooleanField()
    removed_at = models.FloatField(initial=0)


class AIEvent(ExtraModel):
    """One row per AI-assist call (Phase 2), success or failure. This is
    the basis for per-participant rate limiting and spend-ceiling
    enforcement (assist_rate_limit / assist_cost_ceiling_usd), and is
    linked (loosely, via thread_id) to whichever Message eventually gets
    sent using this draft, if any."""

    player = models.Link(Player)  # requester
    wave = models.IntegerField()
    thread_id = models.StringField()
    instruction = models.LongStringField()
    model = models.StringField()
    ai_output = models.LongStringField()
    input_tokens = models.IntegerField()
    output_tokens = models.IntegerField()
    cost_usd = models.FloatField()
    latency_ms = models.IntegerField()
    ok = models.BooleanField()
    error = models.StringField()
    ts = models.FloatField()


class Exposure(ExtraModel):
    """Diffusion mechanic (spec Section 8): one row per (ego, alter) the
    first time ego is shown alter in an already-adopted state. De-duplicated
    at write time (see log_exposure_if_new) so COUNT(*) per player already
    equals "distinct adopted neighbors ego has been exposed to"."""

    player = models.Link(Player)  # ego (the one exposed)
    alter_id = models.StringField()  # an already-adopted contact seen
    ts = models.FloatField()


class Adoption(ExtraModel):
    player = models.Link(Player)
    item = models.StringField()
    exposure_count_at_adoption = models.IntegerField()
    adopted_neighbor_ids = models.LongStringField()  # JSON list
    ts = models.FloatField()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_ms():
    return time.time() * 1000


def get_wave(player: Player) -> int:
    return player.session.config['wave']


def player_handle(player: Player) -> str:
    return player.participant.vars.get('handle') or f'P{player.id_in_group}'


def thread_id_for(a: int, b: int) -> str:
    lo, hi = sorted([a, b])
    return f'{lo}-{hi}'


def log_event(player: Player, type_: str, target_id='', payload=None):
    Event.create(
        player=player,
        wave=get_wave(player),
        actor_id=str(player.id_in_group),
        target_id=str(target_id) if target_id != '' else '',
        type=type_,
        payload=json.dumps(payload or {}),
        ts=now_ms(),
    )


def is_adopted(player: Player) -> bool:
    return len(Adoption.filter(player=player)) > 0


def log_exposure_if_new(ego: Player, alter_id) -> None:
    """Spec Section 8: an Exposure row means ego has now been shown this
    (already-adopted) alter at least once. De-duplicated here by (ego,
    alter) so downstream COUNT(*) is "distinct adopted neighbors", not raw
    impressions."""
    alter_id = str(alter_id)
    already = {e.alter_id for e in Exposure.filter(player=ego)}
    if alter_id not in already:
        Exposure.create(player=ego, alter_id=alter_id, ts=now_ms())


def maybe_seed_diffusion(player: Player):
    """Spec Section 8 seeding, triggered by a timestamp check on every
    live_method call (no background task runner in this build -- the spec
    explicitly allows this: "fires on the next event after the seed
    time"). Session-wide start reference and one-shot flag live in
    `session.vars`, which (unlike `participant.vars`) is shared across all
    players in the session -- exactly the scope this needs."""
    session = player.session
    started_at = session.vars.setdefault('diffusion_started_at', now_ms())
    if session.vars.get('diffusion_seeded'):
        return
    elapsed_s = (now_ms() - started_at) / 1000
    if elapsed_s < session.config['diffusion_seed_time_s']:
        return
    session.vars['diffusion_seeded'] = True
    k = session.config['diffusion_seeds']
    item = session.config['diffusion_item']
    candidates = sorted(player.group.get_players(), key=lambda p: p.id_in_group)[:k]
    seeded_ids = []
    for p in candidates:
        if not is_adopted(p):
            Adoption.create(
                player=p, item=item, exposure_count_at_adoption=0,
                adopted_neighbor_ids='[]', ts=now_ms(),
            )
        seeded_ids.append(p.id_in_group)
    log_event(player, 'diffusion_seeded', payload={'seeded_ids': seeded_ids, 'item': item})


def handle_adoption(player: Player, data: dict):
    if is_adopted(player):
        return {'type': 'adopt_ack', 'already_adopted': True}
    item = player.session.config['diffusion_item']
    adopted_neighbor_ids = sorted(
        {e.alter_id for e in Exposure.filter(player=player)}, key=int
    )
    Adoption.create(
        player=player, item=item,
        exposure_count_at_adoption=len(adopted_neighbor_ids),
        adopted_neighbor_ids=json.dumps(adopted_neighbor_ids),
        ts=now_ms(),
    )
    log_event(player, 'adopted', payload={'exposure_count_at_adoption': len(adopted_neighbor_ids)})
    return {
        'type': 'adopt_ack',
        'already_adopted': False,
        'exposure_count_at_adoption': len(adopted_neighbor_ids),
    }


def build_directory(player: Player):
    others = player.get_others_in_group()
    connected_dst_ids = {
        t.dst_id
        for t in Tie.filter(player=player)
        if t.kind == 'explicit' and t.removed_at == 0
    }
    rows = []
    for p in others:
        adopted = is_adopted(p)
        if adopted:
            # Server-side exposure logging (spec Section 8): pushing an
            # adopted alter into ego's directory counts as an exposure.
            log_exposure_if_new(player, p.id_in_group)
        rows.append({
            'id': p.id_in_group,
            'handle': player_handle(p),
            'connected': str(p.id_in_group) in connected_dst_ids,
            'adopted': adopted,
        })
    return {'type': 'directory', 'players': rows}


def load_thread(player: Player, peer_id: int):
    tid = thread_id_for(player.id_in_group, peer_id)
    peer = player.group.get_player_by_id(peer_id)
    # Message.player is the sender, so a 2-party thread is the union of
    # messages sent by either side. ExtraModel.filter() requires at least
    # one kwarg to be a model instance, hence the two separate calls
    # rather than a single OR query.
    rows = list(Message.filter(player=player)) + list(Message.filter(player=peer))
    matching = sorted(
        [m for m in rows if m.thread_id == tid], key=lambda m: m.ts
    )
    # Reloading a thread that contains a message from an already-adopted
    # peer counts as an exposure too (spec Section 8: "in a thread").
    if is_adopted(peer):
        log_exposure_if_new(player, peer.id_in_group)
    peer_adopted = is_adopted(peer)
    return {
        'type': 'thread_history',
        'peer': peer_id,
        'peer_adopted': peer_adopted,
        # General, conversation-level disclosure (spec Section 7): based
        # only on the viewer's (player's) own condition, not on any
        # message's provenance -- see ai_badge_for and PHASE 3 NOTES.
        'ai_disclosure_banner': ai_badge_for(player),
        'messages': [
            {
                'sender': m.player.id_in_group,
                'body': m.sent_text,
                'ts': m.ts,
            }
            for m in matching
        ],
    }


def assist_enabled(player: Player) -> bool:
    return player.session.config['condition'] in C.ASSIST_ENABLED_CONDITIONS


def ai_badge_for(recipient: Player) -> bool:
    """Spec Section 7: presentation-only, recomputed server-side from
    `condition` alone, every time -- never trust the client for this.
    This is a general conversation-level disclosure ("AI may be used to
    help write messages here"), not a per-message label: it is True for
    every condition-3 recipient regardless of the peer or of whether any
    particular message (or any message at all) was actually ai_assisted."""
    return recipient.session.config['condition'] == 3


def ai_calls_used(player: Player) -> int:
    return len(AIEvent.filter(player=player))


def ai_cost_spent(player: Player) -> float:
    return sum(e.cost_usd for e in AIEvent.filter(player=player))


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = C.PRICING_PER_MTOK.get(model)
    if not rates:
        return 0.0
    return (input_tokens * rates['input'] + output_tokens * rates['output']) / 1_000_000


def _levenshtein(a: str, b: str) -> int:
    """Standard O(len(a)*len(b)) edit-distance DP. Fine for short chat
    messages; not meant for large documents."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,       # deletion
                curr[j - 1] + 1,   # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[-1]


def compute_provenance(sent_text: str, ai_draft_text: str):
    """Classify a sent message's provenance/acceptance against the AI
    draft (if any) that was last shown to the sender for this compose
    action. `ai_draft_text` is '' if the participant never requested (or
    this condition doesn't offer) AI-assist for this message.
    """
    if not ai_draft_text:
        return {
            'provenance': 'human_only',
            'ai_output_final': '',
            'edit_distance': 0,
            'pct_retained': 0.0,
            'acceptance': 'n/a',
        }
    dist = _levenshtein(sent_text, ai_draft_text)
    longest = max(len(sent_text), len(ai_draft_text), 1)
    pct_retained = round(100.0 * (1 - dist / longest), 2)
    acceptance = 'verbatim' if sent_text == ai_draft_text else 'edited'
    return {
        'provenance': 'ai_assisted',
        'ai_output_final': ai_draft_text,
        'edit_distance': dist,
        'pct_retained': pct_retained,
        'acceptance': acceptance,
    }


async def run_ai_draft(player: Player, data: dict):
    """Async live_method branch that proxies the Anthropic API server-side.
    The API key never reaches the client (spec Section 5). Enforces
    per-participant rate limit and spend ceiling *before* calling the API,
    and always writes one AIEvent row (success or failure) so those checks
    stay accurate across calls."""
    thread_id = thread_id_for(player.id_in_group, int(data.get('peer', 0)))
    instruction = data.get('instruction') or 'Draft a friendly opening message.'
    session = player.session
    model = session.config['assist_model']

    if not assist_enabled(player):
        return {'type': 'ai_result', 'ok': False, 'text': None,
                'error': 'AI-assist is not enabled for this condition.'}

    rate_limit = session.config['assist_rate_limit']
    if ai_calls_used(player) >= rate_limit:
        return {'type': 'ai_result', 'ok': False, 'text': None,
                'error': f'AI-assist rate limit reached ({rate_limit} calls per session).'}

    cost_ceiling = session.config['assist_cost_ceiling_usd']
    if ai_cost_spent(player) >= cost_ceiling:
        return {'type': 'ai_result', 'ok': False, 'text': None,
                'error': 'AI-assist spend ceiling reached for this participant.'}

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if AsyncAnthropic is None or not api_key:
        reason = 'anthropic package not installed' if AsyncAnthropic is None else 'ANTHROPIC_API_KEY not set'
        AIEvent.create(
            player=player, wave=get_wave(player), thread_id=thread_id,
            instruction=instruction, model=model, ai_output='',
            input_tokens=0, output_tokens=0, cost_usd=0.0, latency_ms=0,
            ok=False, error=reason, ts=now_ms(),
        )
        return {'type': 'ai_result', 'ok': False, 'text': None,
                'error': f'AI-assist unavailable: {reason}.'}

    client = AsyncAnthropic(api_key=api_key)
    t0 = time.time()
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=200,
            system=[{
                'type': 'text',
                'text': C.FROZEN_SYSTEM_PROMPT,
                'cache_control': {'type': 'ephemeral'},
            }],
            messages=[{'role': 'user', 'content': instruction}],
        )
        text = resp.content[0].text
        latency_ms = int((time.time() - t0) * 1000)
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cost = estimate_cost_usd(model, in_tok, out_tok)
        AIEvent.create(
            player=player, wave=get_wave(player), thread_id=thread_id,
            instruction=instruction, model=model, ai_output=text,
            input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
            latency_ms=latency_ms, ok=True, error='', ts=now_ms(),
        )
        return {'type': 'ai_result', 'ok': True, 'text': text}
    except Exception as exc:
        # Logged (not raised) so a transient API error doesn't take down the
        # participant's websocket connection; a real build should classify
        # these rather than stringify raw exceptions.
        AIEvent.create(
            player=player, wave=get_wave(player), thread_id=thread_id,
            instruction=instruction, model=model, ai_output='',
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            latency_ms=int((time.time() - t0) * 1000),
            ok=False, error=str(exc), ts=now_ms(),
        )
        return {'type': 'ai_result', 'ok': False, 'text': None, 'error': str(exc)}


def record_message(player: Player, data: dict) -> Message:
    peer_id = int(data['peer'])
    tid = thread_id_for(player.id_in_group, peer_id)
    ordinal = 1 + sum(
        1
        for p in player.group.get_players()
        for m in Message.filter(player=p)
        if m.thread_id == tid
    )
    text = data['body']
    prov = compute_provenance(text, data.get('ai_draft_text') or '')
    msg = Message.create(
        player=player,
        wave=get_wave(player),
        recipient_id=str(peer_id),
        thread_id=tid,
        ordinal_in_thread=ordinal,
        sent_text=text,
        char_count=len(text),
        word_count=len(text.split()),
        token_count=len(text.split()),  # naive placeholder tokenizer
        provenance=prov['provenance'],
        ai_output_final=prov['ai_output_final'],
        edit_distance=prov['edit_distance'],
        pct_retained=prov['pct_retained'],
        acceptance=prov['acceptance'],
        compose_time_ms=int(data.get('compose_time_ms', 0)),
        paste_detected=bool(data.get('paste_detected', False)),
        # Placeholder -- immediately overwritten by the 'send' live_method
        # branch, which computes the real value via ai_badge_for() once
        # the recipient (and thus their condition) is known.
        ai_badge_shown=False,
        ts=now_ms(),
    )
    log_event(player, 'message_sent', target_id=peer_id, payload={'len': len(text), 'provenance': prov['provenance']})
    return msg


def handle_connect(player: Player, data: dict):
    peer_id = int(data['peer'])
    existing = [
        t for t in Tie.filter(player=player)
        if t.dst_id == str(peer_id) and t.kind == 'explicit'
    ]
    wave = get_wave(player)
    if not existing:
        Tie.create(
            player=player,
            dst_id=str(peer_id),
            kind='explicit',
            formed_wave=wave,
            active_wave1=(wave == 1),
            active_wave2=(wave == 2),
            removed_at=0,
        )
    log_event(player, 'connect_request', target_id=peer_id)
    return {'type': 'connect_ack', 'peer': peer_id, 'connected': True}


def handle_connect_remove(player: Player, data: dict):
    peer_id = int(data['peer'])
    wave = get_wave(player)
    for t in Tie.filter(player=player):
        if t.dst_id == str(peer_id) and t.kind == 'explicit' and t.removed_at == 0:
            t.removed_at = now_ms()
            if wave == 1:
                t.active_wave1 = False
            else:
                t.active_wave2 = False
    log_event(player, 'connect_remove', target_id=peer_id)
    return {'type': 'connect_ack', 'peer': peer_id, 'connected': False}


# ---------------------------------------------------------------------------
# The single live page (spec Section 4)
# ---------------------------------------------------------------------------


class Formation(Page):
    @staticmethod
    def get_timeout_seconds(player: Player):
        return player.session.config['session_seconds']

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        # Cross-wave snapshot (spec Section 10, Phase 4b): only Wave-1
        # needs to snapshot anything -- Wave-2's formation page is the end
        # of the line for this study, there's no Wave-3 to hand off to.
        if get_wave(player) != 1:
            return
        room_name = player.session.config.get('room_name')
        label = player.participant.label
        # No Room configured (e.g. ad-hoc/demo sessions not run through a
        # Room) means there's no stable cross-wave identity to key on --
        # nothing to snapshot, and recontact will fall back to its own
        # no-prior-snapshot path.
        if not room_name or not label:
            return
        # Tie rows are directed edges (Tie.player is whoever clicked
        # Connect -- see handle_connect / survey1's
        # first_connected_partner_handle for the same issue there): a
        # participant who only *received* a connect request has no
        # outgoing Tie of their own. Check both directions so the
        # snapshot doesn't silently drop the recipient's half of a tie.
        peer_ids = set()
        for t in Tie.filter(player=player):
            if t.kind == 'explicit' and t.active_wave1:
                peer_ids.add(int(t.dst_id))
        my_id_str = str(player.id_in_group)
        for other in player.get_others_in_group():
            for t in Tie.filter(player=other):
                if t.kind == 'explicit' and t.active_wave1 and t.dst_id == my_id_str:
                    peer_ids.add(other.id_in_group)
        ties = []
        for peer_id in peer_ids:
            peer = player.group.get_player_by_id(peer_id)
            # Keyed by the peer's participant.label, not id_in_group --
            # id_in_group is only stable within a single session's Group
            # and means nothing once Wave-2 forms new Groups.
            ties.append({
                'peer_label': peer.participant.label,
                'peer_handle': player_handle(peer),
            })
        crosswave.snapshot_wave1(room_name, label, {
            'handle': player_handle(player),
            # Carried over so Wave 2's recontact app can skip re-asking for
            # a profile entirely when a snapshot exists -- these live in
            # participant.vars (set by intro/Tutorial), not on this app's
            # own Player model, and participant.vars does not itself cross
            # sessions (that's the whole reason this store exists).
            'avatar_preset': player.participant.vars.get('avatar_preset'),
            'interest_tags': player.participant.vars.get('interest_tags'),
            'ties': ties,
            'adopted': is_adopted(player),
            'diffusion_item': player.session.config['diffusion_item'],
            'ts': now_ms(),
        })

    @staticmethod
    def vars_for_template(player: Player):
        return {
            'my_handle': player_handle(player),
            'assist_enabled': assist_enabled(player),
            # Sender-side half of the condition-2-vs-3 manipulation (spec
            # Section 7 / Decision G): whether the sender is told in
            # advance that their recipient will see the general
            # AI-disclosure banner (not that any specific message of
            # theirs will be labeled -- the banner isn't per-message).
            'sender_disclosure_cue': player.session.config['sender_disclosure_cue'],
            # General, conversation-level disclosure (spec Section 7): does
            # *this* player (as a recipient) see the "AI may be used to
            # help write messages" banner? True iff their own condition is
            # 3, regardless of peer or actual AI usage -- see ai_badge_for.
            'ai_disclosure_banner': ai_badge_for(player),
            # Diffusion mechanic (spec Section 8): the client only needs to
            # know the item's display name and whether *this* player has
            # already adopted it (to decide whether to show an Adopt
            # button at all).
            'diffusion_item': player.session.config['diffusion_item'],
            'my_adopted': is_adopted(player),
            'idle_threshold_ms': player.session.config['idle_threshold_ms'],
        }

    @staticmethod
    async def live_method(player: Player, data: dict):
        t = data.get('type')

        # Diffusion seeding (spec Section 8): no background task runner in
        # this build, so seeding is triggered by a timestamp check on every
        # live_method call -- see maybe_seed_diffusion() for why this is
        # safe (session.vars-gated, one-shot, and the spec explicitly
        # allows firing "on the next event after the seed time").
        maybe_seed_diffusion(player)

        if t == 'directory':
            log_event(player, 'directory_viewed')
            yield {player.id_in_group: build_directory(player)}
            return

        if t == 'open_thread':
            peer_id = int(data['peer'])
            log_event(player, 'open_thread', target_id=peer_id)
            yield {player.id_in_group: load_thread(player, peer_id)}
            return

        if t == 'send':
            msg = record_message(player, data)
            recipient = player.group.get_player_by_id(int(msg.recipient_id))
            # Disclosure banner (spec Section 7): a general, conversation-
            # level notice computed from the recipient's own condition
            # alone (not this message's provenance) -- persisted on the
            # Message row for the export layer, but not itself a
            # per-message decision (see PHASE 3 NOTES).
            badge = ai_badge_for(recipient)
            msg.ai_badge_shown = badge
            # Diffusion mechanic (spec Section 8): if the sender has
            # already adopted the item, receiving a message from them is
            # itself an exposure for the recipient -- log it and tell the
            # recipient's client so it can render the sender as adopted.
            sender_adopted = is_adopted(player)
            if sender_adopted:
                log_exposure_if_new(recipient, player.id_in_group)
            payload = {
                'type': 'incoming',
                'peer': player.id_in_group,
                'body': msg.sent_text,
                'ts': msg.ts,
                'adopted': sender_adopted,
            }
            ack = {
                'type': 'ack',
                'peer': int(msg.recipient_id),
                'body': msg.sent_text,
                'ts': msg.ts,
            }
            yield {
                recipient.id_in_group: payload,
                player.id_in_group: ack,
            }
            return

        if t == 'connect':
            result = handle_connect(player, data)
            yield {player.id_in_group: result}
            return

        if t == 'connect_remove':
            result = handle_connect_remove(player, data)
            yield {player.id_in_group: result}
            return

        if t == 'ai_draft':
            log_event(player, 'ai_draft_requested', target_id=data.get('peer', ''), payload={'instruction': data.get('instruction', '')})
            result = await run_ai_draft(player, data)
            yield {player.id_in_group: result}
            return

        if t == 'seen':
            # Client-side exposure capture (spec Section 8): the client
            # reports which already-adopted directory/thread rows it just
            # rendered on screen. This complements the server-side
            # exposure logging already done in build_directory() /
            # load_thread() / the 'send' branch above, catching cases
            # where an adopted peer's row was already cached client-side
            # and re-rendered without a fresh server round-trip.
            for raw_id in data.get('seen_ids', []):
                try:
                    seen_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                peer = player.group.get_player_by_id(seen_id)
                if is_adopted(peer):
                    log_exposure_if_new(player, seen_id)
            log_event(player, 'seen_reported', payload={'seen_ids': data.get('seen_ids', [])})
            yield {player.id_in_group: {'type': 'seen_ack'}}
            return

        if t == 'adopt':
            result = handle_adoption(player, data)
            yield {player.id_in_group: result}
            return

        if t == 'idle':
            # Idle detection (spec Section 12 integrity flags): the client
            # reports one event per continuous idle period once it crosses
            # `idle_threshold_ms` (see Formation.html's activity timer) --
            # a covariate/exclusion flag for analysis, never a blocker.
            log_event(player, 'idle_detected', payload={'idle_ms': data.get('idle_ms', 0)})
            yield {player.id_in_group: {'type': 'idle_ack'}}
            return

        log_event(player, 'unknown_event', payload=data)
        yield {player.id_in_group: {'type': 'error', 'message': f'Unknown event type: {t}'}}


page_sequence = [Formation]


# ---------------------------------------------------------------------------
# Custom export suite (spec Section 13, Phase 5). See PHASE 5 NOTES in the
# module docstring above for the shared design decisions.
# ---------------------------------------------------------------------------


def _frozen_prompt_hash() -> str:
    return hashlib.sha256(C.FROZEN_SYSTEM_PROMPT.encode('utf-8')).hexdigest()[:12]


def _config_stamp(session):
    cfg = session.config
    return [
        session.code,
        cfg.get('room_name', ''),
        cfg.get('condition'),
        cfg.get('wave'),
        cfg.get('assist_model', ''),
        _frozen_prompt_hash(),
    ]


_CONFIG_STAMP_HEADER = [
    'session_code', 'room_name', 'condition', 'wave', 'assist_model', 'frozen_prompt_hash',
]


def custom_export_nodes(players):
    """Node attribute table (spec Section 13): one row per participant per
    session. Adoption/exposure/messaging/tie counts are all derived here
    from the other ExtraModels rather than stored redundantly anywhere."""
    yield _CONFIG_STAMP_HEADER + [
        'participant_label', 'opaque_id', 'handle', 'avatar_preset', 'interest_tags',
        'consented', 'consented_at', 'honor_check', 'n_messages_sent', 'n_messages_received',
        'n_explicit_ties', 'adopted', 'adoption_ts', 'exposure_count_at_adoption',
        'n_exposures_total', 'ai_calls_used', 'ai_cost_spent',
        'met_min_interaction', 'idle_events_n', 'idle_ms_total',
    ]

    excluded_ids, excluded_idg = tombstone.excluded_keys(players)
    player_ids = {p.id for p in players if p.id not in excluded_ids}
    messages = [m for m in Message.filter() if m.player_id in player_ids]
    ties = [t for t in Tie.filter() if t.player_id in player_ids]
    exposures = [e for e in Exposure.filter() if e.player_id in player_ids]
    adoptions = {a.player_id: a for a in Adoption.filter() if a.player_id in player_ids}
    ai_events = [e for e in AIEvent.filter() if e.player_id in player_ids]
    # Idle detection (spec Section 12 integrity flags): aggregate from the
    # generic Event log rather than a dedicated ExtraModel, since it's just
    # a count + total duration, not a rich per-event record.
    idle_events = [
        e for e in Event.filter() if e.player_id in player_ids and e.type == 'idle_detected'
    ]

    sent_by_pid = {}
    for m in messages:
        sent_by_pid.setdefault(m.player_id, []).append(m)
    # "received" is keyed by (session_id, id_in_group) since recipient_id is
    # only an id_in_group string, not a player.id.
    received_count = {}
    for m in messages:
        key = (m.player.session_id, m.recipient_id)
        received_count[key] = received_count.get(key, 0) + 1
    ties_by_pid = {}
    for t in ties:
        ties_by_pid.setdefault(t.player_id, []).append(t)
    exposures_by_pid = {}
    for e in exposures:
        exposures_by_pid.setdefault(e.player_id, []).append(e)
    ai_cost_by_pid = {}
    ai_calls_by_pid = {}
    for e in ai_events:
        ai_calls_by_pid[e.player_id] = ai_calls_by_pid.get(e.player_id, 0) + 1
        ai_cost_by_pid[e.player_id] = ai_cost_by_pid.get(e.player_id, 0.0) + e.cost_usd
    idle_events_by_pid = {}
    idle_ms_by_pid = {}
    for e in idle_events:
        idle_events_by_pid[e.player_id] = idle_events_by_pid.get(e.player_id, 0) + 1
        idle_ms_by_pid[e.player_id] = idle_ms_by_pid.get(e.player_id, 0) + json.loads(e.payload).get('idle_ms', 0)

    for p in players:
        if p.id in excluded_ids:
            continue
        session = p.session
        participant = p.participant
        label = participant.label or ''
        n_explicit_ties = len({
            t.dst_id for t in ties_by_pid.get(p.id, [])
            if t.kind == 'explicit' and t.removed_at == 0
        })
        adoption = adoptions.get(p.id)
        n_messages_sent = len(sent_by_pid.get(p.id, []))
        yield _config_stamp(session) + [
            label,
            opaque_id(session.code, label or participant.code),
            player_handle(p),
            participant.vars.get('avatar_preset'),
            participant.vars.get('interest_tags'),
            participant.vars.get('consented'),
            participant.vars.get('consented_at'),
            participant.vars.get('honor_check'),
            n_messages_sent,
            received_count.get((p.session_id, str(p.id_in_group)), 0),
            n_explicit_ties,
            adoption is not None,
            adoption.ts if adoption else '',
            adoption.exposure_count_at_adoption if adoption else '',
            len(exposures_by_pid.get(p.id, [])),
            ai_calls_by_pid.get(p.id, 0),
            round(ai_cost_by_pid.get(p.id, 0.0), 4),
            # Minimum-interaction gate (spec Section 12): a covariate flag
            # for payment decisions, not an automatic blocker -- the
            # researcher decides what to do with a False row.
            n_messages_sent >= session.config['min_interaction_messages'],
            idle_events_by_pid.get(p.id, 0),
            idle_ms_by_pid.get(p.id, 0),
        ]


def custom_export_edges(players):
    """Edge list (spec Section 13 + Section 9 'build both definitions'):
    explicit ties come straight from the Tie table; behavioral ties (>=3
    messages each way) are derived here at export time from Message counts
    and never stored live."""
    yield _CONFIG_STAMP_HEADER + [
        'kind', 'src_label', 'dst_label', 'weight', 'formed_wave',
        'active_wave1', 'active_wave2', 'removed_at',
    ]

    excluded_ids, excluded_idg = tombstone.excluded_keys(players)
    player_ids = {p.id for p in players if p.id not in excluded_ids}
    by_session_and_idg = {(p.session_id, p.id_in_group): p for p in players}
    ties = [t for t in Tie.filter() if t.player_id in player_ids]
    messages = [m for m in Message.filter() if m.player_id in player_ids]

    for t in ties:
        if t.kind != 'explicit':
            continue
        src = t.player
        if (src.session_id, int(t.dst_id)) in excluded_idg:
            continue
        dst = by_session_and_idg.get((src.session_id, int(t.dst_id)))
        if dst is None:
            continue
        session = src.session
        yield _config_stamp(session) + [
            'explicit',
            src.participant.label or '',
            dst.participant.label or '',
            1,
            t.formed_wave,
            t.active_wave1,
            t.active_wave2,
            t.removed_at,
        ]

    # Behavioral ties: derive per session, per unordered pair, from message
    # counts in each direction (spec: "e.g. >=3 each way").
    msgs_by_session = {}
    for m in messages:
        msgs_by_session.setdefault(m.player.session_id, []).append(m)
    for session_id, session_messages in msgs_by_session.items():
        counts = {}  # (src_idg, dst_idg) -> count
        for m in session_messages:
            src_idg = m.player.id_in_group
            dst_idg = int(m.recipient_id)
            counts[(src_idg, dst_idg)] = counts.get((src_idg, dst_idg), 0) + 1
        seen_pairs = set()
        for (a_idg, b_idg) in counts:
            pair = tuple(sorted((a_idg, b_idg)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if (session_id, pair[0]) in excluded_idg or (session_id, pair[1]) in excluded_idg:
                continue
            a_to_b = counts.get((pair[0], pair[1]), 0)
            b_to_a = counts.get((pair[1], pair[0]), 0)
            if a_to_b >= 3 and b_to_a >= 3:
                a_player = by_session_and_idg.get((session_id, pair[0]))
                b_player = by_session_and_idg.get((session_id, pair[1]))
                if a_player is None or b_player is None:
                    continue
                session = a_player.session
                yield _config_stamp(session) + [
                    'behavioral',
                    a_player.participant.label or '',
                    b_player.participant.label or '',
                    a_to_b + b_to_a,
                    '', '', '', '',
                ]


def custom_export_messages(players):
    """Message table (spec Section 13): provenance/counts/pct_retained/
    acceptance/ordinal -- deliberately WITHOUT sent_text, which lives only
    in the de-identified `custom_export_corpus` table below."""
    yield _CONFIG_STAMP_HEADER + [
        'message_id', 'thread_id', 'ordinal_in_thread', 'sender_label',
        'sender_opaque_id', 'recipient_label', 'recipient_opaque_id',
        'char_count', 'word_count', 'token_count', 'provenance',
        'edit_distance', 'pct_retained', 'acceptance', 'compose_time_ms',
        'paste_detected', 'ai_badge_shown', 'ts',
    ]

    excluded_ids, excluded_idg = tombstone.excluded_keys(players)
    player_ids = {p.id for p in players if p.id not in excluded_ids}
    by_session_and_idg = {(p.session_id, p.id_in_group): p for p in players}
    messages = [m for m in Message.filter() if m.player_id in player_ids]

    for m in messages:
        sender = m.player
        if (sender.session_id, int(m.recipient_id)) in excluded_idg:
            continue
        session = sender.session
        recipient = by_session_and_idg.get((sender.session_id, int(m.recipient_id)))
        recipient_label = recipient.participant.label or '' if recipient else ''
        sender_label = sender.participant.label or ''
        yield _config_stamp(session) + [
            m.id,
            m.thread_id,
            m.ordinal_in_thread,
            sender_label,
            opaque_id(session.code, sender_label or sender.participant.code),
            recipient_label,
            opaque_id(session.code, recipient_label or recipient.participant.code) if recipient else '',
            m.char_count,
            m.word_count,
            m.token_count,
            m.provenance,
            m.edit_distance,
            m.pct_retained,
            m.acceptance,
            m.compose_time_ms,
            m.paste_detected,
            m.ai_badge_shown,
            m.ts,
        ]


def custom_export_corpus(players):
    """De-identified text corpus (spec Section 13: 'handles -> opaque ids'
    -- the one export carrying raw sent_text). message_id/dyad/opaque_author
    /ordinal/sent_text is the spec's exact column list; session/wave/
    condition are added since they're needed to filter/group and don't
    themselves identify anyone."""
    yield _CONFIG_STAMP_HEADER + ['message_id', 'dyad', 'opaque_author', 'ordinal', 'sent_text']

    excluded_ids, excluded_idg = tombstone.excluded_keys(players)
    player_ids = {p.id for p in players if p.id not in excluded_ids}
    by_session_and_idg = {(p.session_id, p.id_in_group): p for p in players}
    messages = [m for m in Message.filter() if m.player_id in player_ids]

    for m in messages:
        sender = m.player
        if (sender.session_id, int(m.recipient_id)) in excluded_idg:
            continue
        session = sender.session
        recipient = by_session_and_idg.get((sender.session_id, int(m.recipient_id)))
        sender_oid = opaque_id(session.code, sender.participant.label or sender.participant.code)
        recipient_oid = (
            opaque_id(session.code, recipient.participant.label or recipient.participant.code)
            if recipient else ''
        )
        dyad = '__'.join(sorted([sender_oid, recipient_oid])) if recipient else sender_oid
        yield _config_stamp(session) + [
            m.id,
            dyad,
            sender_oid,
            m.ordinal_in_thread,
            m.sent_text,
        ]


def custom_export_diffusion(players):
    """Diffusion table (spec Section 13 + Section 8): adoption times,
    exposure counts, neighbor sets, and whether the row was a seed (parsed
    from the one-shot 'diffusion_seeded' Event payload rather than
    re-derived from timing heuristics)."""
    yield _CONFIG_STAMP_HEADER + [
        'participant_label', 'opaque_id', 'item', 'adopted', 'adoption_ts',
        'exposure_count_at_adoption', 'adopted_neighbor_ids', 'n_exposures_total',
        'first_exposure_ts', 'seeded',
    ]

    excluded_ids, excluded_idg = tombstone.excluded_keys(players)
    player_ids = {p.id for p in players if p.id not in excluded_ids}
    adoptions = {a.player_id: a for a in Adoption.filter() if a.player_id in player_ids}
    exposures_by_pid = {}
    for e in Exposure.filter():
        if e.player_id in player_ids:
            exposures_by_pid.setdefault(e.player_id, []).append(e)
    seeded_keys = set()  # (session_id, id_in_group)
    for e in Event.filter():
        if e.player_id in player_ids and e.type == 'diffusion_seeded':
            try:
                payload = json.loads(e.payload)
            except (ValueError, TypeError):
                continue
            session_id = e.player.session_id
            for idg in payload.get('seeded_ids', []):
                seeded_keys.add((session_id, idg))

    for p in players:
        if p.id in excluded_ids:
            continue
        session = p.session
        participant = p.participant
        label = participant.label or ''
        adoption = adoptions.get(p.id)
        my_exposures = sorted(exposures_by_pid.get(p.id, []), key=lambda e: e.ts)
        yield _config_stamp(session) + [
            label,
            opaque_id(session.code, label or participant.code),
            session.config.get('diffusion_item', ''),
            adoption is not None,
            adoption.ts if adoption else '',
            adoption.exposure_count_at_adoption if adoption else '',
            adoption.adopted_neighbor_ids if adoption else '[]',
            len(my_exposures),
            my_exposures[0].ts if my_exposures else '',
            (p.session_id, p.id_in_group) in seeded_keys,
        ]
