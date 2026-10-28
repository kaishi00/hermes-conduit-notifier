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

## 0.113.0 (2026-10-28)

### Fixes
- After moving to a new iPhone, calls from Hermes rang only the Apple Watch,
  as a call for the old iPhone, and answering there gave no voice. Pairing a
  profile with the new iPhone now moves the Watch's ring there too (needs
  push relay 0.23).

### New and improved
- The host's log says where each call from Hermes went: the iPhone, the
  iPhone and its Apple Watch, or a notification because the iPhone can't ring.

## 0.112.0 (2026-10-28)

### New and improved
- Hermes can write the notification a reply sends, with a heading and a
  sentence on the result, when you asked to be told something is done or a
  scheduled job reports. Conduit shows it with Show in notifications on Full
  previews.

## 0.111.0 (2026-10-28)

### New and improved
- With Where you are on, Pulse can hold something for when you walk past a
  kind of shop, like a florist before an anniversary, and your iPhone
  watches the nearest few. Coming near one brings it up right there. A
  shop you said you'd stop at is watched too. A detour is enough for an
  opportunity like the flowers (about 750 m on foot, 3 km driving); an
  errand like eggs at Costco waits until you're at the shop.

## 0.110.0 (2026-10-28)

### New and improved
- Linked Rooms: everyone whose agents are in a room keeps a copy of it on
  their own Hermes, sent by the room's host as it goes on, so Conduit can
  show rooms other people host from your own Hermes. You keep your copy
  when you leave.

## 0.109.1 (2026-10-28)

### Fixes
- Moving a file in the Folder tab to a different disk could follow a link
  instead of moving the link itself, or land inside a folder that had just
  appeared with the target's name. It now lands beside the target first.
- Deleting a link to a key or password file over SSH was refused. It now
  deletes the link, never the file it points to.
- When a copy's name couldn't be claimed, the Folder tab now says why.

## 0.109.0 (2026-10-27)

### New and improved
- Conduit's new Folder tab in Artifacts can search a chat's folder by file
  name and inside files, list its recent files, and make new folders and
  files, rename, move, duplicate, delete and upload, without leaving the
  folder it opened. It never shows, opens, creates or copies files that
  hold keys or passwords, and a save won't overwrite a file that changed
  meanwhile.

## 0.108.0 (2026-10-27)

### New and improved
- Pulse notes when Hermes says it set a reminder, added an event or sent a
  message, and checks a call in that turn did it. Nothing is shown yet:
  `hermes conduit-push pulse claims` lists the ones with none, to judge.

## 0.107.0 (2026-10-27)

### New and improved
- A card you tapped Do it on but never sent, or that Pulse's checks found
  still not done, comes back once, held for your morning brief unless you
  turned next steps up, saying when you tapped Do it. Not if you marked it
  done, put it off or said it wasn't useful.

## 0.106.1 (2026-10-27)

### Fixes
- A reminder on your phone that you kept moving could bring a new Pulse
  card each time. Now it gets one card, and none again for a week, or at
  all once you put it away or said it wasn't useful.

## 0.106.0 (2026-10-27)

### New and improved
- When something gets done or a calendar event ends, Pulse can bring up what
  still needs doing because of it: the hotel once the flights are booked,
  booking the cleaning after the dentist. At most two a report, held for
  your morning brief until you turn next steps up in What Pulse learned.
- A scheduled job that keeps failing no longer counts as taking care of
  something: Pulse brings up what rests on it and says the job is failing.

## 0.105.1 (2026-10-27)

### Fixes
- If Pulse couldn't read its own records just as one of its notifications
  went out, a private one could show its words on the Lock Screen. It now
  says only "Pulse has something for you." then.

## 0.105.0 (2026-10-27)

### Fixes
- Your agents' turns in a room someone else hosts sent you a reply
  notification for a chat Conduit can't open, and a failure notification or
  an alert call when one failed. They don't any more: the room shows them.
  Their approvals and questions still notify.

