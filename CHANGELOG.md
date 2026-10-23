# Conduit plugin changelog

What each plugin version brings, newest first. Conduit shows the entries
between the version on your host and the one an update installs, security
fixes first, so you can tell whether an update matters to you.

Every version bump in `plugin.yaml` adds an entry here (tests/test_changelog.py
checks it). The format is fixed, because Conduit reads it:

- `## <version> (<YYYY-MM-DD>)`: the version and the day it reached main.
- Then any of these sections, in this order: `### Security`, `### Fixes`,
  `### New and improved`. Leave out a section with nothing in it.
- One line per change, starting with `- `. Fixes say what goes wrong without
  the update. Plain words: these show on a phone.

## 0.63.0 (2026-10-23)

### New and improved
- Conduit can hand the whole plugin update to the host: Hermes downloads it,
  restarts the gateway and the dashboard on its own, and sends a notification
  when it's done, so Conduit doesn't have to stay open.
- This changelog: Conduit's update notice shows what each version brings and
  when it was released.

## 0.62.4 (2026-10-23)

### Fixes
- Deleting a place rule removes its jobs too. Without this, they kept running.
- Rate limits count per profile, a stuck GPT-Live status clears on its own, and
  voice tools follow the device that's on the call.

## 0.62.3 (2026-10-23)

### Security
- A Watch approval answers only the command the Watch showed. Without this, an
  approval could land on a different command that replaced it.

## 0.62.2 (2026-10-23)

### Fixes
- Phone changes read as offered, not seen, until the phone answers.

## 0.62.1 (2026-10-23)

### Fixes
- Approval alert calls follow the chat the turn moved to, and the Watch you
  chose wins when two devices' settings merge.

## 0.62.0 (2026-10-22)

### Security
- Group room turns stay out of your memory provider. Without this, words other
  people said in a room could be saved as if you had said them.

## 0.61.3 (2026-10-22)

### Fixes
- Pulse's memory and follow-up calls read keys in their own profile's scope.
  Without this, every Pulse follow-up check failed once the dashboard served
  more than one profile.

## 0.61.2 (2026-10-22)

### New and improved
- Pulse follow-ups get their own daily budget, so they don't use up the day's
  other Pulse notifications.

## 0.61.1 (2026-10-22)

### New and improved
- `hermes conduit-push pulse followups now` checks every pending follow-up at
  once, for testing.

## 0.61.0 (2026-10-22)

### New and improved
- Pulse reminds you of what you said you'd do.

## 0.60.0 (2026-10-22)

### Fixes
- A held turn notifies after 30 minutes whatever Conduit says, so a long job
  can't stay silent by mistake.

### New and improved
- Chats open on your screen keep their replies quiet instead of sending a
  notification.

## 0.59.0 (2026-10-22)

### New and improved
- Standalone call rows and Classic voice chats are archived when the call
  ends, so they don't fill your chat list.

## 0.58.1 (2026-10-22)

### Fixes
- Conduit lets go of a chat once it sits idle. Without this, Hermes Desktop
  refused to open a chat Conduit had used.

## 0.58.0 (2026-10-22)

### New and improved
- Location sharing can include the host's other profiles paired with the same
  iPhone.

## 0.57.0 (2026-10-22)

### New and improved
- Settings > Pulse picks the model Pulse runs on.

## 0.56.1 (2026-10-22)

### Security
- Re-pairing revokes the relay credential it replaces. Without this, every
  re-pair left a working credential behind, until pairing failed at the
  relay's limit with a misleading "try again shortly".

## 0.56.0 (2026-10-22)

### New and improved
- Pulse reports and the daily brief show on Conduit's Pulse screen.

## 0.55.3 (2026-10-22)

### Fixes
- When Hermes can't reach the phone to locate it, it says why and how to fix
  it instead of a bare "not set up".

## 0.55.2 (2026-10-22)

### Fixes
- Pulse ratings no longer save the same lesson twice.

## 0.55.1 (2026-10-22)

### Fixes
- After the relay refuses this host, notifications back off instead of
  retrying every event, and a block in front of the relay (a firewall or
  proxy) is named as such.

## 0.55.0 (2026-10-22)

### New and improved
- Rating Pulse's messages changes what it sends you.

## 0.54.2 (2026-10-22)

### New and improved
- Pulse follows up on what Hermes promised to do.

## 0.54.1 (2026-10-21)

### Fixes
- Pulse takes over its chat when Hermes Desktop holds it, instead of failing.

## 0.54.0 (2026-10-21)

### New and improved
- Conduit's preferences sync across your devices through the host.

## 0.53.2 (2026-10-21)

### Fixes
- Small Pulse fixes.

## 0.53.1 (2026-10-21)

### Fixes
- Pulse turns no longer leave a Live Activity running.

## 0.53.0 (2026-10-21)

