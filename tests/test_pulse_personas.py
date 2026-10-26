"""Pulse's persona suite (designs/pulse-learning.md, "Proving it").

Simulated people use Pulse for 14 days, three reports a day, rating a
random quarter of their cards (Silent rates none; Changer, every card from
the chat they turned down, so the test counts how many thumbs down it takes
rather than how many cards). Each report runs the real
code in a real store: the remembered items the report would show, a model
answering from a fixed fixture of about 60 candidates, the router, the
journal, the cards, and the person's ratings, Not now and Do it. Runs are
seeded, so they repeat.

What must hold on every run is asserted on every report (``check_report``).
What learning has to achieve is measured over the last three days, against
the same run without learning (the same seed draws the same candidates, and
the person does the same things with them), and must hold in NEED of SEEDS
seeds (designs/pulse-learning.md).

Remembered items come in two waves, at the start and on day 11, so the last
three days have remembered items too. The second wave is new items, so a
rule about one item can't carry over; learning about kinds and chats can.
"""

import copy
import functools
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pytest

from test_pulse import ROOT, SID, _load, pulse, report, store  # noqa: F401

candidates = _load("conduit_push.pulse_candidates", ROOT / "pulse_candidates.py")
learning = _load("conduit_push.pulse_learning", ROOT / "pulse_learning.py")
prefs = _load("conduit_push.pulse_prefs", ROOT / "pulse_prefs.py")

START = 1_792_454_400.0  # 2026-10-20 00:00 UTC
DAYS = 14
REPORT_HOURS = (8, 13, 19)
WAVES = (0, 11)  # the days remembered items are said
FOUND_PER_REPORT = 5
RAISED_S = 48 * 3600.0  # the model calls what Pulse brought up this lately "raised"
RATE_SHARE = 0.25
SWITCH_DAY = 7  # Changer turns down the updates chat from here
CHANGER_CHAT = "updates"
LATE = range(11, 14)  # the last three days, where learning is measured
SEEDS, NEED = 20, 19  # a criterion must hold in NEED of SEEDS runs
INVARIANT_SEEDS = 2

OFF, SCREEN, PUSH = "off", "screen", "push"


@dataclass(frozen=True)
class Template:
    """One fixture candidate, as the model sees it and as it really is."""
    key: str
    origin: str  # MEMORY: a remembered item in the store; MODEL: something the model finds
    kind: str
    chat: str  # "" for the calendar, reminders and notes
    area: str
    due_h: Optional[float]  # hours to its date: from its wave for a remembered item, from its report for a found one
    actionable: bool
    value: int  # the model's guess
    skip: str  # the model's reason to leave it out
    floor: bool  # a promise Hermes made, a commitment or an explicit "remind me": a floor once due within a day
    done: bool  # done already or covered: nobody wants to hear it


