#!/usr/bin/env python3
"""
subscription_finder: find recurring charges hiding in a bank or card export.

WHAT IT DOES
------------
Give it a CSV of transactions (date, description, amount). It looks for charges
that repeat on a steady schedule for a steady price, which is what a subscription
looks like, and reports what each one costs per month and per year. It also points
out two things that are easy to miss:

  * PRICE CHANGES: "Netflix went from 15.49 to 17.99 in March."
  * POSSIBLY ENDED subscriptions: charges that stopped, which can mean you
    cancelled successfully, or that a payment is failing.

It only reads your file. Nothing is sent anywhere, and nothing is modified.

THE IDEA, IN PLAIN ENGLISH
--------------------------
A subscription has two fingerprints. The charges are EVENLY SPACED (about 30 days
apart, or 7, or 91...) and the AMOUNT BARELY CHANGES. Everything else is noise:

  * Groceries and coffee are frequent but irregular, and the amounts vary.
  * An electric bill arrives monthly, but the amount changes every time.

So the steps are:

  1. Clean up each description so that "SPOTIFY USA*P4F2A91" and
     "SPOTIFY USA*K9Q1B22" count as the same merchant.
  2. Group charges by merchant.
  3. For each group, measure the gaps between charges. Are they close to weekly,
     monthly, quarterly or yearly?
  4. Check that the amount stays (nearly) the same from one charge to the next.
  5. Check whether the most recent charge is recent enough to still be active.

USAGE
-----
    python subscription_finder.py sample_transactions.csv
    python subscription_finder.py my_export.csv --show-rejected
    python subscription_finder.py my_export.csv --output subscriptions.csv

Educational tool, not financial advice.
"""

# Lets us write modern type hints (like list[str]) on older Python versions.
# It changes nothing about how the program behaves.
from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# A merchant needs at least this many charges before we'll judge it. Two charges
# in a row prove nothing; three starts to look like a pattern.
DEFAULT_MIN_CHARGES = 3

# At least this share of the gaps between charges must fit the schedule. It is
# below 100% on purpose: one late payment creates TWO odd gaps (a long one, then
# a short one as the schedule catches up), and that shouldn't hide a real
# subscription. Groceries and coffee score far lower than this.
MIN_REGULARITY = 0.70

# At least this share of consecutive charge pairs must have matching amounts.
# A single price rise only breaks ONE pair, so it won't hide a real subscription,
# but an electric bill that changes every month breaks nearly all of them.
MIN_STEADY_AMOUNTS = 0.75

# Two amounts "match" if they differ by less than this many dollars, or by less
# than 5% of the larger one, whichever is more generous. (Taxes and rounding
# cause small differences.)
AMOUNT_TOLERANCE_DOLLARS = 1.00
AMOUNT_TOLERANCE_PERCENT = 0.05

# A subscription still counts as active if its last charge was within this many
# billing cycles of the latest date in the file (plus a few days of slack).
ACTIVE_CYCLES = 1.5
ACTIVE_SLACK_DAYS = 3

# Date formats we try, in order. (US-style month/day, as most US banks export.)
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y")


@dataclass(frozen=True)
class Cadence:
    """A billing schedule: its name, its nominal length, and the gaps we accept."""

    name: str
    days: float  # the nominal number of days between charges
    low: int  # the shortest gap we'll accept as "this schedule"
    high: int  # the longest

    def fits(self, gap_days: float) -> bool:
        return self.low <= gap_days <= self.high


# The accepted ranges leave room for weekends and month lengths (28 to 31 days),
# and they don't overlap, so a gap fits at most one schedule.
CADENCES = (
    Cadence("weekly", 7, 5, 9),
    Cadence("every 2 weeks", 14, 12, 16),
    Cadence("monthly", 365 / 12, 27, 34),
    Cadence("quarterly", 365 / 4, 84, 98),
    Cadence("yearly", 365, 350, 380),
)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
# A "dataclass" bundles related values under one name, so the code can say
# `charge.amount` instead of juggling tuples and positions.


@dataclass
class Charge:
    """One purchase from the file."""

    line_number: int  # where it sits in the file (the header row is line 1)
    when: date
    description: str  # exactly as it appeared
    merchant: str  # the cleaned-up name used for grouping
    amount: float  # always positive: how much was spent


@dataclass
class Dataset:
    """Everything read from the file."""

    charges: list[Charge]
    skipped: list[str]  # explanations for rows we had to skip
    spending_sign: str  # "negative" or "positive": which sign means money out
    ignored_credits: int  # rows of the opposite sign (paychecks, refunds), left out


