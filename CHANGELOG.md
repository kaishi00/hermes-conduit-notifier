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

## 0.77.4 (2026-10-25)

### Fixes
- Turning "Self-improvement updates" off, or on, didn't reach chats that
  were already open: they kept showing the updates the way they did when
  they opened, until Hermes restarted. Each update now follows the setting
  as it is when it's made.

## 0.77.3 (2026-10-25)

### Fixes
- Notifications from a bot's chat said its profile, like "default", instead
  of the bot's name. They now use the name the Bots list shows, and so does a
  call from that bot.

## 0.77.2 (2026-10-25)

### Fixes
- Preparing GPT-Live for the Watch failed every time with "No module named
  '_cffi_backend'" on current Hermes, because the install borrowed packages
  from Hermes instead of adding its own. Tap Prepare again after updating.

## 0.77.1 (2026-10-25)

### Fixes
- Pulse could ask about something you'd already finished in a long chat,
  because it read only that chat's last messages. It now also reads what the
  chat said on the same subject before them.
- Reading your older chats, Pulse could remind you of something days after
  its time, like a 4 PM reminder five days late. One it finds there more than
  half a day after its time is now set aside, not sent.
- What you send with Do it, and Hermes's answer to it, no longer become new
  things for Pulse to follow up on, even when Hermes answers much later. An
  offer in Hermes's answer could turn into a promise, and Pulse would then nag
  about it.
- Pulse's report now says only what it was given. It could repeat what it
  had said in earlier turns as if it were news, and blame a reminder job
  that doesn't exist.

## 0.77.0 (2026-10-25)

### New and improved
- Every item in Pulse's report now gets Do it and Done, dates and things
  worth knowing that Pulse found on its own included. Before, only items
  about something Pulse remembers had them.
- Do it on one of those opens the chat Pulse found it in, or a new chat when
  it came from somewhere else, like your calendar or reminders. Pulse then
  reads what came of it and marks the card done by itself once it's taken
  care of.
- Done tells Pulse an item is handled, and the next report knows it's done.

## 0.76.0 (2026-10-25)

### New and improved
- Pulse's morning brief now comes every day at its time: one card that sums
  up what's still open for you, with today's calendar and reminders, what
  your Shortcuts automations logged, and any news you asked to hold for the
  morning. Before, it only came when news had been held for it, so most
  mornings it never showed up.
- The morning brief's default time is now 07:00, an hour before the first
  report, so the two don't arrive together. A time you picked stays.
- The report after the morning brief leaves out what the brief already
  brought up, unless something about it changed. On a busy morning the
  brief keeps your calendar and reminders before anything else.
- `hermes conduit-push pulse status` shows the morning brief's time.

## 0.75.0 (2026-10-25)

### New and improved
- Qwen Live is a separate voice provider on iPhone and Apple Watch.
  The host supplies temporary credentials; call audio connects directly to
  Alibaba Cloud, without going through the Conduit relay or the Hermes host.

## 0.74.0 (2026-10-25)

### New and improved
- The Hermes dashboard's Sessions page shows a Conduit card: whether the
  app is reaching this host (connected, or when it was last seen), and
  whether its notifications get through the relay, with the reason when
  they don't.

## 0.73.0 (2026-10-25)

### New and improved
- Shortcuts automations can log to Hermes. End an automation (a notification
  from an app you pick, a message, an email, a Wallet payment, an NFC tag, a
  sound the phone hears) with Conduit's Log to Hermes action, and Hermes can
  look it up later ("what did my bank send today?") without a reply or a
  push each time. Sign-in codes are never kept, and events go after 30 days.