### New and improved
- When someone you've linked invites you to a room, a Conduit that shows
  room invites gets a Room invite notification, which its Room invites
  setting turns off. It needs the push relay's update (relay 0.21); until
  your relay has it, the invite shows in Conduit without one.

## 0.104.0 (2026-10-27)

### Fixes
- `hermes conduit-push pulse where` could say a place brought up what the
  look ahead saved for it when that never went out, and gave one reason for
  everything still waiting. It now says what happened to each.

### New and improved
- Pulse notifications interrupt only as much as they need to: something
  saved for a place, or that can't wait, comes as time-sensitive (at most
  three a day); one that comes while you're busy, or that can wait, goes
  quietly to Notification Center. Needs the push relay's update.
- A Pulse reminder about a birthday, an anniversary, a gift, a surprise,
  health or money shows only "Pulse has something for you." on the Lock
  Screen, so a surprise stays one. Turn it off with `hermes conduit-push
  pulse private off`.

## 0.103.0 (2026-10-27)

### New and improved
- With Where you are on, Pulse's daily look ahead can save something for
  the right place: "Leaving work: your anniversary is on Wednesday, and
  she loved Kyoto" comes up as you leave work, with a notification, instead
  of in the morning brief. If you don't go there in time, or turn Where you
  are off, it shows on the Pulse screen without a notification.

## 0.102.0 (2026-10-27)

