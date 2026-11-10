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

## 0.154.0 (2026-11-10)

### New and improved
- A Bot Chat keeps its workstreams working together. It sees what each one
  found, relies on and changed, and what you told it in its own chat, and
  acts when one affects another. Under Edit Bot > Work > Coordination, Ask
  (the default) has it ask you before it starts, stops or redirects work on
  its own, Automatic lets it, and Off keeps it as it was.
- A work update the bot has nothing to tell you about stays silent.
- Leave alone on a workstream's card keeps the bot from sending to it or
  stopping it on its own.

## 0.153.8 (2026-11-10)

### Fixes
- Pulse no longer reads a subagent's chat as if you wrote it, so a task a
  bot handed to a subagent no longer comes up as "You requested…". What it
  already learned that way is forgotten once.

## 0.153.7 (2026-11-10)

### Fixes
- A script's `hermes chat --source tool` run, a one-shot `hermes chat -q`
  and a Kanban worker no longer send a reply, failure or background-task
  notification or show Lock Screen progress. Their answers go to whoever
  ran them; one they write with `conduit_notify` still comes.

## 0.153.6 (2026-11-10)

### Fixes
- A bot whose tool call carries arguments that aren't valid JSON is told
  what's wrong with them, instead of being refused as if it had asked for
  nothing.
- A workstream's update that Hermes took too long to accept still counts
  as Conduit's own turn, so its review card isn't refused when only the
  desktop app has the Bot Chat open.
- A review of seven or eight options from a workstream with a long name
  keeps "(more)" on its second half, so the two halves read differently.

## 0.153.5 (2026-11-10)

### Fixes
- A message a Bot Chat sends to a workstream is no longer sent a second time
  after a restart when Hermes had saved it joined to another message, such
  as your own words sent at the same moment.

## 0.153.4 (2026-11-10)

### Fixes
- On a Hermes newer than 0.23.0, your first message after a Pulse report no
  longer hands the report to your memory provider as your last message.

## 0.153.3 (2026-11-10)

### Fixes
- A Pulse card keeps a short sentence that adds one word to its headline,
  such as "today" or a name, instead of dropping it as a repeat.
- A Pulse date card no longer says whose day it is as "The user's".

## 0.153.2 (2026-11-10)

### Fixes
- Pulse cards that Pulse's learning brings up on its own get the same
  headline, facts and buttons as the rest, instead of plain text.
- Pulse cards no longer repeat their headline in the sentence under it,
  draw a due bar that starts and ends on the same day, or show "your"
  where a date names whose it is.

### New and improved
- Pulse asks for shorter headlines, a named first step instead of a bare
  "Do it", and a job's run history when it has one.

## 0.153.1 (2026-11-09)

### Fixes
- A workstream's update that waits behind a long turn still shows its cards
  in the chat when it starts, even if only Desktop has the chat open then.
- Two identical workstream updates sent close together each count as the
  plugin's own, not just the first.

## 0.153.0 (2026-11-09)

### New and improved
- Local Live's agent mode now works on speech servers that only read text
  aloud, like a self-hosted voice adapter: your fast model answers each thing
  you say and hands real work to Hermes as a job, so replies start sooner
  instead of waiting for a full Hermes turn.

## 0.152.0 (2026-11-09)

### Fixes
- An iPad or iPhone still on an older Conduit, opened in the last few days,
  no longer keeps newer cards (like review cards) from showing on the device
  you're using. The older one shows less until you update it.
- When a bot's tool call can't be read, it now hears exactly what was wrong
  with it, so it can fix the call instead of giving up on the card.
- A card your Bot Chat shows when a workstream reports back is no longer
  refused because Hermes Desktop or a browser had the chat open at the time,
  and that update's reply no longer counts as sent from Desktop (for its
  notification and for calls held while you're at a screen).
- With a chat open in Conduit and in Hermes Desktop at once, a message from
  the phone that has it on screen gets cards again: the bot is told to draw
  them, and hears which Conduit screen you wrote from.

## 0.151.0 (2026-11-09)

### New and improved
- When a bot writes a card out as text in its reply instead of showing it
  (some free and smaller models do), the plugin now shows the card in its
  place, and Conduit draws it under the reply. Needs a Conduit build with
  this change; older ones show the reply without the markup.

## 0.150.4 (2026-11-09)

### Fixes
- When a workstream reported seven design options, the review card the
  plugin built from them showed only six. It now splits seven or eight
  options into two halves, so every one shows.

## 0.150.3 (2026-11-09)

### Fixes
- A bot could write the review card for its workstream's options out as
  text in its reply instead of showing it. The plugin now builds that card
  from the workstream's report, so the bot shows it with one short call,
  and a later round stays the same review.

## 0.150.2 (2026-11-09)

### Fixes
- Options a bot's workstream made for you to pick from (designs, layouts,
  pictures) came back as links to files instead of a card. The Bot Chat now
  shows them as a review card you mark, and your marks go back to that
  workstream.

## 0.150.1 (2026-11-09)

### Fixes
- In a group room your Hermes hosts, tapping a bot's notification opened
  that bot's hidden chat for the room instead of the room. Replies, failed
  turns, finished background tasks and approvals now open the room, titled
  with its name, and so does an approval's row in Needs you. Needs the
  latest Conduit.
- In a room that routes through its lead, the lead's pick of who answers
  sent a notification of its own, showing raw JSON. It no longer does; the
  reply from whoever takes the message still notifies.

## 0.150.0 (2026-11-09)

