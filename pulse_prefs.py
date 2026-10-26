"""What Pulse learned the user wants (designs/pulse-learning.md, slice 2).

Whatever the user does with a card leaves a signal (pulse_store's
``signals``), one row per card and kind of signal:

- a thumbs up or down (``THUMB``): one vote, useful or not;
- Do it (``ACT``), by the furthest it got: opened (``ACT_OPENED``), the chat
  answered so something was sent (``ACT_SENT``), Pulse saw the thing get done
  (``ACT_DONE``); a fraction of a useful vote, so opening the prepared prompt
  out of curiosity teaches almost nothing;
- Done on the card (``DONE``): a smaller useful vote than a done Pulse saw
  after Do it. Of Do it and Done only the stronger counts.

A pushed card (its row's ``pushed``) acted on, by Do it or Done, within
QUICK_S of the push is a loudness signal for its kind, not a relevance one.

Not now, and a card opened and left alone, leave none (Eric, 2026-10-24).

The model is a pure function of the signals, recomputed when it's needed: a
restart, a model switch or a lost process can't change it, and taking a
signal back (another rating, Undo on Done) is exact.

Relevance is an additive model in log-odds: an overall lean, plus one for
the candidate's kind, its place (the chat it came from, or its area when it
came from none) and whether it's timely (due within the day, or overdue).
It's fitted jointly (regularized logistic regression, a few passes of Newton
steps), so a vote counts once even though kind and timing go together, and
each attribute is shrunk toward nothing by its prior strength: specific ones
learn fast, the overall lean slowly. That's the long memory (half-life
LONG_S). A place with more than PLACE_LAST votes then follows its last
PLACE_LAST, on top of the long memory: three thumbs down in a row on a chat
the user used to like quiet it, a thumbs up brings it back, and the change
fades over weeks (PLACE_S) unless they keep saying so. A place with fewer
votes has them all in the long memory already, where counting its last ones
again would make a new chat's first thumbs down count twice. Kinds and
timing keep their long memory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

# Signal kinds (pulse_store's signals.what).
THUMB, ACT, DONE = "thumb", "act", "done"
WHATS = (THUMB, ACT, DONE)
# How much of a useful vote each Do it stage is, and Done on the card.
ACT_OPENED, ACT_SENT, ACT_DONE = 0.1, 0.4, 0.75
DONE_WEIGHT = 0.4
# A pushed card acted on within this long of the push is a loudness signal.
QUICK_S = 3600.0
QUICK_STEP = 0.5
QUICK_MAX = 1.0

LONG_S = 60 * 86400.0
# A place with more votes than PLACE_LAST follows its last PLACE_LAST,
# fading over PLACE_S.
PLACE_LAST = 3
PLACE_S = 30 * 86400.0
# Signals older than this weigh under 1% in the long memory: dropped.
KEEP_S = 400 * 86400.0

# The useful share at which learning changes nothing: a user who finds about
# this share of what Pulse brings up useful gets what the model picks.
PRIOR = 0.7
# The attributes the model learns over, from what a signal keeps of a card
# (pulse_store's signals: its kind, place and band): its kind, its place, and
# whether it was timely (its band one of TIMELY), which pools what the user
# says about everything due later or undated. How soon exactly is loudness
# (URGENCY).
FEATURES = ("kind", "place", "timely")
# Prior strength, in votes, per attribute (the overall lean, then each
# attribute), toward nothing learned; and a place's, toward the long
# memory's, when it follows its last votes.
STRENGTH = {"all": 10.0, "kind": 6.0, "place": 3.0, "timely": 4.0}
PLACE_STRENGTH = {"place": 1.5}
SWEEPS = 6
# A coefficient is kept within this, in log-odds.
LIMIT = 4.0

# Scoring (designs/pulse-learning.md, "Relevance" and "Loudness"). A
# candidate's score is what learning adds, in log-odds, plus VALUE_STEP per
# point of the model's value guess above or below 3, plus PICK when the
# model picked it (a follow-up check that decided to tell counts as a pick).
# At SURFACE_AT it comes up: with nothing learned, exactly the model's picks.
VALUE_STEP = 0.5
PICK = 2.5
SURFACE_AT = 1.25
# How loudly: the score, plus URGENCY for how soon it's due, plus what
# pushes acted on quickly earned its kind, against the nudge level's
# PUSH_AT: at or over it, a push; under it, the Pulse screen (or, for a
# follow-up that scores under SURFACE_AT, the morning brief). With nothing
# learned every pick comes up, and pushes when it clears the bar: at the
# balanced nudge one the model values 3 or more does, as every report item
# did before; at gentle, mostly what's due within the day.
PUSH_AT = {"gentle": 2.75, "balanced": 2.0, "eager": 1.5}
LEVELS = ("push", "screen", "brief", "off")  # loudest first

# Urgency bands: how soon a candidate is due when it comes up.
OVERDUE, SOON, TODAY, DAYS, LATER, UNDATED = "overdue", "soon", "today", "days", "later", "undated"
BANDS = (OVERDUE, SOON, TODAY, DAYS, LATER, UNDATED)
SOON_S, TODAY_S, DAYS_S = 6 * 3600.0, 86400.0, 3 * 86400.0
URGENCY = {OVERDUE: 1.0, SOON: 1.0, TODAY: 0.5, DAYS: 0.0, LATER: -0.5, UNDATED: -0.5}


def band(due_at: Optional[float], now: float, pace: float = 1.0) -> str:
    """How soon something is due, as a band. ``pace`` below 1 (a "too late"
    lesson) treats it as due sooner than it is."""
    if due_at is None:
        return UNDATED
    ahead = (due_at - now) * pace
    if ahead < 0:
        return OVERDUE
    if ahead <= SOON_S:
        return SOON
    if ahead <= TODAY_S:
        return TODAY
    if ahead <= DAYS_S:
        return DAYS
    return LATER


TIMELY = (OVERDUE, SOON, TODAY)


def attributes(kind: str, place_: str, band_: str) -> dict[str, str]:
    """A candidate's or a vote's attributes: FEATURES, and its band."""
    return {"kind": kind, "place": place_, "band": band_,
            "timely": ("yes" if band_ in TIMELY else "no") if band_ else ""}


def place(profile: str, chat_id: str, area: str = "", source: str = "") -> str:
    """Where a candidate came from: its chat, else its area, else its
    source (calendar, reminders, notes...); "" when nothing says."""
    if chat_id:
        return f"chat:{profile}:{chat_id}"
    if area:
        return f"area:{area}"
    return f"source:{source}" if source else ""


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@dataclass(frozen=True)
class Vote:
    """One card's relevance evidence: ``useful`` of ``weight`` votes, when,
    about a card with these ``keys`` (each attribute's (name, value))."""
    at: float
    useful: float
    weight: float
    keys: tuple[tuple[str, str], ...]


def _vote(row: dict) -> Vote:
    attrs = attributes(row["kind"], row["place"], row["band"])
    return Vote(row["at"], row["useful"] * row["weight"], row["weight"],
                tuple((name, attrs[name]) for name in FEATURES if attrs[name]))


def votes(signals: Iterable[dict]) -> list[Vote]:
    """The relevance votes in ``signals`` (pulse_store rows): per card, its
    thumb, and the stronger of Do it and Done."""
    cards: dict[int, dict[str, dict]] = {}
    for row in signals:
        if row["what"] in (THUMB, ACT, DONE) and row["weight"] > 0:
            cards.setdefault(row["card_id"], {})[row["what"]] = row
    out = []
    for rows in cards.values():
        if THUMB in rows:
            out.append(_vote(rows[THUMB]))
        acted = max((rows[what] for what in (ACT, DONE) if what in rows), key=lambda row: row["weight"], default=None)
        if acted is not None:
            out.append(_vote(acted))
    return out


@dataclass
class Fit:
    """A fitted relevance model: the overall lean and each attribute value's
    coefficient, in log-odds."""
    bias: float
    weights: dict[tuple[str, str], float] = field(default_factory=dict)

    def eta(self, attrs: dict[str, str]) -> float:
        return self.bias + sum(self.weights.get((name, attrs[name]), 0.0) for name in FEATURES if attrs.get(name))


def fit(data: Sequence[tuple[Vote, float]], center: Fit, strength: dict[str, float],
        groups: Sequence[str] = ("all",) + FEATURES) -> Fit:
    """Fits the additive model to ``data`` (each vote with its weight), the
    coefficients of ``groups`` ("all" for the overall lean, or attributes)
    each pulled toward ``center``'s by a prior worth ``strength`` votes for
    its group, the rest kept at ``center``'s: a few passes of a Newton step
    per group."""
    current = Fit(center.bias, dict(center.weights))
    rows = [(vote.useful / vote.weight if vote.weight else 0.0, weight, vote.keys) for vote, weight in data
            if weight > 1e-9]
    present = {key[0] for _, _, keys in rows for key in keys}
    for _ in range(SWEEPS if rows else 0):
        for group in groups:
            if group != "all" and group not in present:
                continue
            lam = strength[group] * PRIOR * (1 - PRIOR)
            grad: dict[object, float] = {}
            hess: dict[object, float] = {}
            for share, weight, keys in rows:
                target = "bias" if group == "all" else next((key for key in keys if key[0] == group), None)
                if target is None:
                    continue
                p = _sigmoid(current.bias + sum(current.weights.get(key, 0.0) for key in keys))
                grad[target] = grad.get(target, 0.0) + weight * (share - p)
                hess[target] = hess.get(target, 0.0) + weight * p * (1 - p)
            for target, g in grad.items():
                if target == "bias":
                    value, mean = current.bias, center.bias
                else:
                    value, mean = current.weights.get(target, 0.0), center.weights.get(target, 0.0)
                step = (g - lam * (value - mean)) / (hess[target] + lam)
                value = max(-LIMIT, min(LIMIT, value + step))
                if target == "bias":
                    current.bias = value
                else:
                    current.weights[target] = value
    return current


@dataclass(frozen=True)
class Learned:
    """What the signals add up to at a moment: the long and short fits,
    the raw counts behind each attribute value (for "why": the last
    2 * LONG_S's votes, or a place's last PLACE_LAST when it follows them),
    and the loudness each kind earned from pushes acted on quickly."""
    long: Fit
    short: Fit
    counts: dict[tuple[str, str], tuple[float, float]]
    loud: dict[str, float]
    votes: int

    def score(self, attrs: dict[str, str]) -> float:
        """What learning adds to a candidate's score, in log-odds: 0 with no
        evidence."""
        return self.short.eta(attrs) - logit(PRIOR)

    def factor(self, attrs: dict[str, str]) -> Optional[dict]:
        """The biggest part of ``score`` in its direction, with the votes
        behind it: {"scope", "value", "effect", "useful", "votes"}; None
        when learning adds next to nothing."""
        total = self.score(attrs)
        if abs(total) < 0.25:
            return None
        parts = [("all", "", self.short.bias - logit(PRIOR))]
        parts += [(name, attrs[name], self.short.weights.get((name, attrs[name]), 0.0)) for name in FEATURES
                  if attrs.get(name)]
        scope, value, effect = max(parts, key=lambda part: part[2] if total > 0 else -part[2])
        if (effect > 0) != (total > 0) or abs(effect) < 0.1:
            return None
        useful, weight = self.counts.get((scope, value), (0.0, 0.0))
        return {"scope": scope, "value": value, "effect": round(effect, 2), "useful": round(useful, 1),
                "votes": round(weight, 1)}

    def loudness(self, kind: str) -> float:
        return self.loud.get(kind, 0.0)


EMPTY = Learned(Fit(logit(PRIOR)), Fit(logit(PRIOR)), {}, {}, 0)


def learn(signals: Sequence[dict], now: float) -> Learned:
    """The model ``signals`` add up to at ``now``."""
    signals = [row for row in signals if now - row["at"] < KEEP_S]
    found = votes(signals)
    if not found:
        return EMPTY
    prior = Fit(logit(PRIOR))
    faded = [0.5 ** (max(0.0, now - vote.at) / LONG_S) for vote in found]
    long = fit([(vote, vote.weight * fade) for vote, fade in zip(found, faded)], prior, STRENGTH)
    # A place with more than PLACE_LAST votes from its last PLACE_LAST,
    # fading over PLACE_S, the rest of the model kept as the long memory has
    # it: a change of heart about a chat shows after a few votes, without
    # waiting for its older ones to fade.
    seen: dict[str, int] = {}
    recent: list[tuple[Vote, float]] = []
    last: dict[tuple[str, str], tuple[float, float]] = {}
    total: dict[str, int] = {}
    for vote in found:
        about = dict(vote.keys).get("place", "")
        total[about] = total.get(about, 0) + 1
    for vote, fade in sorted(zip(found, faded), key=lambda pair: pair[0].at, reverse=True):
        about = dict(vote.keys).get("place", "")
        if about and seen.get(about, 0) < PLACE_LAST and total[about] > PLACE_LAST:
            recent.append((vote, vote.weight * fade * 0.5 ** (max(0.0, now - vote.at) / PLACE_S)))
            useful, weight = last.get(("place", about), (0.0, 0.0))
            last[("place", about)] = (useful + vote.useful, weight + vote.weight)
        seen[about] = seen.get(about, 0) + 1
    short = fit(recent, long, PLACE_STRENGTH, groups=("place",))
    counts: dict[tuple[str, str], tuple[float, float]] = {}
    for vote in found:
        if now - vote.at > 2 * LONG_S:
            continue
        for key in (("all", ""),) + vote.keys:
            useful, weight = counts.get(key, (0.0, 0.0))
            counts[key] = (useful + vote.useful, weight + vote.weight)
    counts.update(last)
    return Learned(long, short, counts, loudness(signals, now), len(found))


def loudness(signals: Iterable[dict], now: float) -> dict[str, float]:
    """What each kind earned from pushes acted on quickly: QUICK_STEP per
    pushed card acted on (Do it or Done) within QUICK_S of its push,
    fading like the long memory, at most QUICK_MAX."""
    first: dict[int, tuple[float, float, str]] = {}
    for row in signals:
        if row["what"] in (ACT, DONE) and row.get("pushed") is not None and 0 <= row["at"] - row["pushed"] <= QUICK_S:
            seen = first.get(row["card_id"])
            if seen is None or row["at"] < seen[0]:
                first[row["card_id"]] = (row["at"], row["pushed"], row["kind"])
    loud: dict[str, float] = {}
    for at, _, kind in first.values():
        loud[kind] = loud.get(kind, 0.0) + QUICK_STEP * 0.5 ** (max(0.0, now - at) / LONG_S)
    return {kind: min(QUICK_MAX, value) for kind, value in loud.items()}


TIMELY_WORDS = {"yes": "things due within the day or overdue", "no": "things not due within the day"}


def _number(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def factor_words(factor: dict) -> str:
    """A ``Learned.factor`` in words, for `pulse candidates`: "useful 5 of
    6 times: promise items". The times are the votes behind it lately
    (``Learned.counts``), each counted once, not faded as the fit fades them."""
    scope, value = factor.get("scope"), factor.get("value") or ""
    what = {"all": "what Pulse brings up", "kind": f"{value} items",
            "timely": TIMELY_WORDS.get(value, value)}.get(scope)
    if scope == "place":
        what = ("things from that chat" if value.startswith("chat:")
                else f"things about {value[5:]}" if value.startswith("area:")
                else f"things from {value[7:]}" if value.startswith("source:") else value)
    if not what:
        return ""
    votes = float(factor.get("votes") or 0)
    useful = float(factor.get("useful") or 0)
    lean = "more" if (factor.get("effect") or 0) > 0 else "less"
    return f"{lean} of {what}: useful {_number(useful)} of {_number(votes)} times" if votes else f"{lean} of {what}"


def score(learned: "Learned", attrs: dict[str, str], *, value: Optional[int] = None, picked: bool = True) -> float:
    """A candidate's score (see SURFACE_AT): learning, the model's value
    guess (3 when it gave none) and its pick."""
    return learned.score(attrs) + VALUE_STEP * ((value or 3) - 3) + (PICK if picked else 0.0)


def level(score_: float, band_: str, *, loud: float = 0.0, nudge: str = "balanced", floor: bool = False,
          lowest: str = "off") -> str:
    """How loudly something with this score comes up: ``lowest`` when it
    scores under SURFACE_AT, however soon it's due (urgency makes what comes
    up louder, it never brings up what the user said they don't want);
    else, or for a floor, "push" or "screen"."""
    if not floor and score_ < SURFACE_AT:
        return lowest
    if score_ + URGENCY.get(band_, 0.0) + loud >= PUSH_AT.get(nudge, PUSH_AT["balanced"]):
        return "push"
    return "screen"


def quieter(level_: str, ceiling: str) -> str:
    """``level_``, at most ``ceiling`` (pulse_learning.level: what the user set)."""
    if ceiling not in LEVELS or level_ not in LEVELS:
        return level_
    return level_ if LEVELS.index(level_) >= LEVELS.index(ceiling) else ceiling