### Fixes
- The Lock Screen could keep showing a finished turn as running, overnight
  even. A turn that ended before Conduit had registered its Live Activity, or
  that Hermes moved to a new session while compacting a long chat, never sent
  the activity its end. Both now end it (a quick turn's end also needs the
  push relay's update).

## 0.101.0 (2026-10-27)

### Fixes
- Turning Pulse off now deletes the arrivals and departures it kept for
  Where you are, as turning Where you are off does. Before, they stayed
  past their day until Pulse came back on. `hermes conduit-push pulse
  where` lists only the last day's, and says when what waited for a place
  was already on its way.

### New and improved
- Pulse brings up what it remembers when it's useful: the morning brief
  mentions what it knows about the people and things in today's events
  ("lunch with Dan at 1: he started the new job on Monday"), and a reminder
  at a place comes with what Pulse remembers about that place. Arriving
  somewhere with nothing waiting doesn't say anything yet: `hermes
  conduit-push pulse where` shows what Pulse would have brought up.

## 0.100.0 (2026-10-27)

### New and improved
- Pulse can know where you are, once you turn on Where you are in Settings >
  Pulse (it's off until you do). Say "next time I'm at Costco, remind me to
  get batteries" and the reminder comes when you get there, or when you
  leave if that's what you said, instead of on a schedule. Pulse's look-ups
  can use where you are too. It needs your location shared in the
  background, never reads a private place, and each read shows in
  Location's read log. From the command line: `hermes conduit-push pulse
  where`.

## 0.99.0 (2026-10-27)

### Fixes
- On Hermes 0.22.3 and later, setting up rooms with people you've linked for
  a profile the dashboard didn't start with, from an older Conduit, restarted
  Hermes from the wrong process, and Hermes came back with that process's
  settings. Now the setup is saved and you're asked to restart Hermes.

### New and improved
- On Hermes 0.22.3 and later, with one dashboard for several profiles, the
  next Conduit can set up any of them for rooms with people you've linked
  and make those rooms for it, not just for the profile the dashboard started
  with.

## 0.98.0 (2026-10-27)

### Fixes
- Pulse's look-ups: one made right at midnight (UTC) could count the day
  before's look-ups against the new day, and a Maps search past a message's
  6 look-ups read your saved places before it was stopped.

### New and improved
- Pulse suggests, you add. When an event or reminder would help, Pulse
  suggests it on a card of its own instead of adding it, saying what it
  would add and to which calendar or list. Add puts it on your phone, Not
  now drops it, and the card shows how it went, with Undo once it's added.
  Pulse makes at most 2 suggestions a message and 5 a day, and nothing it
  does can change your phone without your tap.

## 0.97.0 (2026-10-27)

### New and improved
- When one of your agents in a room someone else hosts asks for your
  approval, it waits for you on your own Hermes, with the room and who hosts
  it, and you approve it once or deny it there. The room's host never sees or
  answers it. From the command line: `hermes conduit-push rooms approvals`,
  `approve` and `deny`.

## 0.96.1 (2026-10-27)

### Fixes
- Updating the plugin works again. Hermes' install scan refused 0.95.0 and 0.96.0, because a test file held something that looked like a password, so hosts stayed on the version they had.

## 0.96.0 (2026-10-27)

### New and improved
- Pulse looks things up before it speaks. Its reports, morning brief and
  follow-ups can read your calendar, reminders, alarms, contacts and phone
  status, and look places up on Maps between places they name, never where
  you are or your saved places. A follow-up about something said in a chat
  can also search the web. Nothing they call can change anything, and each
  card's Why? says what Pulse looked at. Turn it off in Settings > Pulse or
  with `hermes conduit-push pulse look off`.

## 0.95.0 (2026-10-27)

### Fixes
- When you approve Hermes' requests from a Watch Local Live call, the card now stays up as long as Hermes waits (your approvals timeout setting). On current Hermes it always used five minutes.
- Voice on a profile that no longer exists now says so, instead of saying Hermes couldn't check it.

## 0.94.0 (2026-10-27)

### New and improved
- Rooms with people you've linked keep their agents: each agent's own Hermes
  renews its place in the room every 12 hours, so a room that's quiet for a
  day no longer cuts them off.
- Leave a room you joined, with some or all of your agents, or take someone
  else's agent out of a room you host. Their Hermes is told, and the agent's
  turns there stop. Its chat for the room stays until its owner deletes it.
  From the command line: `hermes conduit-push rooms leave` and `remove`.
- Disbanding a room tells everyone linked into it, and their Hermes take
  their agents out of it.

## 0.93.1 (2026-10-27)

### Fixes
- Something a Pulse report found by itself could show as a deadline, and
  rating it changed how loudly deadlines come up. Now only the deadlines
  Pulse memory keeps count as deadlines.

## 0.93.0 (2026-10-27)

### Fixes
- On Hermes 0.22.3 and later, with one dashboard for several profiles, Local
  Live, voice on your own speech server, Grok Live, Watch calls, taking a chat
  over from Hermes Desktop, the chats Desktop opened, and predictions didn't
  work on any profile but the one the dashboard started with. With this
  plugin and the next Conduit, they work on every profile. Hermes now runs
  other profiles' plugins apart from the dashboard, where these can't work,
  so they keep to the dashboard until Hermes can carry them.

## 0.92.0 (2026-10-27)

### Fixes
- On a speech server that only reads text back, tapping Interrupt in a Local Live call didn't always reach the server, so one that keeps its microphone shut while it speaks could miss what you said next.

## 0.90.0 (2026-10-27)

### New and improved
- Pulse keeps the deadlines it finds in what your Shortcuts automations log,
  like a free trial ending, a subscription renewing or a bill due, and brings
  each up a week ahead and the day before, held for the morning brief. A later
  message can move or close one, and so can saying it's done in a chat.
- Deadlines have their own level in What Pulse learned: turn them up there to
  have them pushed. Each is deleted a day after it's done, otherwise about a
  week after its day, and at once when you delete the event it came from. Turn
  it off in Settings > Pulse or with `hermes conduit-push pulse inbox off`,
  which deletes the ones kept.

## 0.89.0 (2026-10-27)

### New and improved
- Rooms with people you've linked: make a room with your agents and theirs,
  and their Hermes shows the invite. They join with their agents, each with
  its choices for the room, or decline, and the room starts on your Hermes
  once everyone has answered, or sooner when you start it. Their agents run
  on their own Hermes, with their own model and keys, and only they answer
  their agents' approvals. From the command line: `hermes conduit-push rooms
  invite`, `invites`, `join`, `decline`, `drafts`, `start` and `withdraw`.
- A profile whose turns run on Codex's app server can't join a room with
  other people's agents, or be in one you make: its tools run where the
  room's rules can't see them.

## 0.88.0 (2026-10-27)

### New and improved
- Pulse remembers birthdays, anniversaries and other dates that come back every
  year, from your chats and from the people you picked in Conduit, and keeps them
  from year to year. Birthdays and anniversaries come up two weeks ahead, the day
  before and on the day; other yearly dates the day before and on the day. What
  Pulse remembers shows each one's day.
- Once a day, an hour before the morning brief, Pulse looks two weeks ahead at
  your calendar, reminders and what it remembers, and brings up what needs doing
  now, like a gift to order or a table to book, with the morning brief. Turn it
  off with `hermes conduit-push pulse ahead off`.
- What Pulse left out says whether a report or the look ahead left each thing out.

## 0.87.1 (2026-10-27)

### Fixes
- News Pulse found on its own, read just before it came back from Not now or went into the morning brief, is no longer read again right after.

## 0.87.0 (2026-10-26)

### Fixes
- A follow-up that couldn't wait for the morning, and went out on a second try after the night, said it was sent at night.

### New and improved
- Pulse notices when something it brought up got done somewhere else. When a chat shows a remembered thing done or called off, its cards move to Done, marked "seen in the chat", and Undo puts them back.
- A reminder Pulse sent can still be closed for two weeks: say you did it in any chat, or in answer to the reminder, and its card moves to Done. Said again, it's remembered anew and gets its own reminder.
- News Pulse found on its own, not from something it remembered, gets its chats read again a day after it came and once more the day after, and before the morning brief or before it comes back from Not now. If you took care of it, it moves to Done.
- Once you Undo one of these, Pulse doesn't move that card to Done again from the same chats.

## 0.86.0 (2026-10-26)

### New and improved
- Pulse keeps a night, 22:00 to 07:00 unless you change it, and sends no pushes then. What would have pushed waits for the morning brief, unless it can't wait, like a reminder due before morning. A Focus that holds Hermes's calls holds Pulse's pushes too.
- A follow-up waits until you're out of a calendar event or done driving, and shows in Pulse without a push if that takes over three hours.
- A follow-up that needs an office open waits for office hours, and one for the morning or the evening waits for then. Pulse checks it again before it goes, and its Why? says what it waited for.
- A new reason for a thumbs down, Wrong time: the card comes back at a better time, and Pulse stops pushing that kind at that time of day. What Pulse learned lists it, and Undo takes it back.
- Not now's "later today" ends when your night starts.

## 0.85.0 (2026-10-26)

### New and improved
- One command gets your Hermes ready for rooms with other people's agents:
  `hermes conduit-push rooms setup` turns on Hermes' API server (with a new
  key if it has none), saves the tools rooms may use (web, vision, X search
  and past-chat search, unless you pick others with `--ceiling`), keeps
  plugins and MCP servers added later out of rooms, keeps approvals on, and
  restarts the gateway. It says what it will change and asks first.

## 0.84.2 (2026-10-26)

### Security
- Someone's invite or accept can't point your Hermes at a service on your
  computer or network by naming the mailbox with a host name that leads
  there: the name is looked up before anything is sent, and refused as such
  an address already was.

### Fixes
- An accept that reached the mailbox in time is answered even when its
  invite ran out while Hermes was busy with the mailbox, or by a mailbox
  clock a few minutes behind yours. Before, the invite could be cleared
  first and the accept lost with it, leaving the other person waiting.
- Cancelling an invite as it was being made, or unlinking while Hermes set
  its inbox up again, no longer leaves a leftover at the mailbox taking one
  of the inbox's 64 places.

## 0.84.1 (2026-10-26)

### Fixes
- A Hermes that hosts two group rooms or more no longer keeps checking them over and over after a message, using CPU until Hermes restarted.

## 0.84.0 (2026-10-26)

### New and improved
- Pulse shows what it learned from your thumbs, Do it and Done: which chats, kinds and timings it brings up more or less, and which kinds it pushes louder. Reset any one of them, or everything, and undo a reset if you change your mind. A card's Why? also says when what Pulse learned helped bring it up.
- Pulse lists what its latest reports left out and why, such as a chat you muted or something it learned you skip, and you can bring any of them up as a card.

## 0.83.1 (2026-10-26)

### Fixes
- Asking for a chat on an Apple Watch call could find a Kanban task's worker,
  which isn't a chat you can pick up. Calls now leave workers to the board.

## 0.83.0 (2026-10-26)

### New and improved
- Your Hermes can link with someone else's, the first step to rooms where
  your agents and theirs talk: `hermes conduit-push links invite --name
  <you>` makes an invite for one accept within a day, and they accept it
  with `hermes conduit-push links accept`. Both Hermes show four words to
  compare in person, and `links list` and `links remove` show and end Links.
  Links go through the rooms mailbox; nothing travels over them yet.

## 0.82.0 (2026-10-26)

### New and improved
- When Hermes needs several details only you can give, like where you leave from and your budget for a trip, it can ask them all at once, and Conduit shows them as a form: fill in what you know, leave the rest blank, and send it once. On a call from Hermes, say "skip" to leave one blank.

## 0.81.3 (2026-10-26)

### Fixes
- After a plugin update, Pulse no longer says the dashboard is running an older copy when it isn't. The update restarts the dashboard, but the new one waited up to two minutes for the old one's hold on Pulse to run out, and Settings > Pulse and `pulse status` meanwhile read that hold as old code still running. The restarted dashboard now takes Pulse over at once.

## 0.81.2 (2026-10-26)

### Fixes
- A call Hermes places from a scheduled (cron) job now rings. Hermes runs those jobs in a worker that exits as soon as the job ends, and that cut off the call request on its way to the relay, so nothing rang and no missed call was reported. The same could drop a job's ordinary notification.

## 0.81.1 (2026-10-26)

### New and improved
- Pulse learns from what you do with its cards, not only from thumbs: Do it counts by how far it got (opened, sent, done), and Done counts too. It learns which chats, kinds of things and timing you find useful, so a chat you keep turning down goes quiet after a few thumbs down in a row, a thumbs up brings it back, and what's due or overdue still comes up. A push you act on within the hour makes that kind louder. Thumbs no longer move a whole kind between a push, the Pulse screen and the brief: the level you set is the loudest Pulse gets, and levels two thumbs set before go back to yours, those thumbs teaching Pulse instead. A thumbs down on a report card can say why too, and the reason acts at once for that kind of thing. Conduit from the `pulse` branch asks for it there and says what changed under What Pulse learned.

## 0.81.0 (2026-10-26)

### Fixes
- In a group room with another Hermes's agent, a member whose Hermes no
  longer accepts the room (its access was revoked or ran out) no longer holds
  the room up: its turn fails once with a reason, and the others carry on.
- Disbanding a group room now deletes your agents' hidden chats for it on
  your host, as Conduit's warning says. Before, they stayed.

### New and improved
- `hermes conduit-push rooms forget --room <id>` deletes your agent's hidden
  chat for a room someone else hosted, once you no longer want it.

## 0.80.2 (2026-10-26)

### New and improved
- Pulse has quiet times: pick the weekdays it keeps to itself, set it quiet until a day for a vacation, or make it quiet for the rest of today, from a Pulse notification too. While it's quiet, Pulse sends no report, delivery check or morning brief on its own, and doesn't send one it skipped late once the quiet ends; a follow-up due then waits for the first morning brief after, as a card on the Pulse screen meanwhile. Memory keeps reading your chats, and a report you ask for still goes. Needs Conduit from the `pulse` branch to set them.

## 0.80.1 (2026-10-26)

### New and improved
- Why? on a card from your Pulse report now says why that thing came up now and where it came from: the chat, calendar entry, reminder, note or job it rests on. Before, every report card gave the same stock line, and those lines are cleared from earlier cards. The report itself is shorter, each line without the reason in brackets. Needs Conduit from the `pulse` branch to show it.

## 0.80.0 (2026-10-26)

### New and improved
- Conduit's Predictive settings can suggest your next message after a
  reply, offer words for a draft when you pause typing, and let Hermes
  follow up on its own while you look at a chat. All are off until you
  turn them on. Hermes answers on a copy of the chat that can't use tools
  and saves nothing, and only the device that asked sees the answer, so
  Hermes Desktop shows no notes for them. A faster model can answer
  instead: set `auxiliary.conduit_predict` (provider and model) in the
  profile's config.

## 0.79.0 (2026-10-26)

### New and improved
- In a group room that includes another Hermes's agent, your agent now
  follows your rules for that room, on your own host: by default it talks
  without tools, asks you before sharing anything personal, can't search your
  past chats, and saves nothing from the room to its memory or skills. It's
  told who's asking, so other people's requests can't change what it shares,
  and anything in its replies that looks like a password or key is removed.
  Past-chat search no longer finds those rooms. Rooms of only your own agents
  work as before. If Hermes can't read its chat records for a moment, tool
  calls are blocked until it can, so a room's rules are never skipped.

## 0.78.3 (2026-10-26)

### New and improved
- Not now asks until when: later today, tomorrow or next week (those two in time for that morning's brief). The card leaves the list and comes back as new at that time, at the top, unless the thing got done meanwhile: one about something Pulse remembers has its chat read first, and a thing that got done is marked done instead. Needs Conduit from the `pulse` branch; an older app keeps the plain Not now.

## 0.78.2 (2026-10-26)

### New and improved
- A note to Pulse, in your own words: what to always bring up, what to leave out. Settings > Pulse has it (Conduit on the `pulse` branch, #1277), and so does `hermes conduit-push pulse note "…"`. Pulse reads it before every report, morning brief and reminder; where it says what to bring up or leave out, it comes before Pulse's own rules.

## 0.78.1 (2026-10-25)

### Fixes
- A bot's Bot Chat now shows what it made in Artifacts, both in the chat's
  own list and on the Artifacts page. Before, pictures and files a bot sent
  in its Bot Chat never appeared there.

## 0.78.0 (2026-10-25)

### New and improved
- On an Apple Watch call, your requests now stay in one Hermes chat: the
  first one starts it, and later ones continue it, so Hermes remembers what
  you asked before. Work you ask for in the background, quick requests and
  requests for another profile still get their own chat.

## 0.77.7 (2026-10-25)

### Fixes
- With Settings > Pulse > Model on "Same as the Pulse chat", Pulse's memory,
  report lists and follow-up checks ran on Hermes's main model instead of the
  Pulse chat's, so they failed whenever the main model was busy and reports
  went out without their list. They now run on the Pulse chat's model,
  unless Hermes's "Pulse memory" model is set to another.

### New and improved
- `hermes conduit-push pulse status` says which model Pulse runs on.

## 0.77.6 (2026-10-25)

### Fixes
- A Bot Chat kept saying it was open in another Hermes window while the bot
  answered a message from another bot, and Take over never cleared it.
  Conduit now says the bot is busy and sends your message when it finishes.

## 0.77.5 (2026-10-25)

### Fixes
- A group room could stop answering altogether once one of its threads
  passed about 24 messages. Hermes could no longer plan the room's next
  turn, so no member replied again, in that thread or any other, and nothing
  said why. Rooms now keep answering however long their threads get.

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