@dataclass
class Subscription:
    """A merchant whose charges look like a subscription."""

    merchant: str
    cadence: Cadence
    charge_count: int
    first_charge: date
    last_charge: date
    latest_amount: float
    active: bool
    note: str  # price change or "may have ended", or empty

    @property
    def per_year(self) -> float:
        return self.latest_amount * 365 / self.cadence.days

    @property
    def per_month(self) -> float:
        return self.per_year / 12


@dataclass
class Rejection:
    """A merchant we looked at and decided is not a subscription, and why."""

    merchant: str
    charge_count: int
    reason: str


@dataclass
class Result:
    subscriptions: list[Subscription]
    rejected: list[Rejection]
    as_of: date  # the date "now" means for the active/ended decision


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


def normalize_merchant(description: str) -> str:
    """Boil a messy bank description down to a stable merchant name.

    Banks tack reference codes onto descriptions, and the codes change every
    time:   "SPOTIFY USA*P4F2A91"  and  "SPOTIFY USA*K9Q1B22"  are one merchant.

    Rules: uppercase it, turn punctuation into spaces, drop any word that
    contains a digit (those are almost always codes, phone numbers or store
    numbers), and keep the first three words that remain.
    """
    text = re.sub(r"[^A-Z0-9 ]+", " ", description.upper())
    words = [word for word in text.split() if not any(ch.isdigit() for ch in word)]
    # If nothing is left (the description was all codes), fall back to the original.
    return " ".join(words[:3]) or description.strip().upper()