# key  kind        chat     area      due  act value skip    floor done
FIXTURE = """
m01 promise     work     work      10   y 4 -       y n
m02 commitment  family   personal  40   y 4 -       y n
m03 commitment  work     work      110  y 2 -       y n
m04 commitment  health   health    200  y 2 -       y n
m05 promise     updates  work      -    y 3 -       n n
m06 intention   family   personal  -    n 2 -       n n
m07 interest    updates  work      -    n 2 -       n n
m08 watch       work     work      -    n 2 -       n n
m09 fact        family   personal  -    n 1 minor   n n
m10 commitment  work     work      30   y 4 done    n y
m11 date        family   social    150  y 4 -       n n
m12 reminder    health   health    90   y 5 covered n y
m13 promise     work     work      60   y 3 -       y n
m14 commitment  updates  work      70   y 3 -       y n
m15 interest    health   health    -    n 2 -       n n
m16 commitment  family   personal  20   y 2 -       y n
f01 date        -        social    6    y 4 -       n n
f02 date        -        travel    30   y 2 -       n n
f03 date        -        social    100  n 2 -       n n
f04 follow-up   work     work      12   y 4 -       n n
f05 follow-up   work     work      -    y 2 -       n n
f06 follow-up   updates  work      -    y 4 -       n n
f07 follow-up   updates  work      20   y 4 -       n n
f08 watch       updates  work      -    n 2 -       n n
f09 interest    updates  work      -    n 3 -       n n
f10 reminder    -        health    2    y 5 covered n y
f11 reminder    -        home      8    y 4 -       n n
f12 reminder    -        money     50   y 2 -       n n
f13 commitment  family   personal  18   y 4 -       y n
f14 commitment  family   personal  70   y 2 -       y n
f15 commitment  work     work      5    y 5 -       y n
f16 promise     work     work      -2   y 4 -       y n
f17 promise     health   health    -6   y 2 -       y n
f18 interest    family   personal  -    n 2 -       n n
f19 interest    health   health    -    n 2 -       n n
f20 fact        family   personal  -    n 1 minor   n n
f21 fact        work     work      -    n 2 -       n n
f22 other       -        other     -    n 1 minor   n n
f23 other       -        other     -    n 2 -       n n
f24 intention   family   personal  -    n 2 -       n n
f25 intention   work     work      -    y 2 -       n n
f26 date        -        travel    4    y 5 -       n n
f27 date        family   social    26   y 2 -       n n
f28 follow-up   health   health    40   y 2 -       n n
f29 watch       work     work      -    n 2 -       n n
f30 watch       -        money     -    n 2 -       n n
f31 commitment  work     work      48   y 3 done    n y
f32 commitment  health   health    10   y 4 -       y n
f33 reminder    -        home      1    y 5 -       y n
f34 follow-up   family   personal  -    y 2 -       n n
f35 date        -        social    150  n 2 -       n n
f36 interest    work     work      -    n 3 -       n n
f37 fact        health   health    -    n 2 -       n n
f38 other       -        other     -    n 3 -       n n
f39 follow-up   work     work      3    y 4 -       n n
f40 watch       updates  work      6    y 4 -       n n
f41 commitment  family   personal  2    y 4 -       y n
f42 promise     family   personal  30   y 2 -       n n
f43 intention   health   health    -    n 2 -       n n
f44 other       -        other     -    n 1 -       n n
"""


def _template(line: str) -> Template:
    key, kind, chat, area, due, act, value, skip, floor, done = line.split()
    return Template(key, candidates.MEMORY if key.startswith("m") else candidates.MODEL, kind,
                    "" if chat == "-" else chat, area, None if due == "-" else float(due), act == "y", int(value),
                    "" if skip == "-" else skip, floor == "y", done == "y")


TEMPLATES = {template.key: template for template in map(_template, FIXTURE.strip().splitlines())}
REMEMBERED = [template for template in TEMPLATES.values() if template.origin == candidates.MEMORY]
FOUND = [template for template in TEMPLATES.values() if template.origin == candidates.MODEL]


def floor_now(template: Template, hours: Optional[float]) -> bool:
    """Whether a candidate is a floor now: a promise Hermes made that's
    overdue, a commitment due within the day, an explicit reminder at its
    time."""
    if not template.floor or template.done or hours is None:
        return False
    return hours < 0 if template.kind == "promise" else -24 <= hours <= 24


# Personas: how loud each wants a candidate, given hours to its date (None:
# undated) and the day of the run. OFF means they don't want it at all.

def quiet(template: Template, hours: Optional[float], day: int) -> str:
    """Only what's actionable and due soon; the rest is noise."""
    if template.done:
        return OFF
    if floor_now(template, hours):
        return PUSH
    if template.actionable and hours is not None and -48 <= hours <= 24:
        return PUSH if hours <= 6 else SCREEN
    return OFF


def samantha(template: Template, hours: Optional[float], day: int) -> str:
    """Most things, and anything actionable coming up as a push."""
    if template.done or template.kind in ("fact", "other"):
        return OFF
    return PUSH if template.actionable and hours is not None and hours <= 48 else SCREEN


