# subscription-finder

A command-line tool that reads a bank or card export (a CSV) and finds the **subscriptions**
hiding in it: the charges that repeat on a schedule for the same price. For each one it
shows what it costs per month and per year, and it flags two things that are easy to miss:

- **Price increases** ("Netflix went from 15.49 to 17.99 in March")
- **Subscriptions that may have ended**, which can mean a clean cancellation or a failing payment

Written in plain Python with no dependencies. The code is heavily commented so it can be
read top to bottom by someone who doesn't write code. The sample data is synthetic.
The tool only reads your file, and nothing leaves your computer.

## Try it

```
python subscription_finder.py sample_transactions.csv --show-rejected
```

```
Read 227 charges from 2025-09-01 to 2026-10-20 (spending = negative amounts; 30 other rows ignored).
Found 5 likely subscription(s): 4 active, 1 possibly ended.
Active subscriptions cost about 735.64 a year (61.30 a month).

Merchant         Every      Amount  Per month  Per year  Last charge  Notes
---------------  ---------  ------  ---------  --------  -----------  ----------------------------------------------
Pest Shield Svc  quarterly   85.00      28.33    340.00  2026-09-14
Planet Fitness   monthly     24.99      24.99    299.88  2026-05-01  no charge since 2026-05-01; may have ended
Netflix Com      monthly     17.99      17.99    215.88  2026-10-05  price rose 15.49 -> 17.99 (+16%) on 2026-03-05
Spotify Usa      monthly     11.99      11.99    143.88  2026-10-12
Apple Com Bill   monthly      2.99       2.99     35.88  2026-10-20

Considered but not subscriptions:
  Corner Grocery       (70 charges) too irregular for weekly (only 41% of gaps fit)
  Daily Grind Coffee   (55 charges) too irregular for weekly (only 24% of gaps fit)
  Shell Oil            (18 charges) not evenly spaced (typical gap 18 days)
  Amzn Mktp Us         (14 charges) not evenly spaced (typical gap 35 days)
  Electric Co Autopay  (14 charges) amounts vary too much (only 15% of charges match the last)
```

The totals count only **active** subscriptions, so the ended Planet Fitness is listed but
not added in. The tool treats the monthly electric bill as a non-subscription because its
amount changes every month, and it ignores the payroll deposits.

| Option | What it does |
| --- | --- |
| `--show-rejected` | Also list the merchants it examined and why they aren't subscriptions. |
| `--output FILE` | Also save the subscriptions to a new CSV file. |
| `--min-charges N` | Charges needed before a merchant is judged. Default 3, minimum 2. |
| `--as-of DATE` | Treat this date as "today" when deciding what has ended. Default: the newest date in the file. |
| `--spending auto\|negative\|positive` | Which sign means money spent. Default: auto-detect. |
| `--date`, `--description`, `--amount` | Column names, if your export uses different ones. |

Dates may be written `2026-03-05`, `03/05/2026`, `03/05/26`, `05-Mar-2026` or `Mar 05, 2026`.
Amounts may look like `$1,234.50` or `(12.50)`.

## How it works

A subscription has two fingerprints: the charges are **evenly spaced**, and the **amount
barely changes**. Everything else is noise. The tool works through five steps:

1. **Clean the names.** Banks add changing reference codes, so `SPOTIFY USA*P4F2A91` and
   `SPOTIFY USA*K9Q1B22` must count as one merchant. The tool drops any word containing a
   digit (codes, phone numbers, store numbers) and keeps the first three words left.
2. **Group by merchant.** Merchants with fewer than three charges are not judged. Two
   charges prove nothing.
3. **Check the spacing.** It measures the gaps between charges and takes the *median* gap
   (the middle one, so an odd late payment doesn't throw it off). That gap must match a
   known schedule, and at least 70% of the gaps must fit it:

   | Schedule | Gap accepted |
   | --- | --- |
   | weekly | 5 to 9 days |
   | every 2 weeks | 12 to 16 days |
   | monthly | 27 to 34 days |
   | quarterly | 84 to 98 days |
   | yearly | 350 to 380 days |

   The 70% allowance is deliberate. One late payment creates *two* odd gaps (a long one,
   then a short one as the schedule catches up), and that shouldn't hide a real
   subscription.
4. **Check the price.** Each charge is compared with the one before it, and at least 75%
   of those pairs must match (within $1 or 5%). A price rise breaks only *one* pair, so a
   subscription that raised its price is still found, and the tool reports the change. An
   electric bill that differs every month breaks nearly every pair and is rejected.
5. **Check whether it's still going.** If the last charge is more than 1.5 billing cycles
   (plus 3 days) older than the newest date in the file, the subscription is marked
   possibly ended.

Yearly cost is the **latest** amount × the number of billing cycles in a year (so a price
rise is already reflected), and monthly cost is that divided by 12.

## What to look for when reading the code

Everything is in [`subscription_finder.py`](subscription_finder.py), in the order the work
happens:

- **The settings at the top** hold every threshold in one place, each with a comment
  explaining why it has the value it does. If the tool is too strict or too loose for your
  data, this is where you'd tune it.
- **`normalize_merchant`** is step 1. It is small, but most of the tool's accuracy depends on it.
- **`parse_amount`** reads `(12.50)` as negative and refuses `nan` and `inf`, which Python
  would otherwise accept as numbers.
- **`load_charges`** keeps going past bad rows and reports each one by line number.
- **`evaluate`** is the heart of the tool: the spacing test, the price test, then the
  notes about price changes and ended subscriptions. Every rejection carries a plain-English reason.
- **`find_subscriptions`** groups, judges and sorts. "Now" means the newest date in the
  file, not today's date, so an old export gives the same answer next year.

## Tests

```
python -m unittest -v
```

58 tests. [`test_subscription_finder.py`](test_subscription_finder.py) reads like a
specification. It builds tiny made-up histories (a steady monthly charge, a utility bill, a
price rise, a late payment, a cancelled service) and checks the verdict for each. It also
checks the sample file against answers worked out by hand: the five subscriptions, the
735.64 yearly total, and the rejections.

## Limits

This is a first-pass screen, not a financial adviser. A finding means "worth a look".

- **It judges by pattern, not by what the charge is.** A car payment or a regular
  donation looks exactly like a subscription, and that is arguably correct. A
  subscription that bills at irregular intervals, or one that switched between monthly
  and yearly billing, can be missed.
- **Merchant names are matched crudely.** If one service appears under two different
  descriptions (say, a billing company that changed its name), it is counted as two merchants.
  Two different services that share their first three words are counted as one.
- **Price changes are reported only for the most recent one.**
- **Cancelled doesn't always mean ended.** The tool can't tell a cancellation from a
  failed payment, so check "possibly ended" items against your account.
- **Totals assume the latest price continues.** It does not predict future price rises.
- **It needs history.** Three billing cycles are needed to find a subscription, so a
  yearly one needs a file covering at least three years.
