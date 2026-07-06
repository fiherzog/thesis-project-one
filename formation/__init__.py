import json
import os
import time

from otree.api import *

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

PHASE STATUS: this is the Phase 2 (AI-assist) version.
  - Messaging + persistent threads: done (Phase 1).
  - Explicit tie tracking (connect / connect_remove): done (Phase 1).
    Behavioral ties (>=3 messages each way) are NOT computed live -- per
    spec Section 9 they are derived offline from the Message table at
    analysis time.
  - AI-assist (ai_draft branch, AIEvent, provenance snapshot fields on
    Message): done this phase. Condition gating: conditions 2-4 have
    AI-assist enabled, condition 1 (control) does not -- see
    ASSIST_ENABLED_CONDITIONS below. This condition->feature mapping is a
    build-time assumption (documented in the Phase 2 section of this
    docstring) since the exact condition numbering wasn't restated in
    every phase of the spec; adjust ASSIST_ENABLED_CONDITIONS if the
    study's actual condition numbering differs.
  - Disclosure badge (ai_badge_shown, recipient-visible cue): Phase 3.
  - Diffusion mechanic (Exposure/Adoption): Phase 4.

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
    ai_badge_shown = models.BooleanField()  # populated starting Phase 3
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


def build_directory(player: Player):
    others = player.get_others_in_group()
    connected_dst_ids = {
        t.dst_id
        for t in Tie.filter(player=player)
        if t.kind == 'explicit' and t.removed_at == 0
    }
    return {
        'type': 'directory',
        'players': [
            {
                'id': p.id_in_group,
                'handle': player_handle(p),
                'connected': str(p.id_in_group) in connected_dst_ids,
            }
            for p in others
        ],
    }


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
    return {
        'type': 'thread_history',
        'peer': peer_id,
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
        ai_badge_shown=False,  # populated starting Phase 3
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
    def vars_for_template(player: Player):
        return {
            'my_handle': player_handle(player),
            'assist_enabled': assist_enabled(player),
        }

    @staticmethod
    async def live_method(player: Player, data: dict):
        t = data.get('type')

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
            payload = {
                'type': 'incoming',
                'peer': player.id_in_group,
                'body': msg.sent_text,
                'ts': msg.ts,
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

        log_event(player, 'unknown_event', payload=data)
        yield {player.id_in_group: {'type': 'error', 'message': f'Unknown event type: {t}'}}


page_sequence = [Formation]