def changer(template: Template, hours: Optional[float], day: int) -> str:
    """Samantha, until a chat's updates stop being welcome."""
    return OFF if template.chat == CHANGER_CHAT and day >= SWITCH_DAY else samantha(template, hours, day)


@dataclass(frozen=True)
class Persona:
    wants: Callable[[Template, Optional[float], int], str]
    rates: bool = True
    flips: float = 0.0  # the share of their ratings that say the opposite
    does: bool = False  # uses Do it on what they want
    puts_away: bool = False  # swipes away (Not now) some of what they don't
    turns_down: bool = False  # rates every card from Changer's chat once it's unwelcome


PERSONAS = {
    "quiet": Persona(quiet, puts_away=True),
    "samantha": Persona(samantha, does=True),
    "changer": Persona(changer, turns_down=True),
    "silent": Persona(samantha, rates=False, does=True),
    "noisy": Persona(samantha, flips=0.2),
}


@dataclass(frozen=True)
class Report:
    """What one report did, as the persona sees it. Candidates the model
    called raised (brought up lately) and didn't come up again count as
    neither wanted nor missed."""
    day: int
    drawn: int  # candidates the model answered for or found
    valuable: int  # of those, the ones the persona wanted
    surfaced: int
    surfaced_valuable: int
    pushed: int  # items that reached the phone: the picks that came up as a push
    unwanted_pushes: int  # of those, ones the persona didn't want as a push
    chat_surfaced: int  # surfaced from Changer's chat, floors aside (learning never hides those)
    chat_downs: int  # thumbs down on that chat's cards since the switch, so far
    floors_missed: int  # floors due now that weren't surfaced
    found: tuple[str, ...]  # the found candidates drawn, the same for every persona on a seed


