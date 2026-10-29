"""
Tests for subscription_finder.py.

Run them with:   python -m unittest -v

Read the test names like a specification: each one states something the tool
promises to do. Many tests build a small, made-up list of charges so the
expected answer is obvious from the setup alone.
"""

import contextlib
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import subscription_finder as sf


# ---------------------------------------------------------------------------
# Helpers that build fake data
# ---------------------------------------------------------------------------


def make_charges(merchant, start, gap_days, amounts):
    """Make a list of charges: one every `gap_days` days, with the given amounts."""
    return [
        sf.Charge(i + 2, start + timedelta(days=gap_days * i), merchant, merchant, amount)
        for i, amount in enumerate(amounts)
    ]


def write_csv(text):
    """Save text to a temporary CSV file and return its path."""
    folder = tempfile.mkdtemp()
    path = Path(folder) / "t.csv"
    path.write_text(text, encoding="utf-8")
    return path


START = date(2026, 1, 1)


class NormalizeMerchantTests(unittest.TestCase):
    def test_reference_codes_are_dropped(self):
        a = sf.normalize_merchant("SPOTIFY USA*P4F2A91")
        b = sf.normalize_merchant("SPOTIFY USA*K9Q1B22")
        self.assertEqual(a, b)
        self.assertEqual(a, "SPOTIFY USA")

    def test_phone_and_store_numbers_are_dropped(self):
        self.assertEqual(sf.normalize_merchant("NETFLIX.COM 866-579-7172"), "NETFLIX COM")
        self.assertEqual(sf.normalize_merchant("Planet Fitness #4412"), "PLANET FITNESS")

    def test_case_and_punctuation_do_not_matter(self):
        self.assertEqual(sf.normalize_merchant("apple.com/bill"), sf.normalize_merchant("APPLE COM BILL"))

    def test_only_first_three_words_are_kept(self):
        self.assertEqual(sf.normalize_merchant("ONE TWO THREE FOUR FIVE"), "ONE TWO THREE")

    def test_all_code_description_falls_back_instead_of_going_blank(self):
        self.assertEqual(sf.normalize_merchant("12345 67890"), "12345 67890")


class ParsingTests(unittest.TestCase):
    def test_date_formats(self):
        expected = date(2026, 3, 5)
        for text in ("2026-03-05", "03/05/2026", "03/05/26", "05-Mar-2026", "Mar 05, 2026"):
            self.assertEqual(sf.parse_date(text), expected, text)

    def test_bad_date_is_none(self):
        self.assertIsNone(sf.parse_date("yesterday"))
        self.assertIsNone(sf.parse_date(""))

    def test_amount_formats(self):
        self.assertEqual(sf.parse_amount("$1,234.50"), 1234.5)
        self.assertEqual(sf.parse_amount("-5.00"), -5.0)
        self.assertEqual(sf.parse_amount("(12.50)"), -12.5)

    def test_bad_amounts_are_none(self):
        for text in ("", "n/a", "nan", "inf", "-inf"):
            self.assertIsNone(sf.parse_amount(text), text)


class SpendingSignTests(unittest.TestCase):
    def test_auto_picks_the_more_common_sign(self):
        self.assertEqual(sf.choose_spending_sign([-5, -6, -7, 100], "auto"), "negative")
        self.assertEqual(sf.choose_spending_sign([5, 6, 7, -100], "auto"), "positive")

    def test_explicit_choice_wins(self):
        self.assertEqual(sf.choose_spending_sign([-5, -6, -7], "positive"), "positive")


