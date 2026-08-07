"""Per-day request allowances, and spending them at a sustainable rate.

`Pacer` already owns the per-minute half of a free tier: a minimum gap between
calls and a rolling sixty-second token window. Neither needs to survive a
restart, and persisting them would put sqlite in the hot path of every model
call.

A per-day ceiling is the opposite on both counts. It is the limit a fallback
provider actually reaches - Gemini allows a four-figure number of requests a
day where Groq's constraint is tokens per minute - and an in-memory counter
would reset to zero on exactly the restart a runaway loop makes most likely.
So `Budget` counts requests, is seeded from the database once per cycle, and
never touches sqlite itself: `BudgetLedger` owns that, and is only ever called
from the thread that owns the connection.

The second job here is pacing that ceiling. Two hundred requests fired off in
the first twenty minutes leaves nothing for the rest of the day, which is how a
"backup" provider ends up unavailable exactly when the primary runs out. But
spreading evenly from the first request is just as wrong - one alert digest is
fifteen extractions, and a provider that refuses to burst is not a fallback
either. `spread_delay` splits the difference: burst freely while the allowance
is plentiful, then stretch as it runs down.
"""

import threading
import time
from datetime import datetime, timedelta

#: Seconds in the rolling window a daily ceiling is measured over. Rolling
#: rather than calendar, matching `SpendLimiter`: Google resets free-tier quota
#: at midnight Pacific, and counting the last 24 hours is stricter than that
#: from every timezone, which is the safe direction for a ceiling nobody wants
#: to discover by being refused.
WINDOW_SECONDS = 86_400.0

#: Fraction of the daily allowance that may be spent at full speed before
#: pacing engages. Below this, `spread_delay` is zero and bursts pass straight
#: through; above it the interval ramps from half the even spacing up to the
#: full amount as the allowance empties.
SPREAD_RESERVE = 0.5


class Budget:
    """One provider's daily request allowance, and how fast it may be spent.

    Complements `Pacer` rather than replacing it. Both are consulted before a
    request goes out, and the larger delay wins.

    `daily_limit` of 0 means no daily ceiling, which is the right answer for
    Groq and Anthropic - their constraints are per-minute and monetary
    respectively. `.env.example` documents 0 as disabling the check.
    """

    def __init__(self, daily_limit=0, window_seconds=WINDOW_SECONDS,
                 reserve=SPREAD_RESERVE, clock=time.monotonic):
        self.daily_limit = max(0, int(daily_limit or 0))
        self.window_seconds = window_seconds
        self.reserve = reserve
        self._clock = clock
        self.requests_today = 0
        self.denied_until = 0.0
        # Guards the counters only, never sqlite. The pipeline is sequential,
        # but ClassificationRunner.run and a scheduler cycle can interleave at
        # await points while sharing one pool, and an uncontended lock is free.
        self._lock = threading.Lock()

    # Seeding ---------------------------------------------------------------

    def seed(self, requests_today, denied_day=False, now=None):
        """Adopt counts read back from the usage ledger.

        Summary:
            Set the day's spend from persisted history, once per cycle.

        Parameters:
            requests_today (int): Requests already recorded inside the window.
            denied_day (bool): Whether the provider reported a per-day limit
                inside the window. Its own refusal outranks our arithmetic - a
                shared project quota or a limit lowered upstream can exhaust an
                allowance our count did not predict.
            now (float | None): Current monotonic time. Defaults to the clock.
        """
        now = self._clock() if now is None else now
        with self._lock:
            self.requests_today = max(0, int(requests_today or 0))
            if denied_day:
                self.denied_until = now + self.window_seconds

    # Queries ---------------------------------------------------------------

    def exhausted(self, now):
        """
        Summary:
            Whether the daily allowance is gone.

        Parameters:
            now (float): Current monotonic time.

        Returns:
            bool: True when a per-day denial is still in force, or the counted
                requests have reached the ceiling.
        """
        if now < self.denied_until:
            return True
        if not self.daily_limit:
            return False
        return self.requests_today >= self.daily_limit

    def has_headroom(self, now):
        """
        Summary:
            Whether one more request fits inside the daily allowance.

        Parameters:
            now (float): Current monotonic time.

        Returns:
            bool: True when the request may be attempted.
        """
        return not self.exhausted(now)

    def remaining(self):
        """
        Summary:
            Requests left in the daily allowance.

        Returns:
            int | None: The remaining count, or None when there is no daily
                ceiling - which is not the same as zero and must not be
                formatted as "0 left" in the UI.
        """
        if not self.daily_limit:
            return None
        return max(0, self.daily_limit - self.requests_today)

    def reset_in(self, now):
        """
        Summary:
            Seconds until the allowance could plausibly free up.

        Parameters:
            now (float): Current monotonic time.

        Returns:
            float: Seconds remaining on an active denial, or the full window
                when the ceiling was reached by counting. Approximate on
                purpose - it feeds a "try again in about..." message, not a
                timer anything waits on.
        """
        if now < self.denied_until:
            return self.denied_until - now
        return self.window_seconds

    def spread_delay(self, now):
        """How long to hold off so the allowance lasts the rest of the window.

        Zero until `reserve` of the day's requests are spent, so a burst of
        alert extractions passes straight through while there is plenty left.
        Past that the interval ramps with usage, reaching the full even spacing
        as the allowance empties.

        Summary:
            Report the pacing interval the daily allowance currently implies.

        Parameters:
            now (float): Current monotonic time.

        Returns:
            float: Seconds to wait before the next request. 0.0 when there is
                no daily ceiling, or while the reserve is untouched.

        Note:
            A delay longer than a caller's patience is not an error. The pool
            compares this against its sleep budget and reads anything larger as
            "not available now", which fails over to another provider instead
            of stalling. That composition is the point: pacing and failover are
            the same decision seen from two sides.
        """
        if not self.daily_limit:
            return 0.0
        used = self.requests_today / self.daily_limit
        if used < self.reserve:
            return 0.0
        remaining = self.remaining()
        if not remaining:
            return 0.0  # Exhausted; `has_headroom` is what refuses it, not this.
        even = self.window_seconds / self.daily_limit
        return even * min(used, 1.0)

    # Bookkeeping -----------------------------------------------------------

    def book(self, now=None):
        """Reserve one request before sending it.

        Booked optimistically, before the call, mirroring what `complete_json`
        already does with `pacer.wait` - a request in flight has to count, or
        two stages racing would both see the last slot as free.

        Summary:
            Count one request against the daily allowance.

        Parameters:
            now (float | None): Unused; accepted so callers can pass a clock
                reading consistently alongside the other budget methods.
        """
        with self._lock:
            self.requests_today += 1

    def deny(self, scope, now):
        """Record that the provider itself refused.

        A 429 is ground truth. Whatever the local counters believed, the
        provider has just said no, so the counters are wrong and its answer
        wins.

        Summary:
            Apply a provider's refusal to the daily allowance.

        Parameters:
            scope (str): "day" or "minute". Only a day-scoped denial closes the
                allowance; a per-minute one clears on its own and is handled by
                the pool's cooldown instead.
            now (float): Current monotonic time.
        """
        if scope != "day":
            return
        with self._lock:
            self.denied_until = now + self.window_seconds
            if self.daily_limit:
                self.requests_today = max(self.requests_today, self.daily_limit)

    def snapshot(self):
        """
        Summary:
            A plain dict of the current state, for the Settings card.

        Returns:
            dict: Keys `used`, `limit`, and `remaining`. `limit` is 0 and
                `remaining` is None when there is no daily ceiling.
        """
        return {
            "used": self.requests_today,
            "limit": self.daily_limit,
            "remaining": self.remaining(),
        }