class Sim:
    """One persona's run on one seed: the store, the fixture, the clock."""

    def __init__(self, name: str, seed: int, home: Path, noise: float = 0.0, learns: bool = True):
        self.persona = PERSONAS[name]
        # Without learning (the control a run is measured against): what the
        # model picks, as Pulse did before; reasons still act.
        self.learned = None if learns else prefs.EMPTY
        self.store = store.open_store(home)
        self.again = store.open_store(home)  # the same store, opened a second time
        self.noise = noise
        # The same seed draws the same candidates for every persona; what they do is theirs.
        self.draws = random.Random(f"draws-{seed}")
        self.acts = random.Random(f"{name}-{seed}")
        self.items: dict[int, Template] = {}  # remembered item -> its template
        self.wave_start = START
        self.marks: dict[str, int] = {}  # chat -> the message memory read it to
        self.surfaced_at: dict[object, float] = {}  # remembered item or found template -> when last brought up
        self.resting: dict[int, str] = {}  # remembered items the persona's own actions made rest
        self.put_away: dict[int, float] = {}
        self.chat_downs = 0
        self.reports: list[Report] = []
        self.store.set("enabled", True)

    def run(self) -> list[Report]:
        for day in range(DAYS):
            if day in WAVES:
                self.wave(day)
            for hour in REPORT_HOURS:
                self.report(day, START + day * 86400 + hour * 3600)
        return self.reports

    def wave(self, day: int) -> None:
        """The remembered items memory keeps from this wave's chats; the
        last wave's are closed."""
        for item_id in list(self.items):
            self.store.close_item(item_id, status="done", closed_by="sim")
        self.items.clear()
        self.wave_start = at = START + day * 86400
        for template in REMEMBERED:
            chat = template.chat or "notes"
            after = self.marks.get(chat, 0)
            self.marks[chat] = after + 1
            due_at = None if template.due_h is None else at + template.due_h * 3600
            [item_id] = self.store.advance("", chat, message_id=after + 1, at=at, after_id=after, items=[{
                "kind": template.kind, "text": f"{template.key} {template.kind}", "evidence": template.key,
                "explicit": 1, "due": "", "due_at": due_at, "message_id": after + 1, "mentioned_at": at}])
            self.items[item_id] = template

    def listed(self, item_id: int, now: float) -> bool:
        """Whether a remembered item is still one the report lists: open, and
        not so long past its date that memory's next run expires it."""
        template = self.items.get(item_id)
        if template is None:
            return False
        stale = now - pulse.memory.EXPIRE_S
        due_at = None if template.due_h is None else self.wave_start + template.due_h * 3600
        return due_at is None or due_at >= stale or self.wave_start >= stale

    def hours(self, template: Template, now: float) -> Optional[float]:
        if template.due_h is None:
            return None
        return template.due_h - (now - self.wave_start) / 3600 if template.origin == candidates.MEMORY \
            else template.due_h

    def seen(self, template: Template) -> tuple[int, str, str]:
        """The model's take on a template this report: its value guess,
        kind and area, noisy in a noisy run. Drawn for every template in
        the same order whatever the persona does, so seeds stay aligned."""
        value, kind, area = template.value, template.kind, template.area
        if self.draws.random() < self.noise:
            value = min(5, max(1, value + self.draws.choice((-2, -1, 1, 2))))
        if self.draws.random() < self.noise:
            kind = self.draws.choice(report.KINDS)
        if self.draws.random() < self.noise:
            area = self.draws.choice(candidates.AREAS)
        return value, kind, area

    def due(self, template: Template, now: float) -> str:
        """A found candidate's date as the model gives it, in the user's
        time (UTC here); "" for one without."""
        if template.due_h is None:
            return ""
        return datetime.fromtimestamp(now + template.due_h * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M")

    def answer(self, template: Template, value: int, hours: Optional[float], now: float, item_id=None) -> dict:
        """The model's answer for one candidate: it brings up what it values
        at 3 or more that isn't days off, unless it says why not."""
        raised = now - self.surfaced_at.get(template.key if item_id is None else item_id, -RAISED_S) < RAISED_S
        return {"bring_up": value >= 3 and (hours is None or hours <= 72), "value": value,
                "skip": template.skip or ("raised" if raised else ""), "actionable": template.actionable,
                "line": f"{template.key} {template.kind} in {template.chat or 'notes'}"}

    def report(self, day: int, now: float) -> None:
        views = {key: self.seen(template) for key, template in TEMPLATES.items()}
        drawn = self.draws.sample(FOUND, FOUND_PER_REPORT)
        items, resting = report.remembered_items(self.store, now)
        chats = sorted({template.chat for template in drawn if template.chat})
        keys = {chat: f"c{n}" for n, chat in enumerate(chats, 1)}
        remembered = []
        for item in items:
            template = self.items[item["id"]]
            remembered.append({"ref": item["id"], **self.answer(template, views[template.key][0],
                                                                self.hours(template, now), now, item["id"])})
        found = [{**self.answer(template, views[template.key][0], self.hours(template, now), now),
                  "kind": views[template.key][1], "area": views[template.key][2],
                  "source": "chat" if template.chat else "calendar", "chat": keys.get(template.chat, ""),
                  "due": self.due(template, now),
                  "asked": template.floor and template.kind not in ("promise", "commitment")}
                 for template in drawn]
        reply = {"remembered": remembered, "found": found}
        gathered = report.Gathered("", "", items, resting, {key: ("", chat) for chat, key in keys.items()})
        rows = candidates.parse(reply, gathered)
        undecided = copy.deepcopy(rows)
        candidates.decide(self.store, rows, now=now, learned=self.learned)
        # The same store, opened again, decides the same.
        again = candidates.decide(self.again, undecided, now=now, learned=self.learned)
        assert [(row["decision"], row["rank"]) for row in again] == [(row["decision"], row["rank"]) for row in rows]
        run = {"id": f"r{day:02}{int(now) % 86400 // 3600:02}", "state": pulse.RUN_WORKING}
        self.store.set(pulse.REPORT_RUN, run)
        assert self.store.finish_run(pulse.REPORT_RUN, run, rows, {**run, "state": pulse.RUN_READY}, now=now)
        picked = candidates.picks(self.store.run_candidates(run["id"]))  # as the report turn is sent
        cards = learning.add_noticed_cards(
            self.store, turn=_Turn, lines=candidates.lines(picked), chat_id=SID, chat_title="Pulse",
            shown={pick["item_id"] for pick in picked if pick["item_id"] is not None}, kinds=report.KINDS,
            item_chat_title=lambda item: item["chat_id"], now=now, attempt=run["id"],
            marks=(pulse.TURN_CARDS_DONE, store.SPLIT_REPORTS), keep=pulse.TURN_CARDS_DONE_KEEP,
            links=[pick["id"] for pick in picked], levels=candidates.levels(picked))
        self.check_report(run["id"], rows, now)
        self.surfaced_at.update((pick["text"].split()[0] if pick["item_id"] is None else pick["item_id"], now)
                                for pick in picked)
        self.react(day, now, picked, cards)
        self.reports.append(self.measure(day, now, rows, picked))

    def check_report(self, run_id: str, rows: list[dict], now: float) -> None:
        """What the router keeps on every report, learning or not."""
        journal = self.store.run_candidates(run_id)
        # Every candidate is journaled, each with what became of it.
        assert len(journal) == len(rows) and all(row["decision"] in candidates.DECISION_NAMES for row in journal)
        surfaced = [row for row in journal if row["decision"] == candidates.SURFACED]
        # In order, at most MAX_ITEMS besides the floors.
        assert [row["rank"] for row in surfaced] == list(range(1, len(surfaced) + 1))
        assert len([row for row in surfaced if not row["floor"]]) <= candidates.MAX_ITEMS
        # Floors come up, unless the user's own word keeps them out (a rule, the kind off, a no).
        assert not [row for row in journal if row["floor"] and row["decision"] not in (
            candidates.SURFACED, candidates.MUTED, candidates.OFF, candidates.RESTING, candidates.SKIPPED)]
        # Nothing comes up louder than its kind's level, the ceiling the user set.
        assert all(prefs.LEVELS.index(row["level"]) >= prefs.LEVELS.index(learning.level(self.store, row["kind"]))
                   for row in surfaced)
        # Nothing the model gave a reason to leave out: done, covered, raised.
        assert not [row for row in surfaced if row["skip"]]
        # A remembered item rated down, or put away this week, rests (journaled, never asked about).
        resting = set(self.resting) | {item for item, at in self.put_away.items() if now - at <= report.DISMISS_REST_S}
        assert not [row for row in surfaced if row["item_id"] in resting]
        assert {row["item_id"] for row in journal if row["decision"] == candidates.RESTING} == {
            item for item in resting if self.listed(item, now)}
        # Each pick became one card.
        assert all(row["card_id"] for row in surfaced)

    def react(self, day: int, now: float, picked: list[dict], cards: list[int]) -> None:
        """The persona's thumbs, Not now and Do it on this report's cards."""
        persona = self.persona
        for pick, card_id in zip(picked, cards):
            template = TEMPLATES[pick["text"].split()[0]]
            wanted = persona.wants(template, self.hours(template, now), day) != OFF
            turned = persona.turns_down and template.chat == CHANGER_CHAT and day >= SWITCH_DAY
            if persona.rates and (self.acts.random() < RATE_SHARE or turned):
                up = wanted != (self.acts.random() < persona.flips)
                learning.rate(self.store, card_id, "up" if up else "down", now=now)
                if pick["item_id"] is not None:
                    if up:
                        self.resting.pop(pick["item_id"], None)
                    else:
                        self.resting[pick["item_id"]] = report.RATED
                if not up and template.chat == CHANGER_CHAT and day >= SWITCH_DAY:
                    self.chat_downs += 1
            elif persona.puts_away and not wanted and self.acts.random() < RATE_SHARE:
                learning.dismiss(self.store, card_id, True, now=now)
                if pick["item_id"] is not None:
                    self.put_away[pick["item_id"]] = now
            if persona.does and wanted and template.actionable and self.acts.random() < 0.5:
                # Do it: the prepared message is sent, and half the time Pulse sees it get done
                # (pulse_followup.check_acts, on the spot here).
                self.store.act_on_card(card_id, now=now, check_at=now + 1800)
                got_done = self.acts.random() < 0.5
                if got_done:
                    self.store.mark_done(card_id, pick["item_id"], closed_by=learning.ACT_DONE_BY, now=now,
                                         by="pulse")
                self.store.decide_act(card_id, acted_at=now, check_at=None, checks=1,
                                      outcome="done" if got_done else "not done", why="",
                                      stage=prefs.ACT_DONE if got_done else prefs.ACT_SENT)

    def measure(self, day: int, now: float, rows: list[dict], picked: list[dict]) -> Report:
        def wants(row: dict) -> str:
            template = TEMPLATES[row["text"].split()[0]]
            return self.persona.wants(template, self.hours(template, now), day)

        asked = [row for row in rows if row["decision"] != candidates.RESTING
                 and (row["skip"] != "raised" or row["decision"] == candidates.SURFACED)]
        valuable = [row for row in asked if wants(row) != OFF]
        floors = [row for row in asked if floor_now(TEMPLATES[row["text"].split()[0]],
                                                    self.hours(TEMPLATES[row["text"].split()[0]], now))]
        pushed = [pick for pick in picked if pick["level"] == PUSH]
        return Report(
            day=day, drawn=len(asked), valuable=len(valuable), surfaced=len(picked),
            surfaced_valuable=sum(wants(pick) != OFF for pick in picked), pushed=len(pushed),
            unwanted_pushes=sum(wants(pick) != PUSH for pick in pushed),
            chat_surfaced=sum(TEMPLATES[pick["text"].split()[0]].chat == CHANGER_CHAT and not pick["floor"]
                              for pick in picked),
            chat_downs=self.chat_downs,
            floors_missed=sum(row["decision"] != candidates.SURFACED for row in floors),
            found=tuple(TEMPLATES[row["text"].split()[0]].key for row in rows if row["origin"] == candidates.MODEL))


class _Turn:
    """The report turn a found item's card points at."""
    session_id = SID
    reply_id = 1


# A store in memory-backed storage where there is some: a run writes a few
# hundred times, and syncing each to a disk is most of its time.
SCRATCH = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None


@functools.lru_cache(maxsize=None)
def run(name: str, seed: int, noise: float = 0.0, learns: bool = True) -> tuple[Report, ...]:
    with tempfile.TemporaryDirectory(dir=SCRATCH) as home:
        return tuple(Sim(name, seed, Path(home), noise, learns).run())


def window(reports: tuple[Report, ...], days: range) -> dict[str, float]:
    rows = [report_ for report_ in reports if report_.day in days]
    surfaced = sum(row.surfaced for row in rows)
    valuable = sum(row.valuable for row in rows)
    found = sum(row.surfaced_valuable for row in rows)
    return {"surfaced": surfaced / len(rows), "junk": (surfaced - found) / max(surfaced, 1),
            "misses": (valuable - found) / max(valuable, 1), "pushes": sum(row.pushed for row in rows) / len(rows),
            "interruptions": sum(row.unwanted_pushes for row in rows) / len(rows)}


def holds(check: Callable[[int], tuple[bool, str]]) -> None:
    """``check`` holds in NEED of SEEDS seeds; stops once it can't."""
    failed = []
    for seed in range(SEEDS):
        ok, why = check(seed)
        if not ok:
            failed.append(f"seed {seed}: {why}")
            if len(failed) > SEEDS - NEED:
                break
    assert len(failed) <= SEEDS - NEED, "\n".join(failed)


def _rounded(numbers: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 2) for key, value in numbers.items()}


