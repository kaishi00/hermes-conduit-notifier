---
name: drawing-boards
description: Recipes for boards in Conduit (conduit_show cards) - a trip plan, comparing options, a dashboard from the user's own data, a tracker, showing changes as a diff, options to look at and pick from (a quick pick or a design review in rounds), and composed cards with buttons and fields (an email ready to send, a day's agenda, watches, an idea to act on) saved as templates - with which cards to use, in what order, and what to leave to the reply.
---

# Drawing boards in Conduit

A board is one `conduit_show` call: a title, up to 12 cards, an optional
`layout` and up to four `next` follow-ups. You say what to show; Conduit
draws it natively on the user's iPhone, iPad, Mac and call screen, and
shows the cards' Markdown wherever a card can't be drawn. In Hermes Desktop
with the plugin's "Conduit boards" half on, the tool answers with a
`::conduit-board{id="…"}` line: write it once in your reply, on a line of its
own, and Desktop draws the board there. Anywhere else outside Conduit the
tool hands you that Markdown for your reply instead.

## Ground rules

- **Real data only.** Numbers, prices, ratings, forecasts and photos come
  from a tool, a site or the user's files, and name their source. Never fill
  a table with guesses.
- **Short reply.** The board carries the detail; your reply gives the gist in
  a sentence or two and never repeats the cards.
- **One board per answer.** To change it, show the whole board again. A card
  with a `live` name replaces the earlier one of that name.
- **Fix what's listed.** If the answer lists `problems`, fix those parts and
  show the board again, or give them in your reply.
- **Follow-ups are the user's words.** `next` holds messages the user would
  send ("Make it $1,500", "Add a day in Sequoia"), never commands.
- **Ask first when it's big and vague.** For a trip with no dates or budget,
  ask one short question before drawing.

## Trip plan

For "plan a 4-day trip to Yosemite in May".

1. `stats`: the headline facts (days, budget, distance, the weather you
   looked up), with its source.
2. `map` of the main places (`conduit_maps` for place ids when you can).
3. `timeline`: one entry per day, labelled like "Day 1 · Fri 12 May", each
   with timed stops; give each stop a `place` Apple Maps can find.
4. `checklist` of what to book and pack, with a `live` name like
   `yosemite-packing` so ticks survive changes.
5. `callout` (warning) for permits, closures or anything time-sensitive.

Use `layout: "tabs"` once the board passes about five cards. `next`: "Make
it cheaper", "Add a rest day", "Swap day 2 and 3".

## Comparing options

For "compare NVDA, AAPL and MSFT this year" or "which of these laptops".

1. `stats` with one number per option, each with a `spark` from the data you
   fetched and a `change`.
2. `chart` (line for change over time, bar for one measure across options).
3. `table` with one row per option and the facts as columns, the same
   units in a column.
4. `comparison` with pros and cons when there's a choice to make, one
   `recommended` at most.

`next`: "Over 5 years", "Add Google", "Which is cheapest to run?".

## A dashboard from the user's data

For "read sales.csv and build me a dashboard" or "chart my open issues".

1. Read the data with the tools you have (files, MCP servers, the web).
2. `stats` for the three or four numbers that matter most.
3. `chart` for the trend, `table` for the rows behind it (at most 50; say
   in the reply how many there are and what you left out).
4. `list` with `ranked: true` for top items.

One tab per area (`layout: "tabs"`) when there are several. `next`: the
obvious drill-downs, like "Only last quarter" or "By region".

## Tracker

For "track my 30-day walking streak" or "a checklist for moving house".

- `progress` for each goal, `stats` for the streak or totals.
- `checklist` with `group`s for the stages. The user's ticks are kept on the
  host for every one of their devices, and you hear what changed at the
  chat's next turn; give it a `live` name so it stays the same list as you
  add items.
- `status` with a `live` name for something in progress you'll update.

## Showing changes

For "review PR 42", "what did you change?", "show me the diff" or a config
you edited.

1. Get the diff with the tools you have: `git diff`, `git show <commit>`,
   `gh pr diff 42`, or `diff -u old new` for files outside Git.
2. A `diff` card. For anything past a few dozen lines, write it to a file
   and pass `patch_file` (`git diff main > /tmp/review.diff`, then
   `patch_file: "/tmp/review.diff"`) rather than writing the diff out;
   Conduit lists the files and the user opens each. A short one can go as
   `patch`. Two versions of a text with no diff yet go as `before` and
   `after`, with `name`.
3. Put the pull request or commit in `source` (`name`: "PR #42", `url`:
   its page) when there is one, and a title that says what changed.
4. For a review, add a `list` of findings (each `title` the file and line,
   `detail` the issue) or a `callout` (warning) for the one that matters
   most. `stats` with files, lines added and removed and checks can head it.

Your reply says what changed and why in a sentence or two; never paste the
diff again. `next`: "Explain the change in Login.swift", "Fix the warning",
"Show only the tests". If your terminal runs in a container or on another
machine, `patch_file` can't see its files: pass `patch` instead.