class BudgetLedger:
    """The sqlite half of budgeting, kept away from the worker threads.

    Every method here runs on the thread that owns the connection: `load` at
    the start of a cycle and `flush` at the end, both from `PipelineCycle.run`.
    In between, the pool counts in memory and appends plain dicts. That split
    is what keeps the concurrency contract intact while still letting a daily
    ceiling survive a restart.
    """

    def __init__(self, mail, window_seconds=WINDOW_SECONDS):
        self.mail = mail
        self.window_seconds = window_seconds

    def since(self):
        """
        Summary:
            The lower bound of the rolling window, as an ISO timestamp.

        Returns:
            str: A timestamp in the format the mailstore writes.
        """
        return (
            datetime.now() - timedelta(seconds=self.window_seconds)
        ).isoformat(timespec="seconds")

    def load(self, name, budget, now=None):
        """Seed one provider's budget from what the ledger recorded.

        Summary:
            Read a provider's spend in the window into its in-memory budget.

        Parameters:
            name (str): The provider name as recorded in `provider_usage`.
            budget (Budget): The budget to seed.
            now (float | None): Current monotonic time, passed through.

        Note:
            Failures are swallowed and logged by the caller rather than
            raised. A budget that cannot be read should make the pipeline
            cautious, not stop it - and `Budget` starts at zero, which is the
            permissive direction, so the caller is responsible for deciding
            whether an unreadable ledger is worth degrading on.
        """
        since = self.since()
        budget.seed(
            self.mail.provider_requests_since(name, since),
            denied_day=self.mail.provider_denied_day_since(name, since),
            now=now,
        )

    def flush(self, rows):
        """
        Summary:
            Append a cycle's recorded calls to the usage ledger.

        Parameters:
            rows (list[dict]): Usage rows collected during the cycle.

        Returns:
            int: How many rows were written.
        """
        return self.mail.record_provider_usage(rows)
