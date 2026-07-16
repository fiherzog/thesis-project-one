# Provenance Network Platform

An [oTree](https://www.otree.org/) (>= 6.0, async live pages) research platform for a two-wave
online experiment studying how **AI-assisted messaging and its disclosure (or non-disclosure)**
affect social-network formation, trust, and the diffusion of a simple social item (e.g. a
"badge") through a group.

Participants join a session, build a small social network with the other participants in their
cohort by exchanging live chat messages and forming explicit "connections," optionally with the
help of an AI drafting assistant, and complete short surveys. Roughly a week later the same
cohort is recontacted for a second wave that re-measures the network and surveys, to see how
things evolved. The whole platform is driven by a three-condition design (control, AI-assist
unlabeled, AI-assist disclosed), server-side logging of every meaningful event, and a
diffusion mechanic for tracking how an adopted item spreads through the network.

This repository is being built out incrementally, phase by phase, against a detailed build
specification. Each phase is implemented, tested end-to-end against a local oTree devserver,
committed, and pushed before moving on to the next.

## Study design at a glance

- **Conditions** (`condition` in session config, never trusted from the client; valid values are
  exactly `{1, 2, 3}`, enforced server-side by `formation.creating_session`):
  1. **Control** -- no AI-assist available at all.
  2. **AI-assist, unlabeled** -- AI drafting help is on, but never disclosed to the recipient, and
     the sender isn't cued about it either. Participants only learn this may have happened at
     Wave-2 debrief.
  3. **AI-assist, disclosed** -- AI-assisted messages are labeled to the recipient ("AI-assisted"
     badge), and the sender is told in advance that this will happen.
- **Two waves**, run as two separate oTree sessions per cohort, joined via the same oTree
  **Room** so each real participant keeps a stable `participant.label` across both sessions
  (oTree's ordinary `participant.vars` do not survive across separate sessions, so anything that
  needs to survive the multi-day gap -- prior ties, adoption state -- is snapshotted to a small
  JSON store keyed by `(room_name, participant_label)`; see `crosswave.py`).
- **Diffusion mechanic**: a small number of participants are seeded as having "adopted" an item
  partway through the session; exposure (seeing an adopted participant) and adoption are logged
  as first-class events, so downstream analysis can look at exposure-threshold dynamics
  ("simple" vs. "complex" contagion).
- Every meaningful client/server interaction (message sent, connect requested, AI draft
  requested, exposure logged, item adopted, etc.) is written as an `Event`/`AIEvent`/`Exposure`/
  `Adoption` row server-side -- nothing is inferred purely from client state, and the client is
  never trusted for anything that affects condition logic, disclosure, or scoring.

## App structure

Each subdirectory is a self-contained oTree app (`__init__.py` + page templates). Which apps run,
and in what order, is controlled per-session by `app_sequence` in `settings.py`:

| App          | Wave | Purpose |
|--------------|------|---------|
| `intro`      | 1    | Consent, profile creation (handle/avatar/interests), tutorial. |
| `recontact`  | 2    | Wave-2 equivalent of `intro`: welcomes returning participants back, recovers their Wave-1 identity/ties from the cross-wave store, and falls back to a fresh profile form if no snapshot is found. |
| `formation`  | 1, 2 | The core of the study: **one** oTree live Page (not a page sequence) that runs for the whole session. Handles the participant directory, live 1:1 messaging with persistent threads, explicit "connect" ties, the AI-assist drafting flow, the disclosure badge, and the diffusion mechanic (seeding/exposure/adoption). Wave-agnostic by design -- the same code runs in both waves. |
| `survey1`    | 1    | Wave-1 post-session surveys (placeholder instruments: network closeness, trust in AI). |
| `survey2`    | 2    | Wave-2 re-measurement of the same instruments, for longitudinal comparison. |
| `debrief`    | 2    | End-of-study debrief, including disclosing unlabeled AI-assist to condition-2 participants and an option to withdraw data. Currently a placeholder -- full hardening (withdrawal handling, integrity flags, out-of-band debrief for dropouts) is a later phase. |

Supporting modules at the project root:

- `settings.py` -- session configs (one per condition x wave, i.e. `cond{1-3}_wave{1,2}`),
  oTree `ROOMS` definitions (one Room per condition/cohort), and shared defaults.
- `crosswave.py` -- the cross-wave JSON store: `snapshot_wave1()` (called from `formation` at the
  end of a Wave-1 session) and `load_wave1()` (called from `recontact` at the start of Wave-2),
  keyed by `(room_name, participant.label)`, with file locking so concurrent snapshots from
  different participants in the same cohort don't clobber each other.
- `_rooms/` -- oTree Room participant-label files (whitespace-separated labels), used to give
  test/demo cohorts stable identities across waves.

## Build phases

The platform is being built and validated phase by phase; each phase's code is exercised against
a real local devserver (HTTP + live-page websocket traffic) before being committed:

1. **Phase 0** -- spike: confirm oTree 6.x async live-page mechanics work as expected.
2. **Phase 1** -- `intro` + `formation` (messaging, ties) + `survey1`, control condition only.
3. **Phase 2** -- AI-assist drafting (`ai_draft` live-method branch, server-side-only Anthropic
   API calls, provenance/acceptance tracking on sent messages, per-participant rate limiting and
   spend ceiling).
4. **Phase 3** -- disclosure badge: server-computed, recomputed from `condition` + message
   provenance every time, persisted per-message so a later reload reflects what was actually
   shown at send time; plus the sender-side disclosure cue. Completes all three conditions.
5. **Phase 4** -- diffusion mechanic (seeding, exposure logging, adoption) and the full Wave-2
   machinery: `crosswave` store, `recontact`/`survey2`/`debrief` apps, oTree Rooms, and the
   `cond{1-3}_wave2` session configs.
6. **Phase 5** (planned) -- custom data export suite for analysis.
7. **Phase 6** (planned) -- hardening: full debrief/withdrawal handling, integrity flags, edge
   cases around incomplete/dropout participants.

## Running locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export OTREE_ADMIN_PASSWORD=... # and ANTHROPIC_API_KEY if testing AI-assist conditions
otree devserver
```

Then visit `http://localhost:8000` (oTree's demo page) to create a session for any of the
`cond{1-3}_wave{1,2}` configs, or use oTree's REST `/api/sessions` endpoint to script it.

## Status

This README reflects the state of the repository as of the end of Phase 4's implementation.
See the module-level docstrings in each app's `__init__.py` for a more detailed, phase-by-phase
account of what's implemented, what's explicitly deferred, and any build-time assumptions worth
double-checking against the original study spec before running real sessions (e.g. the
placeholder survey wording, the condition-to-feature mapping, and pricing tables used for the
AI-assist spend ceiling).