class LoadingTests(unittest.TestCase):
    def test_reads_spending_and_ignores_income(self):
        path = write_csv("date,description,amount\n2026-01-01,COFFEE,-4.50\n2026-01-02,PAYROLL,2000\n")
        data = sf.load_charges(path, "date", "description", "amount")
        self.assertEqual(len(data.charges), 1)
        self.assertEqual(data.charges[0].amount, 4.5)  # stored as a positive amount
        self.assertEqual(data.ignored_credits, 1)

    def test_credit_card_style_positive_spending(self):
        path = write_csv("date,description,amount\n2026-01-01,A,5\n2026-01-02,B,6\n2026-01-03,C,7\n2026-01-04,REFUND,-3\n")
        data = sf.load_charges(path, "date", "description", "amount")
        self.assertEqual(data.spending_sign, "positive")
        self.assertEqual(len(data.charges), 3)

    def test_bad_rows_are_skipped_with_line_numbers(self):
        path = write_csv("date,description,amount\n2026-01-01,OK,-1\nnot a date,BAD,-2\n2026-01-03,BAD,abc\n")
        data = sf.load_charges(path, "date", "description", "amount")
        self.assertEqual(len(data.charges), 1)
        self.assertEqual(len(data.skipped), 2)
        self.assertIn("line 3", data.skipped[0])
        self.assertIn("line 4", data.skipped[1])

    def test_missing_column_gives_a_helpful_error(self):
        path = write_csv("when,what,howmuch\n2026-01-01,A,-1\n")
        with self.assertRaises(ValueError) as caught:
            sf.load_charges(path, "date", "description", "amount")
        self.assertIn("when", str(caught.exception))  # lists the real columns

    def test_custom_column_names(self):
        path = write_csv("when,what,howmuch\n2026-01-01,A,-1\n")
        data = sf.load_charges(path, "when", "what", "howmuch")
        self.assertEqual(len(data.charges), 1)

    def test_empty_file_is_an_error(self):
        with self.assertRaises(ValueError):
            sf.load_charges(write_csv(""), "date", "description", "amount")

    def test_excel_byte_order_mark_is_handled(self):
        path = write_csv("﻿date,description,amount\n2026-01-01,A,-1\n")
        self.assertEqual(len(sf.load_charges(path, "date", "description", "amount").charges), 1)


class AmountMatchTests(unittest.TestCase):
    def test_small_differences_match(self):
        self.assertTrue(sf.amounts_match(9.99, 10.49))  # within a dollar

    def test_big_percentage_on_big_bill_does_not_match(self):
        self.assertFalse(sf.amounts_match(100, 120))

    def test_five_percent_of_a_large_bill_matches(self):
        self.assertTrue(sf.amounts_match(1000, 1040))  # 4% but more than $1


class CadenceTests(unittest.TestCase):
    def test_typical_gaps(self):
        self.assertEqual(sf.cadence_for(7).name, "weekly")
        self.assertEqual(sf.cadence_for(14).name, "every 2 weeks")
        for days in (28, 30, 31):
            self.assertEqual(sf.cadence_for(days).name, "monthly")
        self.assertEqual(sf.cadence_for(91).name, "quarterly")
        self.assertEqual(sf.cadence_for(365).name, "yearly")

    def test_in_between_gaps_match_nothing(self):
        for days in (3, 20, 50, 200):
            self.assertIsNone(sf.cadence_for(days), days)

    def test_ranges_do_not_overlap(self):
        for gap in range(1, 400):
            self.assertLessEqual(sum(c.fits(gap) for c in sf.CADENCES), 1, gap)