def parse_date(text: str) -> date | None:
    """Read a date in any of the common formats, or return None."""
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(text: str) -> float | None:
    """Turn '$1,234.50', '-5.00' or '(12.50)' into a number, or None if unreadable.

    Accountants write negatives in parentheses, and some bank exports do too.
    """
    cleaned = text.strip().replace("$", "").replace(",", "").replace(" ", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    # Python happily reads the words "nan" and "inf" as numbers; they would
    # poison every calculation, so we refuse them.
    if not math.isfinite(number):
        return None
    return -number if negative else number


def choose_spending_sign(amounts: list[float], mode: str) -> str:
    """Decide whether spending is stored as negative or positive numbers.

    Most bank exports show money OUT as negative and money IN as positive; many
    credit card exports do the opposite. With mode "auto" we assume whichever
    sign is more common is the spending, because people make many more purchases
    than they receive paychecks.
    """
    if mode in ("negative", "positive"):
        return mode
    negatives = sum(1 for a in amounts if a < 0)
    positives = sum(1 for a in amounts if a > 0)
    return "negative" if negatives >= positives else "positive"


def load_charges(
    path: Path, date_column: str, description_column: str, amount_column: str, spending_mode: str = "auto"
) -> Dataset:
    """Read the CSV and keep the spending rows.

    Raises ValueError (with a plain-English message) if the file is empty or a
    requested column doesn't exist.
    """
    # "utf-8-sig" quietly handles the invisible marker Excel sometimes puts at the start.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("the file is empty or has no header row")
        for wanted in (date_column, description_column, amount_column):
            if wanted not in reader.fieldnames:
                raise ValueError(
                    f"there is no column named {wanted!r}. "
                    f"Columns in this file: {', '.join(reader.fieldnames)}"
                )

        parsed: list[tuple[int, date, str, float]] = []
        skipped: list[str] = []
        # start=2 because line 1 is the header, so the first data row is line 2.
        for line_number, row in enumerate(reader, start=2):
            when = parse_date(row.get(date_column) or "")
            amount = parse_amount(row.get(amount_column) or "")
            description = (row.get(description_column) or "").strip()
            if when is None or amount is None or not description:
                skipped.append(f"line {line_number}: could not read date, description or amount")
                continue
            if amount != 0:
                parsed.append((line_number, when, description, amount))

    sign = choose_spending_sign([a for *_, a in parsed], spending_mode)
    keep_negative = sign == "negative"
    charges: list[Charge] = []
    ignored = 0
    for line_number, when, description, amount in parsed:
        if (amount < 0) == keep_negative:
            charges.append(Charge(line_number, when, description, normalize_merchant(description), abs(amount)))
        else:
            ignored += 1  # money coming in, such as a paycheck or a refund
    return Dataset(charges, skipped, sign, ignored)


# ---------------------------------------------------------------------------
# The detective work
# ---------------------------------------------------------------------------


def amounts_match(a: float, b: float) -> bool:
    """Do two charge amounts count as 'the same price'?"""
    return abs(a - b) <= max(AMOUNT_TOLERANCE_DOLLARS, AMOUNT_TOLERANCE_PERCENT * max(a, b))


def cadence_for(gap_days: float) -> Cadence | None:
    """Which billing schedule (if any) does this typical gap match?"""
    return next((c for c in CADENCES if c.fits(gap_days)), None)


def evaluate(merchant: str, charges: list[Charge], as_of: date) -> Subscription | Rejection:
    """Decide whether one merchant's charges look like a subscription."""
    charges = sorted(charges, key=lambda c: c.when)
    count = len(charges)

    # --- Test 1: are the charges evenly spaced? -----------------------------
    gaps = [(b.when - a.when).days for a, b in zip(charges, charges[1:])]
    typical_gap = statistics.median(gaps)  # the median ignores the odd late payment
    cadence = cadence_for(typical_gap)
    if cadence is None:
        return Rejection(merchant, count, f"not evenly spaced (typical gap {typical_gap:g} days)")
    on_schedule = sum(cadence.fits(g) for g in gaps) / len(gaps)
    if on_schedule < MIN_REGULARITY:
        return Rejection(
            merchant, count, f"too irregular for {cadence.name} (only {on_schedule:.0%} of gaps fit)"
        )

    # --- Test 2: does the price stay put? -----------------------------------
    # We compare each charge with the one before it. A price rise breaks one
    # pair; a bill that varies every month breaks most of them.
    amounts = [c.amount for c in charges]
    pairs = list(zip(amounts, amounts[1:]))
    steady = sum(amounts_match(a, b) for a, b in pairs) / len(pairs)
    if steady < MIN_STEADY_AMOUNTS:
        return Rejection(merchant, count, f"amounts vary too much (only {steady:.0%} of charges match the last)")

    # --- It looks like a subscription. Add the useful observations. ---------
    notes: list[str] = []

    # The most recent place where the price jumped, if there was one.
    for index in range(len(pairs) - 1, -1, -1):
        old, new = pairs[index]
        if not amounts_match(old, new):
            change = (new - old) / old
            direction = "rose" if new > old else "fell"
            notes.append(
                f"price {direction} {old:.2f} -> {new:.2f} ({change:+.0%}) on {charges[index + 1].when}"
            )
            break

    # Is it still being charged? Compare the last charge with the newest date in the file.
    days_since = (as_of - charges[-1].when).days
    active = days_since <= cadence.days * ACTIVE_CYCLES + ACTIVE_SLACK_DAYS
    if not active:
        notes.append(f"no charge since {charges[-1].when}; may have ended")

    return Subscription(
        merchant=merchant,
        cadence=cadence,
        charge_count=count,
        first_charge=charges[0].when,
        last_charge=charges[-1].when,
        latest_amount=amounts[-1],  # the CURRENT price, after any increase
        active=active,
        note="; ".join(notes),
    )


def find_subscriptions(
    charges: list[Charge], min_charges: int = DEFAULT_MIN_CHARGES, as_of: date | None = None
) -> Result:
    """Group charges by merchant and test each group."""
    if as_of is None:
        # "Now" means the newest date in the file, not today's date, so a file
        # exported last month gives the same answer however old it is.
        as_of = max((c.when for c in charges), default=date.today())

    groups: dict[str, list[Charge]] = defaultdict(list)
    for charge in charges:
        groups[charge.merchant].append(charge)

    subscriptions: list[Subscription] = []
    rejected: list[Rejection] = []
    for merchant, members in groups.items():
        if len(members) < min_charges:
            continue  # not enough history to say anything
        outcome = evaluate(merchant, members, as_of)
        (subscriptions if isinstance(outcome, Subscription) else rejected).append(outcome)

    # Most expensive first. Ties are broken by name so the order never changes.
    subscriptions.sort(key=lambda s: (-s.per_year, s.merchant))
    rejected.sort(key=lambda r: (-r.charge_count, r.merchant))
    return Result(subscriptions, rejected, as_of)


# ---------------------------------------------------------------------------
# Presenting results
# ---------------------------------------------------------------------------


def money(value: float) -> str:
    return f"{value:,.2f}"


def format_report(result: Result, dataset: Dataset, show_rejected: bool) -> str:
    """Build the human-readable report as one block of text."""
    charges = dataset.charges
    if not charges:
        return "No spending found in the file."

    first = min(c.when for c in charges)
    last = max(c.when for c in charges)
    lines = [
        f"Read {len(charges):,} charges from {first} to {last} "
        f"(spending = {dataset.spending_sign} amounts; {dataset.ignored_credits} other rows ignored)."
    ]

    subs = result.subscriptions
    active = [s for s in subs if s.active]
    ended = len(subs) - len(active)
    if not subs:
        lines.append("No subscriptions found.")
    else:
        ended_text = f", {ended} possibly ended" if ended else ""
        lines.append(f"Found {len(subs)} likely subscription(s): {len(active)} active{ended_text}.")
        lines.append(
            f"Active subscriptions cost about {money(sum(s.per_year for s in active))} a year "
            f"({money(sum(s.per_month for s in active))} a month)."
        )
        lines.append("")
        lines.extend(format_table(subs))

    if show_rejected and result.rejected:
        lines.append("")
        lines.append("Considered but not subscriptions:")
        width = max(len(r.merchant) for r in result.rejected)
        lines.extend(f"  {r.merchant.title():<{width}}  ({r.charge_count} charges) {r.reason}" for r in result.rejected)

    if dataset.skipped:
        lines.append("")
        lines.append(f"Skipped {len(dataset.skipped)} row(s) that could not be read:")
        lines.extend(f"  {reason}" for reason in dataset.skipped)

    return "\n".join(lines)


def format_table(subs: list[Subscription]) -> list[str]:
    """Lay the subscriptions out as an aligned text table."""
    headers = ["Merchant", "Every", "Amount", "Per month", "Per year", "Last charge", "Notes"]
    rows = [
        [
            s.merchant.title(),
            s.cadence.name,
            money(s.latest_amount),
            money(s.per_month),
            money(s.per_year),
            str(s.last_charge),
            s.note,
        ]
        for s in subs
    ]
    widths = [max(len(r[i]) for r in [headers] + rows) for i in range(len(headers))]
    text_columns = {0, 1, 6}  # left-align words; right-align numbers

    def render(cells: list[str]) -> str:
        parts = [c.ljust(w) if i in text_columns else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths))]
        return "  ".join(parts).rstrip()

    return [render(headers), render(["-" * w for w in widths])] + [render(r) for r in rows]