# What holds on every run -----------------------------------------------------------

@pytest.mark.parametrize("name", PERSONAS)
def test_every_run_keeps_the_routers_rules(name):
    for seed in range(INVARIANT_SEEDS):
        reports = run(name, seed)  # check_report asserts on every report
        assert len(reports) == DAYS * len(REPORT_HOURS) and sum(row.surfaced for row in reports)


def test_the_same_seed_gives_the_same_run():
    with tempfile.TemporaryDirectory(dir=SCRATCH) as home:
        assert tuple(Sim("quiet", 0, Path(home)).run()) == run("quiet", 0)


def test_the_same_seed_draws_the_same_candidates_for_everyone():
    assert [row.found for row in run("quiet", 0)] == [row.found for row in run("samantha", 0)]
    assert [row.found for row in run("quiet", 0)] != [row.found for row in run("quiet", 1)]


# What learning has to achieve (slice 2) --------------------------------------------
# Each criterion compares the last three days with the same run without
# learning (the same seed, so the same candidates and the same person): what
# differs is what Pulse learned, not which candidates the days drew.

def _margin(more: float, less: float) -> float:
    """``more`` - ``less``, to six places: window averages are sums of
    thirds and ninths, which floats can land a hair under."""
    return round(more - less, 6)


def test_quiet_gets_quieter_without_getting_useless():
    def check(seed):
        late, control = window(run("quiet", seed), LATE), window(run("quiet", seed, learns=False), LATE)
        ok = (late["interruptions"] <= control["interruptions"] / 2 and _margin(control["junk"], late["junk"]) >= 0.05
              and late["misses"] <= 0.3)
        return ok, f"without learning {_rounded(control)}, learning {_rounded(late)}"
    holds(check)


