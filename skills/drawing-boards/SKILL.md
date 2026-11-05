---
name: drawing-boards
description: Recipes for boards in Conduit (conduit_show cards) - a trip plan, comparing options, a dashboard from the user's own data, a tracker, and showing changes (a review, a commit, a config) as a diff - with which cards to use, in what order, and what to leave to the reply.
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