- With Pulse on, its reports read what your automations logged, and when
  you ask Hermes to tell you when something happens ("let me know when the
  bank confirms the transfer"), Pulse checks as soon as a new event comes in.
- Conduit's Settings can show what was logged from your iPhone and delete
  any of it.

## 0.72.0 (2026-10-24)

### Security
- Encryption keys for notifications now rotate every 7 days through a key
  exchange inside the encrypted channel, so new keys never travel over the
  dashboard connection. On iOS 26 the exchange is quantum-safe (ML-KEM-768
  with X25519): a recording of today's traffic can't open later
  notifications, even with a future quantum computer.

## 0.71.2 (2026-10-24)

### Fixes
- A notification no longer shows a file's path on the host
  (MEDIA:/opt/data/cache/…): replies, questions, approvals, calls, updates
  and phone changes show a paperclip and the file's name, or its type when
  the name is only a code. So do the answer card under a question or an
  approval, and the call screen.

## 0.71.1 (2026-10-24)

### Fixes
- Pictures Hermes made with image generation now show in Artifacts. Without
  the update, a chat's generated images are missing from its Artifacts list
  and from the Artifacts page.

## 0.71.0 (2026-10-24)

### New and improved
- Encryption keys for notifications now rotate. Conduit hands this host a
  new key every 30 days, or sooner when you ask (`hermes conduit-push e2e
  rotate`). The old key still opens your answers for 7 days, so nothing in
  flight is lost; `rotate --now` drops it at once for a key that may have
  leaked.

## 0.70.3 (2026-10-24)

### Fixes
- A Pulse reminder held back for a free slot no longer goes out the next
  day for something you did while it waited: it's checked again first.
- The morning brief leaves out news that got done or went out of date
  while it waited for the brief. Undo on the card puts it back.
- Pulse checks see the calendar and open reminders on your phone, so
  something you already planned there isn't nagged about.
- The report also looks at later chats for things you asked Pulse to watch
  and things you wanted to start, and for items from your other profiles.
- After Do it, Pulse also notices when you got it done in another chat.

## 0.70.2 (2026-10-24)

### Fixes
- Pulse no longer asks about something you finished later in another chat or
  a cron run, or that a newer release moved past: a follow-up and the report
  now read later chats on the same subject, follow a long chat into its
  continuation, and see what Hermes is still running in the background.
- A week-old follow-up checks once more before asking whether you still want
  it, so a finished one closes instead.
- On Windows, the dashboard's check for an update still in progress could
  end the process it looked at. It now only asks Windows whether it runs.

## 0.70.1 (2026-10-24)

### Fixes
- Answering an approval by voice on the Apple Watch could say "approved"
  when nothing ran, because the request had just been answered elsewhere or
  timed out. The call now says so instead, and says "approved" only once
  Hermes has taken the answer.

## 0.70.0 (2026-10-24)

### New and improved
- A call from Hermes about a question or an approval can be answered on the
  Apple Watch, even with your iPhone out of reach: the Watch reads out the
  choices (and which one Hermes recommends) or the command, and your spoken
  answer goes to Hermes. Needs approval, question and failure calls on.
- With the Watch on Local Live, a call from Hermes rings it too, and what
  you say answers what Hermes asked.

## 0.69.2 (2026-10-24)

### Security
- A Watch could approve "once" a request that showed no command at all.
  Such a request can now only be denied, like one too long to show whole.

### Fixes
- On a Hermes that doesn't name its turns, the Lock Screen could keep
  showing a finished reply as running. It now ends after two minutes at most.
- Unpairing while a new pairing was claimed could delete the new one.
- A calendar or reminder link with no host name is refused instead of
  dropped by the phone.
- When the relay stopped answering, each notification could hold up the
  ones behind it for about a minute, until some were dropped. Each now gives
  up after about 30 seconds.
- A call watch run that Hermes names only by its job said the phone would
  ring when it couldn't; it now says what it found instead.
- A calendar store written by a newer plugin says to update the plugin
  instead of asking you to try again forever.
- Malformed answers or reports from the phone no longer cause server errors.

## 0.69.1 (2026-10-24)

### Fixes
- An update from Conduit could read as failed ("the gateway didn't restart")
  when the host restarts the dashboard together with the gateway. The update
  had worked: the restarted dashboard now finishes it and says so.

## 0.69.0 (2026-10-24)

### New and improved
- Pulse's report now lists everything it could bring up before it picks, and
  keeps that list for 30 days: what it picked, and why it left the rest out
  (`hermes conduit-push pulse candidates`). Reports read the same as before.

## 0.68.0 (2026-10-23)

### New and improved
- Swipe a Pulse card away with Not now. It hides the card without teaching
  Pulse anything, and Pulse leaves that topic alone for a week.
- The morning brief shows as one block with the cards it brought, under a
  short opener.
- Pulse pushes say what they bring: "Pulse noticed 3 things", "Your morning
  brief" or a follow-up.

## 0.67.0 (2026-10-23)

### New and improved
- Ask Hermes to text someone and it writes the message on their contact
  card. Tap Send text and Messages opens with the number and the text
  filled in; nothing is sent until you tap Send there.

## 0.66.1 (2026-10-23)

### Fixes
- If you rated a Pulse report by typing before its items showed as cards,
  that rating could be counted a second time weeks later. It now goes once
  the cards appear.
- Two Pulse processes settling cards at the same moment could each forget
  the other's, so a card could be looked up again. They no longer clash.

## 0.66.0 (2026-10-23)

### New and improved
- Pulse's report no longer arrives as one block to rate by typing "1 yes,
  2 no". Each thing it noticed is its own card, labelled with what it is
  about (a date, a promise, a follow-up), with thumbs up and down. Items
  about something Pulse remembers also get Do it and Done.
- A report that noticed something now sends a notification listing the
  items. One that found nothing stays quiet.

## 0.65.2 (2026-10-23)

### Fixes
- Updating the plugin from Conduit failed with "The update failed on the
  host (TypeError)" on a Hermes from before October 20: the gateway restart
  asked for an option that Hermes didn't have yet. The update now restarts
  the gateway on older Hermes too.

## 0.65.1 (2026-10-23)

### New and improved
- On a Watch call, a follow-up that only adds to a running job ("also ask
  for a quiet table") goes into the job without changing its direction.
  Corrections, holds and cancels still redirect it.

## 0.65.0 (2026-10-23)

### New and improved
- Notifications can show in the language Conduit is set to: the plugin now
  tells the phone what each notification is about and sends the chat's title
  on its own, so Conduit writes the titles itself.

## 0.64.2 (2026-10-23)

### Fixes
- When the last location has no distance to a saved place, the agent no
  longer blames the "places only" setting. It says the phone left distances
  out of that update, the update was taken at a private place, or the place
  was added after it, and offers a fresh location only when that can work.

## 0.64.1 (2026-10-23)

### Fixes
- After Do it on a follow-up card, Pulse now checks the chat even with
  follow-up checks turned off or their daily budget spent, so the card still
  gets marked done.

### New and improved
- A Pulse report that found nothing worth bringing up no longer leaves a card
  on the Pulse screen, and earlier ones like it are cleared away.

## 0.64.0 (2026-10-23)

### New and improved
- Follow-up cards on the Pulse screen can be marked done. Pulse closes that
  promise and stops checking on it, without counting it as a thumbs down, and
  Undo puts it back. A card marked done also leaves the morning brief.
- After Do it on a follow-up card, Pulse reads that chat once Hermes has
  answered and marks the card done by itself when the chat shows it got done.

## 0.63.3 (2026-10-23)

### Fixes
- A Watch call that has reconnected too many times is told to start again, so
  it ends at once instead of waiting for a reply that never comes.
- A Watch call on Grok or Local Live whose voice service stops answering ends
  after about a minute and frees its place, so other Watch calls can start.

## 0.63.2 (2026-10-23)

### Security
- A flood of wrong Local Live keys can no longer lock out your voice server's
  real key. Without this, its key stopped working for up to a minute.

### Fixes
- Undoing a setting change Hermes made reaches Hermes even when the change was
  made a while ago. Without this, Hermes could miss that you put it back.

## 0.63.1 (2026-10-23)

### New and improved
- Pulse has a nudge setting: gentle, balanced or eager. It sets how many
  reminders a day you get, how soon promises are checked, how much Pulse may
  spend on follow-ups and how much it takes to make a report.
- Pulse's daily budgets for memory and follow-ups can be set from Conduit or
  with `hermes conduit pulse budget`.

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