class EvaluateTests(unittest.TestCase):
    def run_eval(self, charges, as_of=None):
        as_of = as_of or max(c.when for c in charges)
        return sf.evaluate("X", charges, as_of)

    def test_steady_monthly_charge_is_a_subscription(self):
        result = self.run_eval(make_charges("X", START, 30, [9.99] * 6))
        self.assertIsInstance(result, sf.Subscription)
        self.assertEqual(result.cadence.name, "monthly")
        self.assertTrue(result.active)
        self.assertEqual(result.note, "")

    def test_cost_math_monthly(self):
        sub = self.run_eval(make_charges("X", START, 30, [10.0] * 4))
        self.assertAlmostEqual(sub.per_year, 10 * 12, delta=0.01)
        self.assertAlmostEqual(sub.per_month, 10, delta=0.01)

    def test_cost_math_quarterly_and_weekly(self):
        quarterly = self.run_eval(make_charges("X", START, 91, [100.0] * 4))
        self.assertAlmostEqual(quarterly.per_year, 400, delta=0.01)
        weekly = self.run_eval(make_charges("X", START, 7, [5.0] * 6))
        self.assertAlmostEqual(weekly.per_year, 5 * 365 / 7, delta=0.01)

    def test_irregular_spacing_is_rejected(self):
        days = [0, 3, 40, 44, 90, 95]
        charges = [sf.Charge(i, START + timedelta(days=d), "X", "X", 5.0) for i, d in enumerate(days)]
        self.assertIsInstance(self.run_eval(charges), sf.Rejection)

    def test_varying_amounts_are_rejected_like_a_utility_bill(self):
        result = self.run_eval(make_charges("X", START, 30, [58, 91, 142, 77, 120, 64]))
        self.assertIsInstance(result, sf.Rejection)
        self.assertIn("amounts vary", result.reason)

    def test_one_price_rise_is_noted_not_rejected(self):
        result = self.run_eval(make_charges("X", START, 30, [15.49] * 3 + [17.99] * 3))
        self.assertIsInstance(result, sf.Subscription)
        self.assertEqual(result.latest_amount, 17.99)  # reports the CURRENT price
        self.assertIn("rose 15.49 -> 17.99", result.note)
        self.assertIn("+16%", result.note)

    def test_price_drop_is_noted(self):
        result = self.run_eval(make_charges("X", START, 30, [20.0] * 3 + [10.0] * 3))
        self.assertIn("fell 20.00 -> 10.00", result.note)

    def test_one_late_payment_does_not_hide_a_subscription(self):
        charges = make_charges("X", START, 30, [9.99] * 8)
        charges[4].when += timedelta(days=12)  # one very late payment
        self.assertIsInstance(self.run_eval(charges), sf.Subscription)

    def test_stopped_subscription_is_flagged_as_possibly_ended(self):
        charges = make_charges("X", START, 30, [9.99] * 4)
        result = self.run_eval(charges, as_of=charges[-1].when + timedelta(days=120))
        self.assertFalse(result.active)
        self.assertIn("may have ended", result.note)

    def test_not_yet_overdue_counts_as_active(self):
        charges = make_charges("X", START, 30, [9.99] * 4)
        result = self.run_eval(charges, as_of=charges[-1].when + timedelta(days=40))
        self.assertTrue(result.active)

    def test_input_order_does_not_matter(self):
        charges = make_charges("X", START, 30, [9.99] * 5)
        forward = self.run_eval(charges)
        backward = self.run_eval(list(reversed(charges)))
        self.assertEqual(forward, backward)


class FindSubscriptionsTests(unittest.TestCase):
    def test_groups_by_merchant(self):
        charges = make_charges("NETFLIX", START, 30, [15.0] * 5) + make_charges("GYM", START, 30, [30.0] * 5)
        result = sf.find_subscriptions(charges)
        self.assertEqual({s.merchant for s in result.subscriptions}, {"NETFLIX", "GYM"})

    def test_too_few_charges_are_ignored_entirely(self):
        result = sf.find_subscriptions(make_charges("X", START, 30, [9.99] * 2))
        self.assertEqual(result.subscriptions, [])
        self.assertEqual(result.rejected, [])

    def test_min_charges_can_be_lowered(self):
        result = sf.find_subscriptions(make_charges("X", START, 30, [9.99] * 2), min_charges=2)
        self.assertEqual(len(result.subscriptions), 1)

    def test_most_expensive_first(self):
        charges = make_charges("CHEAP", START, 30, [5.0] * 4) + make_charges("PRICEY", START, 30, [50.0] * 4)
        names = [s.merchant for s in sf.find_subscriptions(charges).subscriptions]
        self.assertEqual(names, ["PRICEY", "CHEAP"])

    def test_as_of_defaults_to_newest_date_in_file(self):
        charges = make_charges("X", START, 30, [9.99] * 4)
        self.assertEqual(sf.find_subscriptions(charges).as_of, charges[-1].when)

    def test_as_of_can_be_overridden(self):
        charges = make_charges("X", START, 30, [9.99] * 4)
        late = charges[-1].when + timedelta(days=200)
        result = sf.find_subscriptions(charges, as_of=late)
        self.assertFalse(result.subscriptions[0].active)

    def test_no_charges_does_not_crash(self):
        result = sf.find_subscriptions([])
        self.assertEqual(result.subscriptions, [])


