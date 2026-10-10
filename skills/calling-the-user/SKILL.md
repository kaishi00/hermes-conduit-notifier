---
name: calling-the-user
description: When and how to phone the user through Conduit with the conduit_call_user tool, including calls made on your own judgment.
---

# Calling the user through Conduit

`conduit_call_user` makes the user's iPhone ring like a phone call. When they
answer, a live voice conversation opens in the chat that called, and it
opens with your reason and this turn's final reply. Calls interrupt people,
so they follow the user's own settings in the Conduit app:

- **Calls they ask for** ("call when I ask", on by default once calls are on).
- **Calls you decide to make** ("Hermes decides", off unless they turned it on).
- **Limits**: a minimum gap between calls and a cap per hour and per day. A
  call over a limit becomes the usual notification.

## When the user asked

The user said "call me", "phone me", "ring me", "give me a call" when
something is done, found or ready. Call with `asked_by_user: true`.

1. Do the work in this turn. The call rings when the turn ends, never before.
2. Call the tool once, with a `reason` that carries the news, not a promise:
   "The migration finished and every table checks out." or "The build failed
   on the iOS target; the log points at a missing certificate."
3. End the turn with the full result as your final reply. The call opens on
   it, so the user can ask follow-up questions straight away.

If the work hands off to a background process and ends the turn early, the
call rings when that turn ends. Call the tool in the turn that has the result
instead (for example the turn that handles the process's completion).

In a scheduled job (cron) the same applies: call in the run that has the news.

## When you decide

Only with `asked_by_user: false`, and only when all of these hold:

- The user would clearly want to know now, not when they next look: an
  outage, a deadline about to pass, a result they were waiting on, an action
  that only they can take before it's too late.
- A notification isn't enough: it's urgent or they told you they're away.
- You haven't called about the same thing already.

Never call for routine progress, a finished chat reply, small talk, or to
ask something that can wait for their next message. When unsure, don't call:
put it in your reply, and the user gets the usual notification.

## Reading the answer

- `ok: true` means the phone rings when this turn ends (unless a limit holds
  it back or the user is already in a voice call with you). Don't tell the
  user it's ringing; just finish the turn.
- `calls_off` when they asked: tell them calls are off in Conduit (Voice
  settings, Calls from Hermes) and give the result in your reply.
- `calls_off` when you decided: don't mention it; give the news in your reply.
- `not_paired`, `too_many`, `subagent`: give the news in your reply. A
  subagent reports back; the main conversation decides whether to call.

## When they don't pick up

If the user declines the call or it goes unanswered, the next turn in this
chat starts with a note from Conduit saying so. They got a missed-call
notification that opens this chat, but they haven't heard your news: lead
with it if it still matters. Don't call again about the same thing unless
they ask you to.

## Writing the reason

One short spoken sentence, said first when the user answers. Lead with the
news. No markdown, links, ids or code: it's read aloud. Keep private details
(passwords, keys, personal data) out of it; the full detail stays in the chat.