def write_csv(path: Path, subs: list[Subscription]) -> None:
    """Save the subscriptions to a new CSV file."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["merchant", "every", "charges", "latest_amount", "per_month", "per_year",
             "first_charge", "last_charge", "status", "notes"]
        )
        for s in subs:
            writer.writerow([
                s.merchant.title(), s.cadence.name, s.charge_count, f"{s.latest_amount:.2f}",
                f"{s.per_month:.2f}", f"{s.per_year:.2f}", s.first_charge, s.last_charge,
                "active" if s.active else "possibly ended", s.note,
            ])


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------


def date_argument(text: str) -> date:
    """Let argparse read a --as-of date, with a friendly error if it's wrong."""
    parsed = parse_date(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not a date: {text!r} (try 2026-10-25)")
    return parsed


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, run the analysis, print the report.

    Returns 0 on success and 1 on a problem with the input. (The number
    returned is the program's "exit code", which other tools can check.)
    """
    parser = argparse.ArgumentParser(description="Find recurring charges (subscriptions) in a transactions CSV.")
    parser.add_argument("csv_file", type=Path, help="the transactions CSV to examine")
    parser.add_argument("--date", default="date", metavar="COLUMN", help="name of the date column (default: date)")
    parser.add_argument("--description", default="description", metavar="COLUMN",
                        help="name of the description column (default: description)")
    parser.add_argument("--amount", default="amount", metavar="COLUMN", help="name of the amount column (default: amount)")
    parser.add_argument("--spending", choices=("auto", "negative", "positive"), default="auto",
                        help="which sign means money spent (default: auto-detect)")
    parser.add_argument("--min-charges", type=int, default=DEFAULT_MIN_CHARGES, metavar="N",
                        help=f"charges needed before a merchant is judged (default: {DEFAULT_MIN_CHARGES})")
    parser.add_argument("--as-of", type=date_argument, metavar="DATE",
                        help="treat this as today when deciding what has ended (default: newest date in the file)")
    parser.add_argument("--show-rejected", action="store_true",
                        help="also list merchants that were examined and why they are not subscriptions")
    parser.add_argument("--output", type=Path, metavar="FILE", help="also save the subscriptions to this CSV file")
    args = parser.parse_args(argv)

    # parser.error() prints a friendly message and exits with code 2.
    if args.min_charges < 2:
        parser.error("--min-charges must be at least 2")

    if not args.csv_file.is_file():
        print(f"Not a file: {args.csv_file}", file=sys.stderr)
        return 1

    try:
        dataset = load_charges(args.csv_file, args.date, args.description, args.amount, args.spending)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    result = find_subscriptions(dataset.charges, args.min_charges, args.as_of)
    print(format_report(result, dataset, args.show_rejected))

    if args.output:
        write_csv(args.output, result.subscriptions)
        print(f"\nSaved {len(result.subscriptions)} subscription(s) to {args.output}")
    return 0


# This guard means "only run main() when the file is executed directly", not
# when another file (like the tests) merely imports it.
if __name__ == "__main__":
    sys.exit(main())