class SampleFileTests(unittest.TestCase):
    """The shipped sample data, checked against answers worked out by hand."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "sample_transactions.csv"
        cls.data = sf.load_charges(path, "date", "description", "amount")
        cls.result = sf.find_subscriptions(cls.data.charges)
        cls.by_name = {s.merchant: s for s in cls.result.subscriptions}

    def test_finds_exactly_the_five_planted_subscriptions(self):
        self.assertEqual(
            set(self.by_name),
            {"NETFLIX COM", "SPOTIFY USA", "APPLE COM BILL", "PLANET FITNESS", "PEST SHIELD SVC"},
        )

    def test_noise_is_rejected(self):
        rejected = {r.merchant for r in self.result.rejected}
        self.assertTrue({"CORNER GROCERY", "DAILY GRIND COFFEE", "ELECTRIC CO AUTOPAY"} <= rejected)

    def test_payroll_is_ignored(self):
        self.assertNotIn("ACME CORP PAYROLL", {s.merchant for s in self.result.subscriptions})
        self.assertEqual(self.data.ignored_credits, 30)

    def test_only_planet_fitness_has_ended(self):
        self.assertEqual([s.merchant for s in self.result.subscriptions if not s.active], ["PLANET FITNESS"])

    def test_netflix_price_rise_is_reported(self):
        self.assertIn("15.49 -> 17.99", self.by_name["NETFLIX COM"].note)

    def test_active_yearly_total(self):
        # Netflix 17.99*12 + Spotify 11.99*12 + Apple 2.99*12 + Pest 85*4
        total = sum(s.per_year for s in self.result.subscriptions if s.active)
        self.assertAlmostEqual(total, 215.88 + 143.88 + 35.88 + 340.00, delta=0.05)


class OutputTests(unittest.TestCase):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sf.main(list(args))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    SAMPLE = str(Path(__file__).parent / "sample_transactions.csv")

    def test_report_contains_totals_and_names(self):
        code, out, _ = self.run_main(self.SAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("735.64 a year", out)
        self.assertIn("Netflix Com", out)
        self.assertIn("may have ended", out)

    def test_rejected_hidden_unless_asked(self):
        _, plain, _ = self.run_main(self.SAMPLE)
        _, verbose, _ = self.run_main(self.SAMPLE, "--show-rejected")
        self.assertNotIn("Considered but not", plain)
        self.assertIn("Considered but not", verbose)
        self.assertIn("Electric Co Autopay", verbose)

    def test_output_csv_is_written(self):
        target = Path(tempfile.mkdtemp()) / "out.csv"
        code, _, _ = self.run_main(self.SAMPLE, "--output", str(target))
        self.assertEqual(code, 0)
        lines = target.read_text().splitlines()
        self.assertEqual(len(lines), 6)  # header + 5 subscriptions
        self.assertTrue(lines[0].startswith("merchant,"))

    def test_missing_file_is_a_clean_error(self):
        code, _, err = self.run_main("nope.csv")
        self.assertEqual(code, 1)
        self.assertIn("Not a file", err)

    def test_bad_column_is_a_clean_error(self):
        code, _, err = self.run_main(self.SAMPLE, "--amount", "cost")
        self.assertEqual(code, 1)
        self.assertIn("cost", err)

    def test_min_charges_below_two_is_refused(self):
        code, _, _ = self.run_main(self.SAMPLE, "--min-charges", "1")
        self.assertEqual(code, 2)

    def test_bad_as_of_date_is_refused(self):
        code, _, _ = self.run_main(self.SAMPLE, "--as-of", "soon")
        self.assertEqual(code, 2)

    def test_as_of_changes_the_ended_list(self):
        _, out, _ = self.run_main(self.SAMPLE, "--as-of", "2027-06-01")
        self.assertIn("0 active", out)

    def test_no_spending_message(self):
        path = write_csv("date,description,amount\n")
        _, out, _ = self.run_main(str(path))
        self.assertIn("No spending found", out)

    def test_skipped_rows_are_reported(self):
        path = write_csv("date,description,amount\n2026-01-01,A,-1\nbad,B,-2\n")
        _, out, _ = self.run_main(str(path))
        self.assertIn("Skipped 1 row", out)


if __name__ == "__main__":
    unittest.main()