### Fixes
- A report could come out as a card with no buttons, about something a
  follow-up card already showed. Every report card now comes from Pulse's
  checked list, with its own buttons, and never repeats a card still open.

### New and improved
- Pulse cards can show what they're about: a date, a deadline, a job's
  recent runs, a promise in the words it was made, a status or the steps of
  something underway, with up to two buttons. Ask puts words in the chat for
  you to edit and send. The latest Conduit draws them; older apps show the
  cards as before.
- When something Pulse is following changes (a job fails again, or
  recovers), its card is updated or closed instead of a new one appearing.
  If it happens again after it was over, that's a new card.
- The morning brief comes in parts the app can lay out: a summary, what's
  on today, what's still open, and how many of each.

## 0.149.1 (2026-11-09)

### Fixes
- In a room hosted on a Windows computer whose Hermes lives on a network
  share, Conduit on another host showed the room's pictures and files by
  name instead of drawing and opening them.

## 0.149.0 (2026-11-09)

### New and improved
- In a Linked Room, every bot sees the pictures and files sent into the
  room, whoever's host it runs on: each file goes to the guests' hosts,
  and a guest's bot reads its own host's copy.
- Bots in a room with other people's agents may look at a picture sent
  into the room, whatever else the room's rules allow them.
- In a room someone else hosts, Conduit draws its pictures and opens its
  files from your own host's copy, instead of showing them by name.

## 0.148.1 (2026-11-09)

### Fixes
- A review's next round no longer loses what you marked when Hermes shows
  it without the decisions' ids: a decision with the same title keeps its
  marks, and a new one never takes an old one's place.
- A resent review is no longer cleared, and Hermes shows one round of a
  review at a time.
- Hermes no longer reads a review you marked only partly as fully marked.

## 0.148.0 (2026-11-09)

### New and improved
- A bot's Bot Chat now does quick things itself with any of its tools, and
  hands work to the background only once it takes more than a few steps.
  Simple asks are answered where you're reading instead of in a hidden chat.
- Cards, Apple Maps and the password vault's prompts always show in the Bot
  Chat, whatever its setting.
- A new setting, Everything Here, has the Bot Chat do all of it itself and
  start background work only when it's long or you ask.

## 0.147.2 (2026-11-09)

### Fixes
- On a host that manages the plugin, Finish Update could leave the gateway
  on the old plugin where systemd runs it as root (an LXC host). The host
  now runs those restarts itself, as an update does, and still pulls
  nothing.

## 0.147.1 (2026-11-09)

### Fixes
- With a memory provider such as Hindsight or Honcho, every Pulse report,
  brief and delivery check was saved and recalled like your own messages,
  and a provider that reprocesses the whole chat each time could cost
  dollars a day. Pulse's turns now stay out of the memory provider; what
  you write in the Pulse chat is saved as before.

## 0.147.0 (2026-11-08)

### New and improved
- Hermes can show you options to choose between when they're easier to see
  than to describe: layouts, colours, charts, posters. Tap the one you like
  right in the chat, or open a review to mark several choices Approve,
  Change or Skip with notes, and Hermes comes back with the next round.
- In Bot Mode, a workstream draws the options and your Bot Chat shows them:
  your marks go straight back to that workstream, every round.

## 0.146.0 (2026-11-08)

### New and improved
- A host that installs and upgrades the plugin itself can say so with
  CONDUIT_PUSH_MANAGED. Conduit then stops asking to update the plugin on
  that host, and an update started from Conduit is refused, so the host
  keeps the version it was tested with.

## 0.145.0 (2026-11-08)

### New and improved
- When your agent reads a room with other people for you, a message's
  pictures and files are named in it, and a message of files alone is
  read too, now that Conduit sends them as the room's own attachments.

## 0.143.1 (2026-11-08)

### Fixes
- Pulse made two or three copies of the same card from one check, and the
  morning brief came more than once: on a host with profiles, each profile's
  copy of the plugin ran Pulse. Now only one does.
