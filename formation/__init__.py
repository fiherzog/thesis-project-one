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

PHASE STATUS: this is the Phase 1 (skeleton + control cohort) version.
  - Messaging + persistent threads: done.
  - Explicit tie tracking (connect / connect_remove): done. Behavioral
    ties (>=3 messages each way) are NOT computed live -- per spec Section
    9 they are derived offline from the Message table at analysis time.
  - AI-assist (ai_* branches, AIEvent, provenance snapshot fields on
    Message): Phase 2.
  - Disclosure badge: Phase 3.
  - Diffusion mechanic (Exposure/Adoption): Phase 4.
"""


class C(BaseConstants):
    NAME_IN_URL = 'formation'
    PLAYERS_PER_GROUP = None  # one group spanning every player in the session
    NUM_ROUNDS = 1


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
    msg = Message.create(
        player=player,
        wave=get_wave(player),
        recipient_id=str(peer_id),
        thread_id=tid,
        ordinal_in_thread=ordinal,
        sent_text=text,
        char_count=len(text),
        word_count=len(text.split()),
        token_count=len(text.split()),  # naive placeholder tokenizer for Phase 1
        provenance='human_only',
        ai_output_final='',
        edit_distance=0,
        pct_retained=0.0,
        acceptance='n/a',
        compose_time_ms=int(data.get('compose_time_ms', 0)),
        paste_detected=bool(data.get('paste_detected', False)),
        ai_badge_shown=False,
        ts=now_ms(),
    )
    log_event(player, 'message_sent', target_id=peer_id, payload={'len': len(text)})
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
        return {'my_handle': player_handle(player)}

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

        log_event(player, 'unknown_event', payload=data)
        yield {player.id_in_group: {'type': 'error', 'message': f'Unknown event type: {t}'}}


page_sequence = [Formation]