### New and improved
- Pulse: Hermes reaches out on its own when something needs you. Off by
  default.

## 0.52.3 (2026-10-21)

### Fixes
- Live Activity updates pause more reliably after the relay refuses them.

## 0.52.2 (2026-10-21)

### Fixes
- Live Activity updates pause when the relay refuses them, instead of
  retrying every turn.

## 0.52.1 (2026-10-21)

### Fixes
- GPT-Live takes a briefing up to 64 KB. Without this, calls with a long
  briefing (CarPlay, long chats) failed to start.

## 0.52.0 (2026-10-21)

### New and improved
- Read-only calendar and reminders: Hermes can read them but never change
  them.

## 0.51.9 (2026-10-21)

### Fixes
- A calendar or reminder change goes to the device you're using, and one a
  re-registered phone missed is reported lost instead of vanishing.

## 0.51.8 (2026-10-21)

### Fixes
- Artifacts leave out files that are no longer on the host.

## 0.51.7 (2026-10-20)

### Security
- Phone sharing keeps to what you chose: calendars you unselect are dropped,
  and re-keying a phone forgets its shared location.

## 0.51.6 (2026-10-20)

### Fixes
- Ships the fixes from 0.51.5 that went out under the same number.

## 0.51.5 (2026-10-20)

### Fixes
- Health, location and phone data stores lock across processes, so a device
  registering during a wipe isn't lost.

## 0.51.4 (2026-10-20)

### Security
- A Watch call's grant stays on its own profile. Without this, a grant could
  reach jobs on another profile of the same host.

## 0.51.3 (2026-10-20)

### Security
- Voice reads of health, location and phone data refuse an answer when the
  device changed its key during the read, instead of sealing it to the old
  key.

## 0.51.2 (2026-10-20)

### Security
- A spoken yes approves a phone change only when it's your very next words.

## 0.51.1 (2026-10-20)

### New and improved
- Hermes knows you can approve phone changes by voice.

## 0.51.0 (2026-10-20)

### New and improved
- Hermes knows which part of Conduit a message came from (chat, voice call,
  Watch, CarPlay).

## 0.50.1 (2026-10-19)

### Fixes
- Local Live fixes.

## 0.50.0 (2026-10-19)

### New and improved
- Local Live on a read-back speech server: the plugin runs the call's turns.

## 0.49.0 (2026-10-19)

### New and improved
- Local Live agent mode: a fast model answers, Hermes does the work.

## 0.48.4 (2026-10-19)

### New and improved
- Local Live on the Watch shows Hermes' approval requests on the Watch.

## 0.48.3 (2026-10-19)

### Fixes
- Notifier fixes from community reports.

## 0.48.2 (2026-10-19)

### Fixes
- plugin.yaml lists the plugin's tools, hooks and middleware, so Hermes shows
  them.

## 0.48.1 (2026-10-19)

### Fixes
- Contributor fixes from 2026-10-18.

## 0.48.0 (2026-10-19)

### Fixes
- Two chats setting up the same call watch make one job instead of two.

### New and improved
- Local Live calls on the Watch.

## 0.47.1 (2026-10-18)

### Fixes
- The speech server can read its key from a file
  (`voice.speech_server.api_key_file`).

## 0.47.0 (2026-10-18)

### New and improved
- Media, contact and live status cards.

## 0.46.1 (2026-10-18)

### Security
- Subagents can't set up or change call watches.
- The pairing file is written owner-only and never through a symlink.

## 0.46.0 (2026-10-18)

### New and improved
- Hermes can show stats, charts, forecasts, comparisons and steps as cards.

## 0.45.1 (2026-10-17)

### Fixes
- Call-screen cards no longer repeat their text in the chat.

## 0.45.0 (2026-10-17)

### New and improved
- Hermes can show maps and places as cards, in chats too.

## 0.44.1 (2026-10-17)

### Fixes
- Local Live calls are saved like other calls.

## 0.44.0 (2026-10-17)

### New and improved
- Hermes can put tables, steps and links on a voice call's screen.

## 0.43.2 (2026-10-17)

### Fixes
- A chat's Artifacts show up for chats on a busy profile. Without this, the
  sheet could stay empty.

## 0.43.1 (2026-10-17)

### Fixes
- Voice calls delete calendar items with the phone's own tool.

## 0.43.0 (2026-10-17)

### New and improved
- Hermes can delete a reminder.

## 0.42.1 (2026-10-17)

### Fixes
- Local Live: a start error retries, and errors are logged.

## 0.42.0 (2026-10-17)

### New and improved
- Local Live: your own speech server speaks Classic replies and can run calls
  through Hermes.

## 0.41.0 (2026-10-16)

### New and improved
- Custom pictures for group rooms.

## 0.40.0 (2026-10-16)

### New and improved
- Conduit's Artifacts screen lists a profile's files.
