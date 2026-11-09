"""Capabilities: the features this plugin serves, as Conduit reads them."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import Response

from . import core

router = core.router


# --- Capabilities ------------------------------------------------------------
#
# Conduit reads this once per connection to tell which of its features this
# plugin serves, and nudges an update when one it uses is missing. A plugin
# without this route predates it, which Conduit reads as "update". Names are
# stable feature ids, one per route family; add one with every new route.

ROUTE_CAPABILITIES = (
    "gemini-live",
    "qwen-live",
    "web-search",
    "memory",
    "personality",
    "gpt-live",
    "grok-live",
    "voice-sessions",
    "voice-tags",
    "voice-summary",
    "voice-chat-calls",
    "session-takeover",
    "e2e-notifications",
    # POST /e2e/rotate: keys rotate inline through a key exchange.
    "e2e-inline-rotation",
    "watch-tools",
    "watch-jobs",
    "watch-job-follow-ups",
    "watch-live-token",
    "watch-chats",
    "watch-audio",
    "watch-local-live",
    "watch-local-live-approvals",
    "local-live-read-back",
    "desktop-views",
    "turn-surface",
    # POST /turn-surface/discard: forgets a report whose send failed.
    "turn-surface-discard",
    # POST /sessions/history-page: Hermes' page of a chat's rows, the ones
    # Conduit holds sent as their fingerprints.
    "history-page",
    "hermes-calls",
    "hermes-call-presence",
    "hermes-call-outcomes",
    # GET /calls/waiting, POST /calls/waiting/answer, and answer_call on a
    # Watch grant: a call reads and answers what Hermes waits on.
    "hermes-call-answers",
    "conduit-settings",
    "conduit-prefs",
    "pulse",
    # The Pulse screen: cards, ratings and lessons (/pulse/feed, /rate, /learned, /levels).
    "pulse_feed",
    # PUT /pulse takes "model": the model Pulse's turns run on.
    "pulse_model",
    # PUT /pulse takes "nudge" and the daily token budgets.
    "pulse_nudge",
    "pulse_done",
    "pulse_acted",
    # POST /pulse/dismiss, cards' "brief" and "dismissed_at", Pulse push labels with a count.
    "pulse_dismiss",
    # The morning brief at PUT /pulse's brief_time comes every day with what's still open.
    "pulse_daily_brief",
    # /pulse/done and /acted take every report item, one Pulse found on its
    # own included, and one it found in a chat points at that chat.
    "pulse_found_actions",
    # PUT /pulse takes "note", the user's note to Pulse, which its reports,
    # brief and checks read.
    "pulse_note",
    # /pulse/dismiss takes "until" (later today, tomorrow, next week): the
    # card comes back as new then.
    "pulse_rest",
    # A report card's "why" is its reason (why now, where it came from),
    # for Why? on the Pulse screen; "" when it has none.
    "pulse_card_why",
    # PUT /pulse takes "quiet_days" and "quiet_until" (quiet times: weekends,
    # a vacation, quiet for today), and the state and feed say when it's quiet.
    "pulse_quiet",
    # Pulse learns from thumbs, Do it and Done what to bring up and how
    # loudly (pulse_prefs); a kind's level is the loudest it gets, and
    # thumbs no longer move it.
    "pulse_learns",
    # Learning shown: a card's "learned" (the biggest part learning played
    # in bringing it up, for Why?), /pulse/left_out (what reports left out,
    # and why) with /pulse/left_out/bring_up, and /pulse/learned's
    # "preferences" with /pulse/learned/reset.
    "pulse_explains",
    # PUT /pulse takes "night" (nothing pushes in it), the state and feed
    # say it (the feed with the brief's time, for Not now at night), and
    # /pulse/rate takes "wrong_time": the card comes back at a better time,
    # and that part of the day pushes no more for its kind.
    "pulse_hours",
    # Pulse brings up the picked people's birthdays and the yearly dates
    # memory keeps, and looks two weeks ahead once a day (pulse_ahead): PUT
    # /pulse takes "look_ahead", and /pulse/left_out's entries say which
    # part left each out ("from": "report" or "look_ahead").
    "pulse_dates",
    # Pulse memory keeps deadlines from the Shortcuts inbox (a trial ending,
    # a bill due): PUT /pulse takes "inbox_deadlines", /pulse/memory lists
    # them as kind "deadline" with "from_inbox", "sender" and "logged_at",
    # and their cards' topic is "deadline", with a level of its own.
    "pulse_inbox",
    # Pulse's turns may look things up first (pulse_tools): GET /pulse says
    # "look_first" and PUT /pulse takes it; a card's "looked" lists what its
    # turn looked at ("calendar", "phone", "contacts", "maps", "web").
    "pulse_look",
    # A Pulse turn's calendar adds are suggestions (pulse_proposals): GET
    # /pulse/feed?proposals=1 lists their cards (kind "proposal"), and POST
    # /pulse/proposals/<id>/add, /dismiss and /undo act on them.
    "pulse_proposals",
    # Pulse reads where the user is with Where you are on (pulse_places): GET
    # /pulse says "where" and "where_ready" and PUT /pulse takes "where"; a
    # remembered item said for a place has "place"; a card's "looked" may
    # list "location".
    "pulse_where",
    # Hide private reminders on the Lock Screen (pulse_push): GET /pulse says
    # "private_pushes" and PUT /pulse takes it. With it on, a Pulse push about
    # a gift, a surprise, someone's birthday, health or money has the body
    # "Pulse has something for you." and `private: true` in its sealed content.
    "pulse_private",
    # Pulse brings up next steps after something got done or an event ended
    # (pulse_candidates): their cards' topic is "next", and /pulse/learned's
    # "levels" has "next", held for the morning brief until the user turns
    # it up.
    "pulse_next",
    # Walking past (pulse_spots): a /location/ingest batch carrying
    # "spot_entries" (the watched shops the user came near, maybe none) and
    # "spots_room" gets "reply" back, sealed (spots_aad): {"spots": [{"id",
    # "query", "until", "intent", "radius": {"walk", "drive"}}]}, the shops
    # to watch now. GET /pulse says
    # "spots" and PUT /pulse takes it.
    "pulse_spots",
    "present-v1",
    "health-sync",
    "voice-tools",
    "phone-sync",
    "phone-alarms",
    "phone-contacts",
    "location-sync",
    "location-locate",
    # sharing.other_profiles: the host's other profiles paired with the
    # same iPhone read the location (location_profiles).
    "location-profiles",
    # /inbox: events the user's Shortcuts automations log (Log to Hermes).
    "shortcut-inbox",
    "pairing",
    "self-restart",
    # POST /self/update runs the whole update on the host and notifies.
    "self-update",
    # GET /self/gateway-work names what a gateway restart waits on; POST
    # /self/gateway-work/restart-now ends the update's wait for it.
    "gateway-work",
    "cron-notifications",
    # "act" on /cron/notifications: a job delivered to a Bot Chat just shows
    # its report there unless it lets the bot act on it (cron_bot_chat).
    "cron-bot-chat-reports",
    "reply-sources",
    "chat-on-screen",
    # POST /predict, GET /predict: Conduit's Predictive features, answered
    # by a no-tools fork of the chat for the asking device only.
    "predict",
    # POST /side-question: Conduit's step summaries, answered as Hermes' /btw
    # answers but only to the asking device (no "[btw …]" note in Desktop).
    "side-question",
    "room-files",
    # GET /room-files/data-url: a picture sent into a room, read back so
    # Conduit draws it instead of its file name.
    "room-file-pictures",
    # The same route reads any file sent into a room (up to 16 MB), so
    # Conduit opens and plays them as a chat does.
    "room-file-reads",
    # Linked Rooms carry each file sent into a room to its guests' hosts
    # (room_files_link), and the same route reads a home's path from this
    # host's copy, so a guest's Conduit draws a room someone else hosts too.
    "room-files-linked",
    "room-images",
    "speech-server",
    "local-live-agent",
    "artifacts",
    # /workspace/*: the Folder tab's listings, search, recent files and file actions.
    "workspace-files",
    # GET /workspace/resolve: where a file a reply names by its name is, from
    # the chat's folder, its repository's top, the terminal's folder and its
    # own tool calls (hermes-conduit#1809).
    "workspace-resolve",
    "local-live",
    # GET/PUT /rooms/choices and /rooms/defaults: an agent's choices in rooms
    # with other people's agents, enforced on this host (Linked Rooms).
    "room-rules",
    # DELETE /rooms/sessions: this profile's hidden chats for a room.
    "room-forget",
    # /rooms/identity and /rooms/links: Links between this installation and
    # other people's, for Linked Rooms (no profile: they're the installation's).
    "room-links",
    # GET/POST /rooms/setup: this Hermes' API server, a profile's room
    # ceiling and approvals, set up for Linked Rooms.
    "room-setup",
    # /rooms/drafts, /rooms/invites and /rooms/people: rooms with people
    # you've linked, from the draft to the start (Linked Rooms).
    "room-invites",
    # ?conduit_profile= on the routes that need the dashboard process (Local
    # Live, the speech server and Grok Live sockets, Watch grants, chat
    # takeover, Desktop views, predictions): any profile's, on a Hermes that
    # hosts other profiles' plugins apart (hermes-conduit#1336).
    "dashboard-profiles",
    # POST /rooms/leave and /rooms/drafts/remove: taking agents out of a
    # room with people you've linked, yours or theirs, and its fresh grants
    # while it goes on (Linked Rooms).
    "room-leave",
    # GET /rooms/approvals and POST /rooms/approvals/answer: your agents'
    # approvals from rooms other people host (Linked Rooms).
    "room-approvals",
    # GET/DELETE /rooms/copies and GET /rooms/copies/log: this Hermes' copies
    # of rooms other people host, sent by their home (Linked Rooms).
    "room-copies",
    # POST/GET /rooms/copies/messages: posting in rooms other people host,
    # through this Hermes, and GET /rooms/people's posts on the home: who
    # wrote what (Linked Rooms).
    "room-posts",
    # GET/PUT /rooms/looks: your bots' pictures and glow colors and yours,
    # sent to the people in your rooms over Links, and theirs (Linked Rooms).
    "room-looks",
    # GET/PUT /rooms/chats: a room's private chat with one of your agents,
    # told which room it's about, with notes on rooms it Listens in
    # (Linked Rooms).
    "room-chats",
    # ?conduit_profile= on POST /rooms/drafts and GET/POST /rooms/setup: a
    # room made for, and this Hermes set up for rooms on, any profile's, on a
    # Hermes that hosts other profiles' plugins apart (hermes-conduit#1336).
    "room-dashboard-profiles",
    # PUT /rooms/choices' Use my data lets a room's agent say when you're
    # busy (conduit_free_busy), times only (Linked Rooms).
    "room-free-busy",
    # PUT /rooms/choices' run_work: Run work, a room's agent starting work
    # for it, each start OKd on the owner's phone (a room's work ask, the
    # present store's room-work feature; Linked Rooms).
    "room-run-work",
    # /links/rules and /links/asks: asks between linked agents, answered by
    # each Link's rules or its owner (link_asks), and the present store's
    # prompts for them (Linked Rooms).
    "link-asks",
    # /workstreams: a bot's work for the Work panel and the user's own actions
    # on a workstream (Reply, Stop, Continue, Check first), ?conduit_profile=
    # included (Bot workstreams).
    "bot-workstreams",
    # GET /waiting: every chat with an approval or a question waiting on the
    # user, for the Needs you list, the profile strip's counts and the
    # in-app banner (Hermes is waiting on you, issue 1705).
    "waiting-list",
    # GET/POST /workstreams/settings: the bot's work settings, which Edit Bot
    # > Work shows and changes (on or off, what the Bot Chat does itself, which
    # settings this plugin takes (`policies`), running at once, after a
    # restart, the workstreams' approvals).
    "bot-work-settings",
    # POST /workstreams/adopt: a voice call's job kept in its bot's registry,
    # in the Work panel marked as a call's (Bot workstreams M5).
    "work-adopt",
    # POST /workstreams/call-ended: a call kept with the Bot Chat ended in
    # any voice mode, so the updates its workstreams held go out.
    "work-call-ended",
    # GET/POST /boards/checklists: a checklist card's ticks, kept on the host
    # for every Conduit, and heard by Hermes at the chat's next turn (Cards
    # Hermes shows).
    "board-checklists",
    # GET /boards/rooms: the boards agents showed in a room this host runs,
    # which Conduit draws under their messages (Cards Hermes shows).
    "room-boards",
    # GET /boards/desktop and POST /boards/desktop/seen: the boards the
    # plugin's Desktop half draws in Hermes Desktop chats (Cards Hermes shows).
    "desktop-boards",
    # GET /boards/diffs: the whole of a diff card too long for conduit_show's
    # answer, read when the user opens a file (Cards Hermes shows).
    "diff-cards",
    # POST /boards/taps: a tap on a card's tap button, heard by Hermes with
    # the user's message about it (Cards Hermes shows, issue 1946).
    "card-taps",
    # DELETE /boards/taps?id=: forgets a tap whose message Conduit didn't
    # send (Cards Hermes shows).
    "card-tap-drop",
    # GET /boards/reviews: a review card's round with the pages its options
    # open; its marks go as a tap with action review (Cards Hermes shows).
    "review-cards",
)


def _plugin_version() -> Optional[str]:
    """The version in the plugin's plugin.yaml, or None if unreadable."""
    path = os.path.join(core._plugin_root(), "plugin.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key.strip() == "version":
                    return value.strip().strip("\"'") or None
    except (OSError, UnicodeDecodeError):
        return None
    return None


# The version this process imported. Hermes imports this file once per
# dashboard process, so after `hermes plugins update` the version on disk
# moves ahead of the code that is serving until the dashboard restarts.
LOADED_VERSION = _plugin_version()
# Changes with every dashboard process, so Conduit can tell a restart landed.
BOOT_ID = os.urandom(8).hex()


# A host that installs and upgrades the plugin itself (an image that bakes a
# pinned version, a config manager) sets CONDUIT_PUSH_MANAGED: "1" (or
# true/yes/on) for a plain "managed", or a short text naming who manages it.
# Conduit then asks nobody to update it, and /self/update refuses.
MANAGED_ENV = "CONDUIT_PUSH_MANAGED"
MANAGED_BY_MAX = 80


def managed_by() -> Optional[str]:
    """None when the plugin isn't managed; "" for a plain managed host, or the
    host's own name for whoever manages it (printable, at most 80 chars)."""
    raw = (os.environ.get(MANAGED_ENV) or "").strip()
    if not raw or raw.lower() in {"0", "false", "no", "off"}:
        return None
    if raw.lower() in {"1", "true", "yes", "on"}:
        return ""
    text = "".join(ch for ch in raw if ch.isprintable()).strip()
    return text[:MANAGED_BY_MAX].strip()


@router.get("/capabilities")
async def get_capabilities(response: Response) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    installed = _plugin_version()
    body = {"ok": True, "version": installed, "loaded_version": LOADED_VERSION,
            "restart_needed": installed != LOADED_VERSION, "boot_id": BOOT_ID,
            "capabilities": list(ROUTE_CAPABILITIES)}
    managed = managed_by()
    if managed is not None:
        body["managed"] = True
        if managed:
            body["managed_by"] = managed
    return body
