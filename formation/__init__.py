import json
import os
import time

from otree.api import *

import crosswave

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

PHASE STATUS: this is the Phase 4 (diffusion + Wave 2) version. All four
conditions exist, both waves run through this same app, and the
diffusion mechanic is wired in.
  - Messaging + persistent threads: done (Phase 1).
  - Explicit tie tracking (connect / connect_remove): done (Phase 1).
    Behavioral ties (>=3 messages each way) are NOT computed live -- per
    spec Section 9 they are derived offline from the Message table at
    analysis time.
  - AI-assist (ai_draft branch, AIEvent, provenance snapshot fields on
    Message): done Phase 2. Condition gating: conditions 2-4 have
    AI-assist enabled, condition 1 (control) does not -- see
    ASSIST_ENABLED_CONDITIONS below. This condition->feature mapping is a
    build-time assumption (documented in the Phase 2 section of this
    docstring) since the exact condition numbering wasn't restated in
    every phase of the spec; adjust ASSIST_ENABLED_CONDITIONS if the
    study's actual condition numbering differs.
  - Disclosure badge (ai_badge_shown, recipient-visible cue): done Phase
    3 -- see PHASE 3 NOTES below.
  - Diffusion mechanic (Exposure/Adoption, seeding, adopt surface): done
    this phase -- see PHASE 4 NOTES below.
  - Wave 2 (Rooms, cross-wave store, recontact app): done this phase --
    this app itself needed NO changes for Wave 2 beyond what Phase 1
    already built (it was wave-agnostic from the start, per the
    CORE INVARIANTS above); the new work lives in the `crosswave` module
    and the new `recontact`/`survey2`/`debrief` apps.

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
  - The badge is presentation-only and is recomputed server-side, every
    time, from `condition` + `Message.provenance` -- the client is never
    trusted to say whether a message should show the AI badge.
    `ai_badge_for(recipient, msg)` is the single source of truth: only
    condition 3 (disclosed) ever returns True, and only for messages
    whose provenance is 'ai_assisted'. Condition 4 (undisclosed) and
    condition 2 (available/un-manipulated, no disclosure UI at all) never
    show a badge, regardless of provenance.
  - The badge is computed once, at push time (when the message is
    delivered to its recipient in the 'send' branch), and the result is
    stored on `Message.ai_badge_shown` so re-loading thread history later
    reflects exactly what the recipient actually saw at the time, rather
    than being recomputed against whatever the *viewer's* condition
    happens to be when they reload (this matters because a thread mixes
    messages sent by both participants, and only the message's actual
    recipient should ever see its badge).
  - The condition-3-vs-4 manipulation also has a sender-side component
    (spec Decision G / Section 7): does the sender know in advance that
    their AI-assisted messages will be labeled to the recipient? That is
    carried by the `sender_disclosure_cue` boolean session-config flag
    (see settings.py), passed to the template via vars_for_template and
    rendered as a single line of copy near the AI-assist button -- kept
    deliberately minimal per the spec's "keep the difference minimal"
    instruction, since the cue itself is not supposed to be a second,
    uncontrolled manipulation.

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

    # Conditions with AI-assist turned on (see PHASE STATUS docstring note
    # above -- this mapping is a build-time assumption, adjust if the
    # study's real condition numbering differs).
    ASSIST_ENABLED_CONDITIONS = {2, 3, 4}

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
    ai_badge_shown = models.BooleanField()  # spec Section 7; see ai_badge_for()
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
    my_id_str = str(player.id_in_group)
    # Reloading a thread that contains a message from an already-adopted
    # peer counts as an exposure too (spec Section 8: "in a thread").
    if is_adopted(peer):
        log_exposure_if_new(player, peer.id_in_group)
    peer_adopted = is_adopted(peer)
    return {
        'type': 'thread_history',
        'peer': peer_id,
        'peer_adopted': peer_adopted,
        'messages': [
            {
                'sender': m.player.id_in_group,
                'body': m.sent_text,
                'ts': m.ts,
                # Ground truth is whatever was computed/stored at push
                # time (see ai_badge_for); only ever surfaced to the
                # message's actual recipient, never to the sender's own
                # view of their own sent message.
                'ai_badge_shown': bool(m.ai_badge_shown) if m.recipient_id == my_id_str else False,
            }
            for m in matching
        ],
    }


def assist_enabled(player: Player) -> bool:
    return player.session.config['condition'] in C.ASSIST_ENABLED_CONDITIONS


def ai_badge_for(recipient: Player, msg: 'Message') -> bool:
    """Spec Section 7: presentation-only, recomputed server-side from
    `condition` + `msg.provenance` every time -- never trust the client
    for this. Only condition 3 (disclosed) ever labels a message, and
    only when that message was actually ai_assisted."""
    cond = recipient.session.config['condition']
    return cond == 3 and msg.provenance == 'ai_assisted'


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
            # Sender-side half of the condition-3-vs-4 manipulation (spec
            # Section 7 / Decision G): whether the sender is told their
            # AI-assisted messages will be labeled to the recipient.
            'sender_disclosure_cue': player.session.config['sender_disclosure_cue'],
            # Diffusion mechanic (spec Section 8): the client only needs to
            # know the item's display name and whether *this* player has
            # already adopted it (to decide whether to show an Adopt
            # button at all).
            'diffusion_item': player.session.config['diffusion_item'],
            'my_adopted': is_adopted(player),
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
            # Disclosure badge (spec Section 7): computed once, here, at
            # push time, from the recipient's own condition + the
            # message's provenance -- then persisted on the Message row
            # so a later thread reload shows exactly what was actually
            # disclosed, not a recomputation against whatever condition
            # the *viewer* happens to be in at reload time.
            badge = ai_badge_for(recipient, msg)
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
                'ai_badge_shown': badge,
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

        log_event(player, 'unknown_event', payload=data)
        yield {player.id_in_group: {'type': 'error', 'message': f'Unknown event type: {t}'}}


page_sequence = [Formation]