## Showing options to choose between

For "mock up a few directions", "how should the chart look?", "which
colour?" or any choice that's easier to see than describe.

1. Make two to four options for each decision, each a self-contained HTML
   page (inline CSS, no outside scripts) sized for a phone, or a picture.
   Give a page as `html`, or write it to a file on the Hermes host and give
   `file` (an .html page, or a .png or .jpg you made); a picture you found
   online goes as `image` (https).
2. A `review` card: a `title`, and `review.decisions`, each with a `title`,
   a line of `text` on what's being decided and its `options` (`label`, a
   line of `text`, `recommended` on the one you'd pick). `layout`: `side`
   to see them side by side (the default), `switch` to flip between them
   full size, `before_after` for a current and a proposed version.
3. One decision is a quick pick the user taps right in the chat. Several (up
   to six) open a review: the user marks each Approve, Change or Skip, adds
   notes, and sends.
4. Their marks come back as their message. Keep what they approved, rework
   what they marked Change as their notes say, and show the next round with
   the same `review.id` (the earlier card's answer gives it), each decision
   keeping its `id` (d1, d2…); leave out the decisions they settled. When
   everything is approved, go ahead and build it.

In a Bot Chat, have a workstream make the options (it writes the pages to
files and reports them), then show the review with `for` set to its ref,
like `W3`, and each option's `file` the path it reported; never answer
with links to the files. When the update says its report names pages,
`conduit_show {"title": ..., "from_work": "W3"}` shows the review the plugin
made from them, as the next round when that workstream already has one. The user's marks go straight to that workstream, and every round
of the review stays with it: show each round with `for` and the same id.
Reviews aren't shown in group rooms or on a voice call yet: describe the
options there instead.

## Composed cards: build your own, with buttons

When no card type fits, build one: `type: "composed"` with `sections`, each
a `column` (the default) or a `row` of up to six elements: `text` (role
heading, body, caption or mono), `metric`, `badge`, `icon` (an SF Symbol
name), `image`, `progress`, `divider`, `field` and `button`. Pick roles and
tones (neutral, accent, good, warning, bad); Conduit owns the fonts and
colors. Mark a section `more: true` to fold detail until the card is opened.

Any card can carry up to three `actions` at its foot. A button can:

- `open` a link: https, or `maps:`, `tel:`, `sms:`, `mailto:`, `calshow:`
  (Calendar), `x-apple-reminderkit:` (Reminders);
- `copy` text, or `send` a message as the user (their words, never a
  command);
- `tap`: you hear it at once as the user's message, with the card as they
  saw it and its field values. Do it with your tools, then show the card
  again with the same `live` name, saying how it went: "Sent 20:41" with a
  good badge, or what went wrong. Never show it done before your tool says
  so.

A tap is the user's OK for exactly what the card showed, nothing more. So
put on the card what will happen (the recipient, the text, the amount) and
give a button that sends, buys or deletes a `confirm` question. Fields never
ask for passwords, PINs, one-time or verification codes, tokens or keys
(a field named so is dropped). Every composed card needs a `live` name
when it has a tap button, so you can show the result in its place.

### An email ready to send

`badge` "Not sent" (warning); a row of `field`s `to` and `subject`; a
`field` `body` with `lines: 6`; actions `Send` (tap, primary, confirm "Send
to Mei?") and `Discard` (tap, destructive). On the tap, send it with the
user's mail tool using the field values, then show the card again with a
"Sent" badge (good) and no buttons.

### A day's agenda

A `timeline` card does a plain agenda. For free time and actions: a section
per block, `text` heading with the time, `caption` with the place, a `badge`
"Free" (good) for gaps; actions `Open in Calendar` (open `calshow:`) and
`Find a slot` (tap). Read the calendar with `conduit_calendar` first.

### Watches and follow-ups

A section per watch: `text` heading (what's watched), `caption` with the
last check, a `badge` with its state, a `progress` for a delivery; actions
`New watch` (send "Set up a new watch"), and a tap `Pause`.

### An idea to act on

`text` heading ("Book the vet"), `body` with why now, `caption` with the
source; actions `Do it` (tap, primary), `Done` (tap), `Dismiss` (tap).

### Saving it as a template

When the user will want the same kind of card again, save it once with
`save_templates`: a `name` like `email-draft`, a line of `about`, and the
composed card with `{{slot}}` wherever a value goes. A section with
`repeat: "events"` is drawn once per item of the list slot `events`, its
values written `{{events.time}}`; each item is one of the card's 8
sections, so split a longer list over more cards. Then show it with
`{"type": "template", "template": {"name": "email-draft", "fill": {"to":
"mei@example.com", "subject": "Friday"}}, "live": "email-mei"}`: give the
card's title, live name, source and actions beside it as usual. The drawing
rule lists your saved templates; save one again with the same name to change
it, or without `card` to delete it.