def test_samantha_hears_more_without_more_junk():
    def check(seed):
        late, control = window(run("samantha", seed), LATE), window(run("samantha", seed, learns=False), LATE)
        return _margin(late["surfaced"], control["surfaced"]) >= 0.5 and late["junk"] <= 0.2, \
            f"without learning {_rounded(control)}, learning {_rounded(late)}"
    holds(check)


def _diverge(noise: float) -> Callable[[int], tuple[bool, str]]:
    def check(seed):
        calm, keen = window(run("quiet", seed, noise), LATE), window(run("samantha", seed, noise), LATE)
        return _margin(keen["surfaced"], calm["surfaced"]) >= 1.0 and _margin(keen["pushes"], calm["pushes"]) >= 1.0, \
            f"quiet {_rounded(calm)}, samantha {_rounded(keen)}"
    return check


def test_quiet_and_samantha_end_up_with_different_assistants():
    holds(_diverge(0.0))


def test_they_still_diverge_when_the_model_is_noisy():
    holds(_diverge(0.2))


def test_changer_stops_hearing_a_chat_soon_after_turning_it_down():
    def check(seed):
        reports = run("changer", seed)
        before = sum(row.chat_surfaced for row in reports if SWITCH_DAY - 3 <= row.day < SWITCH_DAY)
        after = [row for row in reports if row.day >= SWITCH_DAY]
        last = max((n for n, row in enumerate(after) if row.chat_surfaced), default=None)
        downs = after[last].chat_downs if last is not None else 0
        # It came up before the switch, and stopped coming up within five thumbs down (Changer rates
        # every card from it): what the model rates highest from it may still come up now and then.
        return before > 0 and downs <= 5, f"surfaced before: {before}, thumbs down before it last came up: {downs}"
    holds(check)


def test_silent_does_a_little_better_from_do_it_alone():
    def check(seed):
        late, control = window(run("silent", seed), LATE), window(run("silent", seed, learns=False), LATE)
        return late["misses"] < control["misses"] and late["junk"] <= max(control["junk"], 0.1), \
            f"without learning {_rounded(control)}, learning {_rounded(late)}"
    holds(check)


def test_floors_always_surface():
    def check(seed):
        missed = sum(row.floors_missed for row in run("quiet", seed))
        return not missed, f"{missed} floors left out"
    holds(check)