- Pulse cards came back after you marked them Done: one oddly saved date
  made every check skip looking at earlier cards. Dates saved like that no
  longer stop any of Pulse's checks, and a card you marked Done or put away
  keeps the same words from coming back for a month (one about a reminder,
  a calendar entry or something Pulse remembers keeps that thing's rules).

## 0.143.0 (2026-11-08)

### New and improved
- Files sent into a group room (PDFs, text, pasted text, audio, video)
  open, preview and play in Conduit as they do in a chat, instead of
  showing as a name you can't tap. In a room someone else hosts, they
  still show by name.

## 0.142.0 (2026-11-08)

### New and improved
- A picture sent into a group room shows as the picture in Conduit,
  instead of a file icon and its name. In a room someone else hosts, it
  still shows by name.

## 0.141.6 (2026-11-08)

### Fixes
- Cards a workstream drew before you stopped it never reached its Bot
  Chat, because a stopped turn sends no update. The bot's next update
  from it now names them, so they're shown in the Bot Chat.

## 0.141.5 (2026-11-08)

### Fixes
- A bot asked for a map or the nearest shop in its Bot Chat handed it to a
  workstream, and the map it drew stayed in that workstream's chat. With
  "Also Looks Things Up" on, the bot now looks places, directions and
  travel times up itself, and cards a workstream draws (maps, forecasts
  and the rest) are shown again in the Bot Chat.

## 0.141.4 (2026-11-08)

### Fixes
- In a room with linked people, @mentioning one bot got you an answer from
  it, but its reply often tagged another bot, and Hermes gives a turn to
  any bot that's tagged, so both answered anyway. A bot you @mention now
  answers you itself and tags another only when you asked for them.

## 0.141.1 (2026-11-08)

### Fixes
- A bot's work kept showing "Needs you" after you answered its question in
  the Bot Chat or in the reply field on its card: the answer waited behind
  the question instead of answering it. Your answer now answers the
  question, and the work carries on. The bot passes on only what you said
  in that chat, never an answer of its own.

## 0.141.0 (2026-11-08)

### Fixes
- On a host whose dashboard runs on its own beside the gateway, a room with
  linked people stopped working once the gateway restarted: Hermes moves
  such rooms into the gateway, and the plugin kept using the old, frozen
  copy. The other people's copies stopped getting new messages, and their
  agents' turns were refused. Rooms now follow Hermes into the gateway, the
  gateway takes the rooms' mail, and the other people's agents get grants
  for the room as it goes on. New rooms start in the gateway whenever it
  runs.

## 0.140.3 (2026-11-08)

### Fixes
- A card's choice field that listed the same option twice (Yes and yes)
  reached the phone with both, so its picker couldn't tell them apart. It
  now keeps one of each, and a choice left with fewer than two options is
  dropped.

## 0.140.2 (2026-11-08)

### Fixes
- A card's date field holding a day that doesn't exist, like 31 February,
  showed today but sent the impossible date; now it starts empty.
- A card field asking for a PIN code or an MFA code, or a PIN written next
  to Chinese or Japanese text or in full-width letters, wasn't refused as
  a secret.
- A tap on a card showing a long diff could be turned away as too large.
- A tap whose message didn't go after all could be heard by a later message
  starting with the same words; now it's forgotten.

## 0.140.1 (2026-11-08)

### Fixes
- When a send failed just as its "sent from your iPhone, spoken" note was
  still on its way, the note could land afterwards and label the next
  message with the same words from another device. It's dropped now.
- A bot driving its screen rewrote the screen's hold file on every click;
  it now renews the hold every 30 seconds, and the wait for a free screen
  reads it once a look.
- A Linked Room's work ask that the owner left unanswered kept the host
  reading it every 5 seconds for up to a day; after two minutes it's read
  every 30 seconds, without locking the file.

## 0.140.0 (2026-11-08)

### New and improved
- Hermes can build its own cards for anything the ready-made ones don't fit,
  like an email ready to send, a day's agenda or an idea to act on: text,
  numbers, tags, pictures, progress bars, fields to fill in and buttons.
- Any card can carry buttons that open a link, Maps, Calendar or Reminders,
  copy text, send a message, or ask Hermes to do something. A tap reaches
  Hermes as your message, with what the card showed and what you filled in,
  and the card then shows how it went. A button that sends, buys or deletes
  asks you first.
- Hermes can save a card it builds as a template and fill it by name later,
  so cards it shows often come up faster.

## 0.139.3 (2026-11-08)

### New and improved
- Your agents now point you to Settings › Multiplayer (Conduit's new name
  for People) when they mention the people you've linked with.

## 0.139.2 (2026-11-08)

### Fixes
- A bot with workstreams on couldn't show cards in its Bot Chat: asked for
  a card, it started a workstream, whose card showed only in the
  workstream's own chat. The Bot Chat now draws its own cards.
- A Bot Chat looking something up couldn't read the rest of a long web page
  it had just fetched, so it answered from the start of the page.
- A chat open in Conduit on two devices at once didn't get Conduit's cards
  (the weather, a plan, a chart) or know which device sent the message, so
  answers came as plain text.

## 0.139.1 (2026-11-07)

### Fixes
- On a gateway's own listener (the Webapp), a Watch job, Pulse or a Watch
  call whose turn a gateway restart cut off held every later message in that
  chat until someone discarded it. The plugin now lets go of its own cut-off
  turn before its next message. A turn you sent yourself is left for you, and
  Conduit shows it so you can resend or discard it. A turn cut off before
  this update is left for you too.

## 0.139.0 (2026-11-07)

### New and improved
- Linked Rooms: everyone in a room now sees the other people's agents with
  their own pictures and glow colors, and each person's picture by their
  messages, as their owner's Conduit shows them.

## 0.137.5 (2026-11-07)

### Fixes
- Updating the plugin from Conduit, or setting up Linked Rooms, could leave
  the gateway on the old plugin on a host where systemd runs it as root (an
  LXC host, say): Hermes' own restart stopped with "Refusing to install the
  gateway system service as root". The plugin now restarts the gateway
  through systemd there.

## 0.137.4 (2026-11-07)

### Fixes
- With step summaries on in Conduit, Hermes Desktop showed each summary's
  long "Look back at the tool calls…" question and its answer as a btw note
  in the chat. Conduit now asks the plugin, which answers only the phone
  that asked.

## 0.137.3 (2026-11-07)

### Fixes
- A bot that wrote a link to its work in its own words (Zhongli's "W7:
  Provision the LXC") showed a plain link in its Bot Chat instead of the work
  card. The plugin now puts the link back as Conduit and Hermes Desktop read
  it.

## 0.137.2 (2026-11-07)

### Fixes
- The Hermes dashboard no longer reads all of the plugin's code each time it
  lists its plugins. That took seconds on a server and minutes on a phone,
  enough to freeze the dashboard and drop Conduit's connection.

## 0.137.1 (2026-11-07)

### Fixes
- Tapping a file link in a project chat now opens the file when the link
  starts at the project's top folder and the chat worked in a subfolder.
  Before, Conduit said the file wasn't on the server.
- A file link in a chat with no folder of its own is also looked for in your
  terminal's working folder, where Hermes wrote it.

## 0.137.0 (2026-11-07)

### Fixes
- A work update a group room couldn't take now reaches your Bot Chat even if
  Hermes restarts at just the wrong moment.

### New and improved
- In a Linked Room, a room with other people's agents, you can now turn on
  Run work for your bot. It can then start work for the room, and each
  start asks you on your phone first: Start, change the words, or Don't
  start. In a call, say "start it": a plain yes doesn't start work. What the work finds reaches your bot privately, and it shares only
  what your rules for that room allow. You turn it on for each room: a
  new room never starts with it on.

## 0.136.0 (2026-11-07)

### New and improved
- A bot in one of your group rooms can now start work from the room, like
  making an image you asked for there, instead of asking you to repeat it in
  its Bot Chat. The work shows in its Work panel as usual, and when it's
  done the result is posted back in the room and the bot passes it on.

## 0.135.0 (2026-11-07)

### Fixes
- A plugin update or gateway restart that met a running task said only
  "Letting running tasks finish…", for up to half an hour, without naming
  the task. Conduit now shows each one (which bot's chat, its title, how
  long it has run) and when the gateway restarts anyway.
- The update waited up to 30 minutes for running tasks before it even sent
  the restart, and then the gateway could wait 30 more. Its own wait is now
  10 minutes.

### New and improved
- Restart now, under that list, stops the chats it names and restarts the
  gateway at once. Work Conduit can't stop, such as a scheduled job, is
  ended by a forced restart through systemd.

## 0.134.10 (2026-11-07)

### Fixes
- A call could open with "pick up and I'll tell you more", which makes no
  sense once you've answered. The call now says the news itself in that
  first sentence.

## 0.134.9 (2026-11-06)

### Fixes
- Asking Hermes to call you while calls are off for that profile now gets a
  clear answer and a link to turn them on. Before, Hermes said it couldn't
  call and set up a call watch to ring instead, which checked every 5
  minutes and couldn't ring either.

## 0.134.8 (2026-11-06)

### Fixes
- On a Local Live call with a speech server that reads text aloud, a filler
  the server says on its own while Hermes works ("let me look that
  up") was dropped, so you heard silence instead. You now hear it, Hermes'
  answer plays once it ends, and talking over it or tapping Stop stops it like
  the rest of the reply.

## 0.134.7 (2026-11-06)

### Fixes
- Maps lookups from or to a saved place, such as Home, no longer give
  distances to the metre or turn-by-turn steps, which together could show
  where the place is. Distances are rounded to 100 m (with Conduit's next
  update), and the maps tool tells the agent so.

## 0.134.6 (2026-11-06)

### New and improved
- When Local Live's fast model can't be reached, the call now says so as
  itself ("this one will take me a little longer") instead of saying it's
  passing your request to Hermes.

## 0.134.5 (2026-11-06)

### Fixes
- Updating the plugin from Conduit works again on a host where Hermes was
  updated while its dashboard kept running. Before, the update failed with
  "'PythonDeclaration' object has no attribute 'installs_packages'" until the
  dashboard was restarted by hand.

## 0.134.4 (2026-11-06)

### Fixes
- A workstream that needs you on the bot's screen now always says so to its
  Bot Chat, also when its chat was compacted during that turn. Before, the
  Bot Chat could hear only that it finished.
- A workstream's card keeps showing that it drives the screen after its
  chat is compacted.
- When many chats wait for the screen at once, a workstream is no longer
  told it will hear when the screen is free unless it's on the list, and one
  bot's waiters no longer push another bot's off a shared host's list.
- A workstream that couldn't be told the screen is free (its bot was at its
  limit of running work) is tried again after a growing pause, not every
  few seconds.

## 0.134.3 (2026-11-06)

### Fixes
- In a Linked Room whose Group Settings pick who answers (a lead bot, or
  automatically), a post from someone linked now gets routed like your own
  messages. Before, only their first post was, and every later one went to
  everyone.

## 0.134.2 (2026-11-06)

### Fixes
- Pulse no longer brings up apps you haven't connected in Hermes (a calendar,
  reminders, mail or any other): leaving one unconnected is your choice, so it
  stops sending a card about it every day, and briefs no longer say they may
  be missing something because of it. Cards like that from the last three
  days go away at their next daily check.

## 0.134.1 (2026-11-06)

### Security
- A card no longer draws hidden characters that make text read in a
  different order than it holds, like the Trojan Source trick in code. In a
  diff line each one now shows as a � mark, so the review and Copy Changes
  show it's there; in titles and other text they're dropped.

## 0.134.0 (2026-11-06)

### New and improved
- One of a bot's chats drives its screen at a time: while a workstream, the
  Bot Chat or another chat is clicking on it, the others wait their turn
  instead of fighting over the mouse, and a workstream that waited hears
  when the screen is free. Conduit shows who has the screen now.
- A workstream that needs you on the bot's screen (a sign-in, a 2FA code, a
  CAPTCHA) says so on its card in Conduit, with the screen one tap away.

## 0.133.4 (2026-11-06)

### Fixes
- Pulse's reports and morning brief no longer come back as "Context was
  compacted. The previous response is complete — awaiting your next
  message." When Hermes compacts Pulse's chat as a report starts, the report
  still gets written, and the cards and pushes that only said that are gone.
  A bot woken in its Bot Chat as Hermes compacts it gets to work rather than
  answering with that line.

## 0.133.3 (2026-11-06)

### Fixes
- A tapped file name now opens the file when its folder or file name has an
  apostrophe in it, like `John's Projects`. On an SSH profile, a tap on a file
  in a folder with brackets in its name no longer opens a wrong file of the
  same name.
- A tapped file name opens the right file when a folder in its path ends in
  a comma, semicolon, bar or space, rather than a file of the same name
  elsewhere (not on an SSH profile, whose files aren't checked).

## 0.133.2 (2026-11-05)

### New and improved
- Notifications, Live Activity updates and answers to Hermes' questions
  reach the relay sooner: the connection to it stays open between them
  instead of being set up again every time.

## 0.133.1 (2026-11-05)

### Fixes
- A diff card names a file with accents or other non-English letters as
  it's spelled, like `café.txt`, where it showed escape codes before.
- When cards can't be drawn, their text version no longer ends inside an
  unclosed code block when it's cut to fit, so a long diff or code sample
  shows whole lines and the rest of the reply reads as text.

## 0.133.0 (2026-11-05)

### New and improved
- Hermes can show changes as a diff card: a review, a commit, a config it
  edited or two versions of a text, with each file's added and removed
  lines, line numbers and colors. Long diffs stay on your host and open a
  file at a time, so they don't slow the chat down.

## 0.132.2 (2026-11-05)

### Fixes
- One reading the host can't keep, like a sleep or workout that ends
  before it starts or runs past a week, no longer stops a whole health
  sync, and with it every good reading in it. The host leaves that one out,
  counts it and keeps the rest. Calendar events, reminders and arrivals at
  a saved place work the same way.

## 0.132.1 (2026-11-05)

### Fixes
- A tapped file name now opens the file when its folder or file name has
  spaces or brackets in it, like `My Project` or `Report (final).pdf`, and
  when the name ends in punctuation. Before, Conduit said it wasn't on the
  server.

## 0.132.0 (2026-11-05)

### New and improved
- Tapping a file a reply names by its name, like `AppState.swift`, opens the
  file Hermes actually worked on, found from the chat's own steps, instead
  of saying it can't be found.

## 0.131.3 (2026-11-05)

### Fixes
- In a typed chat, Hermes could say something was on your screen when
  nothing was shown: text it put up for a call's screen waited for a call
  that wasn't open. Hermes now hears to write it in its reply, and cards it
  shows in a typed chat stay in the chat.

## 0.131.2 (2026-11-05)

### Fixes
- Updating the plugin from Conduit while a chat was running left Hermes
  unreachable for up to 30 minutes, and the update said the gateway didn't
  restart. The update now waits for running chats to finish while the
  gateway keeps serving, then restarts it. A restart that still has to wait
  shows as waiting rather than failing.

## 0.131.1 (2026-11-05)

### Fixes
- Pulse could tell you work hadn't started when it had already finished in
  a chat: after a first step got done, the rest of that chat was hidden from
  its report, so it read the silence as "not started". Pulse now reads what
  the chat said after each step got done, and never claims something hasn't
  happened just because it can't see it.

## 0.131.0 (2026-11-05)

### New and improved
- Conduit can show every chat waiting on your approval or answer, in every
  profile: a count on the profile strip, a banner when another chat starts
  waiting, and a Needs you list that opens each one at its card.

## 0.130.5 (2026-11-05)

### Fixes
- Pulse could remember the same thing twice and bring it up twice: when you
  said it in two profiles' chats, said it again in other words around it, or
  it was noted once as something you want to do and once as something you're
  considering. It now keeps one, and brings the one it has up to date.

## 0.130.4 (2026-11-04)

### Fixes
- A GPT-Live call that should open with a greeting, or a call from Hermes,
  started in silence: OpenAI's model no longer speaks first when told to, so
  only Conduit's second ask a few seconds later got it talking, and on the
  Apple Watch nothing did. Conduit now opens these calls itself, so the
  greeting comes within a second.

## 0.130.3 (2026-11-04)

### Fixes
- Pulse read a message you sent with a picture as if you had also written
  your recalled memories and Conduit's notes for that turn, which Hermes
  stores after the picture. It now reads only what you wrote.

## 0.130.2 (2026-11-04)

### Fixes
- A Bot Chat handed its memory provider's tools, such as Hindsight's
  retain and recall, to a workstream. It now calls them itself, as it does
  Hermes' own memory.

## 0.130.1 (2026-11-04)

### Fixes
- A resolved workstream's link didn't open in Hermes Desktop ("Session
  owner could not be resolved"). Resolving it now shows its chat in
  Desktop's Sessions list, and work resolved before is shown once.

## 0.130.0 (2026-11-04)

### Fixes
- An old copy of Conduit you no longer open, on another iPhone or iPad,
  kept Hermes from drawing boards and cut tables, timelines and tabs from
  them. Now only the Conduits you've used in the last three days count.
- With Tool Search on, Hermes often answered in text, the weather for one,
  without looking for the board tool. It's now told where to find it.

### New and improved
- Hermes Desktop can draw the boards Hermes shows, as cards in the reply,
  once you turn on "Conduit boards" in Desktop's Capabilities › Plugins.
  Without it, Desktop keeps getting the board as text.

## 0.129.1 (2026-11-04)

### Fixes
- A bot that passed on a workstream's link in its own words left out the
  part Hermes Desktop opens, so the workstream couldn't be opened there.
  Bots now give the link as written, outside any code block.

## 0.129.0 (2026-11-04)

### New and improved
- Boards in group rooms: cards an agent shows in a room on your Hermes
  appear under its message there, instead of as text.

## 0.128.0 (2026-11-04)

### New and improved
- Boards: Hermes can show tables you can sort, day-by-day plans,
  checklists, ranked lists, progress toward goals and tips or warnings as
  cards in the chat, up to 12 at once, as tabs when there are many, with
  follow-up buttons that ask the next question in a tap.
- Checklist ticks are kept on your Hermes, so every one of your devices shows
  the same ones, and Hermes hears what you ticked at your next message.
- When part of a card can't be shown, Hermes is told exactly what and why,
  so it can fix it and show it again; and every card set carries its text,
  which Conduit shows in a card's place whenever it can't draw it.
- Outside Conduit (Hermes Desktop, a messaging app) and in group rooms, Hermes gets the cards
  as text to put in its reply instead of an error.

## 0.127.6 (2026-11-03)

### Fixes
- Pulse: on a Hermes older than v0.22.0, Pulse could be on and never run,
  and Settings asked for a restart that didn't help. It runs there now, and
  a Hermes that can't run it says so in Settings and in its log.

## 0.127.5 (2026-11-03)

### Fixes
- A host whose clock ran more than an hour fast sent every encrypted
  notification as a plain "Hermes" banner that opened nothing, with no sign
  why. It now dates them by the push relay's clock and says in its log how
  far off the host's clock is.

## 0.127.4 (2026-11-03)

### Fixes
- Bot Mode: on Hermes Desktop, which doesn't list workstreams, there was no
  way to open one or answer its approval. The Bot Chat's links to its work
  now open the workstream in Desktop too, and a workstream waiting on you
  comes with its link.

## 0.127.3 (2026-11-03)

### Fixes
- Hermes could say it can't call you, set an alarm or add a reminder, even
  with Conduit set up for it: each Conduit tool now opens its description
  with what it does, short enough for Hermes' tool list to show it whole.
- Asked to call you when something happens later ("call me when the results
  are certified"), Hermes could set up a job that only sends a message, or
  call right away. It's now told to set up a call watch, checked every few
  hours for something slow.

## 0.127.2 (2026-11-03)

### Fixes
- Workspace files: an empty file (a .gitkeep, say) uploaded from Conduit's
  Folder tab always failed. It now uploads as an empty file.

## 0.127.1 (2026-11-03)

### New and improved
- Bot Mode: a Bot Chat now also looks things up itself by default, so a
  quick question gets a quick answer instead of starting a workstream. A bot
  you set to only coordinate in Edit Bot > Work keeps that.
- Bot Mode: a Bot Chat that looks things up hands setting or removing a
  place rule to a workstream, as it does anything else that changes
  something.

## 0.127.0 (2026-11-03)

### New and improved
- Bot Mode: a bot's Bot Chat can coordinate work of its own. It starts
  workstreams (each a chat of its own, with an optional worktree for coding
  work), follows them, and reports back in the Bot Chat. Conduit shows them
  in the bot's Work panel and on Bots Home.
- This is on for every bot after the update. A Bot Chat then only
  coordinates: its terminal, file and web tools run in a workstream instead
  of in the Bot Chat itself. Edit Bot > Work lets a bot look things up
  itself as well, or turns workstreams off so its Bot Chat runs its own
  tools as before.
- Bots can ask each other for help and answer, and a workstream picks up
  again after Hermes restarts.
- Edit Bot > Work: whether a bot starts workstreams, asks before it does, or
  never does, and the model its workstreams use.
- A voice call's "call me when it's done" jobs show in the bot's Work panel,
  and a Bot Chat call tells you what its coordinator reported.
- Scheduled jobs delivered to a Bot Chat just show their report by default.
  Turn on "Let the bot act on it" for a job to have the bot follow up on
  each run.

## 0.126.2 (2026-11-03)

### Fixes
- With the plugin in a named profile, Settings > Pulse's Set up Pulse made
  Pulse's chat in the default profile, where Pulse never looks, so Pulse sent
  nothing. Pulse now tells Conduit which profile it runs in, so the chat is
  made there, and when a chat titled Pulse is in another profile, Settings >
  Pulse says which one.

## 0.126.1 (2026-11-02)

### Fixes
- Predictions could show one suggested reply fewer than you chose: a
  suggestion between 201 and 280 characters took a place, then Conduit left
  it out as too long for a chip. Those are now left out on the host, so a
  shorter one takes the place.

## 0.126.0 (2026-11-02)

### Fixes
- On a host where Conduit connects to Hermes' Webapp (`hermes webapp`)
  instead of `hermes dashboard`, Predictions said the chat isn't open,
  Watch jobs and a Watch call's Hermes chat ran where the phone couldn't
  see them, Pulse sent nothing, and messages from CarPlay or the Watch lost
  the note saying where they came from. Those work there now. Predictions
  answer there only while Hermes still holds the chat, up to an hour after
  its last reply; a chat opened after Hermes restarts gets none until it
  replies again.
- A message Hermes refused, or that Conduit never sent (Conduit takes its
  note back when the send fails), no longer leaves its
  "sent from CarPlay" (or iPhone, Watch...) note waiting, where the same words
  sent from another app in that chat could pick it up. One whose answer was
  lost on the way keeps it, since Hermes may have taken it, and on Hermes'
  Webapp a note left behind still lapses after 15 minutes.

## 0.125.0 (2026-11-02)

### New and improved
- Linked Rooms: you can let your agent use MCP servers you choose in rooms
  with people you've linked. They stay off unless you pick them in the room
  setup, a server added later stays out, and their tools work only in rooms
  where Custom names them.

## 0.124.0 (2026-11-01)

### New and improved
- Predictive's fast model is now one of Hermes' helper models, named
  "Conduit predictions", so you can pick it in Conduit under Models &
  providers > Helper models, or on Hermes' own Models page.

## 0.123.3 (2026-11-01)

### Fixes
- Pulse's "still want it?" card for something open over a week now says
  what it's about. It used to say only "It has been over a week and
  nothing shows it happened". Cards already on the Pulse screen or waiting
  for the morning brief say it too.

## 0.123.2 (2026-11-01)

### Fixes
- Messages from other bots no longer keep failing with "held by another
  session writer". A Bot Chat from before Hermes' gateway took over chats
  only moved to the gateway when a bot's message arrived while nothing
  had the chat open, so with it open in Conduit or Desktop every message
  was refused. On Linux and macOS hosts, the plugin now moves such a Bot
  Chat itself the next time it's free, usually right after a restart, and
  messages get through from then on, open or not.

## 0.123.1 (2026-11-01)

### Fixes
- Pulse no longer brings up something again while its card is still open
  when that card had nothing to tie it to (no remembered item, reminder or
  calendar entry): it now matches by what the card says, the same names and
  the same day, so a birthday you already have a card for doesn't come back
  in a report or the morning brief before its day.

## 0.123.0 (2026-10-31)

### New and improved
- Fewer notifications pile up. Each scheduled job keeps one notification:
  a new run replaces the last one instead of adding another.
- Hermes can update a notification it sent earlier instead of adding a
  new one ("Report in progress", later "Report ready"), from any chat or
  scheduled job of the same profile, and can make the update quiet: no
  sound or banner. Quiet updates need relay 0.26, and so does replacing
  an unencrypted notification.
- A quiet notification makes no sound now, Pulse's included (one sent
  while your phone says you're busy, or a quiet report). They used to
  chime when the completion sound was on.
- A muted scheduled job's background task no longer notifies when the
  run's chat is one Hermes' gateway made (a `local-` chat that names the
  job only through its task).

## 0.122.3 (2026-10-31)

### Fixes
- Pulse no longer makes a card out of a command that failed harmlessly.
  A command that said its failure doesn't matter, a lookup like ls or cat
  with its errors silenced or a yes/no check like grep or test, is left out
  of what Pulse reads when its output shows nothing wrong, and so is a call that went
  through when it ran again.
  A failure that says what went wrong, like a traceback, still shows, now
  with the line that says it.

## 0.122.2 (2026-10-31)

### Fixes
- Every Hermes profile that has Conduit turned on now runs this version.
  Before, a profile made by cloning another kept the plugin it was cloned
  with, so its notifications missed the chat's name and tapping one opened
  nothing. A profile turned on without its own copy sent no notifications
  at all. Each one is linked to the main install when the dashboard starts,
  and an old copy is kept in the profile's plugins-backup folder. A profile
  that runs its own gateway picks it up when that gateway restarts, and
  may fail until then, so restart it after updating.

## 0.122.1 (2026-10-30)

### Fixes
- The morning brief no longer repeats what an open card on the Pulse
  screen already says (a promise, a reminder or a calendar entry). On the
  day it's due it may come in the brief once.

## 0.122.0 (2026-10-30)

### New and improved
- Alert calls no longer ring while you're working at a screen. When you're
  talking to Hermes in Hermes Desktop, the web dashboard, a terminal, or a
  chat open in Conduit, a question calls only if it's still waiting after 10
  minutes, an approval shortly before Hermes would turn it down, and a
  failed request sends a notification instead of calling. You can turn this
  off in Calls from Hermes.

## 0.121.0 (2026-10-30)

### New and improved
- Opening a chat on a slow connection is quicker: Conduit downloads only the
  messages it doesn't already have on the phone.

## 0.120.4 (2026-10-30)

### Fixes
- A notification for a reply with a card made for Hermes Desktop (such as
  hermes-rich-ui's) showed the raw `::richui{...}` line. It now says a card
  is there, and a page the agent previews shows as its file's name.

## 0.120.3 (2026-10-30)

### Fixes
- Settings > Pulse named the Pulse chat, which you no longer see. Its
  model picker now says what Default runs on, and what's wrong with
  Pulse's chat says how to fix it from Settings > Pulse.

## 0.120.2 (2026-10-29)

### Fixes
- Pulse could bring up the same thing again while its card was still on
  your Pulse screen, or still waiting for the morning brief, a place or a
  shop: a birthday on your calendar on several days as it got closer, or
  something it remembers a day after the brief mentioned it. While a card
  is open, nothing else about the same thing comes up, except once in the
  last 24 hours before it's due (for a date, its whole due day). A calendar entry also gets one card as a phone reminder
  does: once its card is done, none again for a week, and none once you
  put it away.

## 0.120.1 (2026-10-29)

### Fixes
- When a linked person's Hermes asked whether you're free, a busy time
  that ran to the end of the day read as ending at 00:00 (or 01:00 where
  clocks go forward at midnight). It reads 24:00 now. A linked Hermes still
  on 0.120.0 leaves that busy time out of what it keeps, though the answer
  it reads still says it, until it updates.

## 0.120.0 (2026-10-29)

### New and improved
- Your agent can ask someone you've linked with whether they're free on a
  date, or a question. Their Hermes answers by the rules they set, or they
  answer themselves; no agent reads the ask on their side.

## 0.119.2 (2026-10-29)

### Fixes
- After the plugin reloaded, a prediction, step summary or self-improvement
  review could again notify as if the chat had replied. It no longer does.

## 0.119.1 (2026-10-29)

### Fixes
- On some Hermes versions (0.21 among them), Conduit's predictions, step
  summaries (a /btw) and the self-improvement review each sent a
  notification as if the chat had replied, with text such as
  {"replies":[...]}, and moved the chat's Lock Screen progress. Only the
  chat's own replies notify now. Like a subagent, they can't use Conduit's
  tools either (notifications, calls, phone, location and the rest).

## 0.119.0 (2026-10-29)

### New and improved
- Use my data: in a room with other people's agents, your agent can say
  when you're busy, from the calendar your phone shares, to find a time.
  Only the busy times go, with your time zone and when your phone last
  synced, never what, where or with whom.

## 0.118.1 (2026-10-29)

### Fixes
- With Ask me first, your answer could be lost to the room when your
  agent's turn failed with an error, or when a helper it started ran first.
  It now reaches the agent's next turn in that room.

## 0.118.0 (2026-10-29)

### New and improved
- Ask me first: in a room with other people's agents, your agent asks your
  phone before it shares something personal about you. Share, say what it
  should say instead, or don't share; an answer that comes later reaches it
  before its next turn in that room. Needs a Conduit that shows a room's asks.

## 0.117.1 (2026-10-29)

### Fixes
- Do it on a Pulse card that came from a Kanban task's worker opened the
  worker's chat, which can't be continued. It now starts a new chat about
  the task, and Conduit shows the task where the chat was.

## 0.117.0 (2026-10-29)

### Fixes
- A room hosted elsewhere that went on after your agents were taken out of
  it read as ended to your agent. It now says you were taken out.
- Your agent read every agent in a room hosted elsewhere as still in it.
  It now knows that only of your own agents.

### New and improved
- Conduit can open a private chat with one of your agents about a room, and
  your agent's notes on a room it Listens in arrive there. Notes start from
  the room's newest message when the chat opens.
- A room chat you delete is forgotten, so notes stop going to it.

## 0.116.0 (2026-10-29)

### Fixes
- Deleting your copy of a room you're still in lost what you'd posted
  there, so your agents took your earlier messages for the host's. It's
  now refused until you leave, and a room this Hermes hosts can't be
  deleted as a copy.
- Rooms you rejoined over a new link listed your posts from the old one.

### New and improved
- Your agents can read your rooms with other people for you in your
  private chats: notes, a summary, what someone said, a draft reply.
- Groundwork for a room's own private chat, where an agent that only
  listens will leave you notes once the room goes quiet or ends. It starts
  working with the Conduit update that opens these chats.

## 0.115.5 (2026-10-29)

### Fixes
- Finding a chat on an Apple Watch call could pick an old Kanban task's
  worker, saved before Hermes marked them, whose only message is "work
  kanban task" and the task's id. It now skips them, as the iPhone does.

## 0.115.4 (2026-10-29)

### Fixes
- Finding a chat on an Apple Watch call could pick a tool integration's or a
  one-shot run's session, which aren't chats. It now skips them, as the
  iPhone does.

## 0.115.3 (2026-10-28)

### Fixes
- Notifications called the main bot "Default" when it has no name, in its
  Bot Chat's title and in the relay's plain copy, while Conduit calls it
  Hermes. They now say Hermes (push relay 0.23.2).

## 0.115.2 (2026-10-28)

### Fixes
- A Bot Chat's notifications named a bot that has no name by its profile id
  in lowercase, while Conduit shows it capitalized. They now match, and the
  relay's plain copy names an unnamed profile the same way (push relay
  0.23.1).

## 0.115.1 (2026-10-28)

### Fixes
- A message you spoke (a voice call, CarPlay, the Apple Watch) could be
  answered with a sound file Hermes made itself, which the call then read
  out as a file path. Hermes is now told the answer is heard, so it replies
  in words. A file you ask for still comes as before.

## 0.115.0 (2026-10-28)

### Fixes
- A room's copy with one damaged entry couldn't be read at all.

### New and improved
- In a room someone else hosts that your agents are in, you can post too,
  through your own Hermes. Your agents can tell your messages from ones the
  host wrote under your name.

## 0.114.0 (2026-10-28)

### New and improved
- Voice jobs no longer pile up in Hermes Desktop's chat list: once Hermes
  has answered one, it's archived like a finished call, and typing into it
  brings it back. Conduit still lists it under its call.
- Calls kept with a chat now show on every device: each one's chat is
  listed under Voice in Conduit and shows its call cards, not only on the
  phone that made the call.

## 0.113.0 (2026-10-28)

### Fixes
- After moving to a new iPhone, calls from Hermes rang only the Apple Watch,
  as a call for the old iPhone, and answering there gave no voice. Pairing a
  profile with the new iPhone now moves the Watch's ring there too (needs
  push relay 0.23).

### New and improved
- The host's log says where each call from Hermes went: the iPhone, the
  iPhone and its Apple Watch, or a notification when the relay couldn't ring
  the iPhone.

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
