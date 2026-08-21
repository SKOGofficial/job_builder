"""Choosing a provider per call, and moving on when one runs out.

The pipeline stages do not know this exists. Each is handed an object with
`complete_json` on it and calls that, exactly as it always did with a
`GroqClient`. What changed is that the object is now a view onto a pool, so the
question "which model serves this?" is answered per call rather than per
process.

The rules, in the order they are applied to each provider in a task's chain:

1. Configured at all, and able to do this shape of work.
2. Not cooling down from a refusal it already gave us.
3. Daily allowance not spent.
4. Its pacing delay - the largest of minimum gap, token window, and daily
   spread - fits inside the caller's patience.

The last one is where pacing and failover turn out to be the same decision.
A provider that would need four hundred seconds to honour its own daily spread
is, from the caller's point of view, simply not available right now, and the
next provider gets the call. Nothing has to special-case it.

When nothing in the chain will take the work, the default provider gets it and
we wait - but only for as long as the caller can afford. That budget is the
whole reason this module knows what thread it is on: `PipelineCycle.dispatch`
and `.prepare` run on the event loop, where a long sleep freezes the UI, while
the router and the classification runner are already inside `asyncio.to_thread`
and can block freely. Past the budget, "wait" means what it has always meant
here - raise, let the handler stop its pass with everything it has written
intact, and resume next cycle. The database is the resume point.
"""

import logging
import threading
import time

from clients.providers.base import (
    ProviderBudgetExhausted,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderRequestTooLarge,
    ProviderUnavailable,
    estimate_tokens,
)
from clients.providers.budget import Budget, BudgetLedger
from clients.providers.routing import (
    SHAPE_JSON,
    SHAPE_RESEARCH,
    TASKS,
    chain_for,
    default_provider,
    resolve,
)

log = logging.getLogger(__name__)

#: How long a call may block when it is running on the event loop thread.
#: Anything longer than a beat there is a visibly frozen UI. Short enough to
#: absorb a pacing gap, not long enough to ride out a rate limit.
#:
#: No pipeline stage takes this any more - `dispatch` and `prepare` are awaited
#: and put their model calls on an executor, so they take `THREAD_MAX_WAIT`
#: like the router. This remains the default for a call made on the loop
#: thread, which is what the thread check in `sleep_budget` falls back to.
LOOP_MAX_WAIT = 2.0

#: How long a call may block off the event loop. Bounded below
#: `scheduler.MIN_INTERVAL_SECONDS` (60) so a wait can never make two cycles
#: overlap, and above the worst honest pacer delay, which `token_delay` caps at
#: sixty seconds minus the age of the oldest booking.
THREAD_MAX_WAIT = 45.0

#: Floor on a cooldown, for a provider that returns 429 with no usable hint.
#: Without it a zero retry-after would let the same provider be retried
#: immediately, which is what the cooldown exists to prevent.
MIN_COOLDOWN = 5.0

#: How long a provider sits out after failing to serve a request at all.
#: Long enough that a decommissioned model or a revoked key is not re-tried
#: once per message for a whole batch, short enough that a transient 5xx costs
#: one cycle rather than a day. Deliberately not the daily window that a spend
#: ceiling earns - this may well be over in a second.
UNAVAILABLE_COOLDOWN = 300.0


def _build_groq(_mail):
    from clients.llm_client import DISPLAY_NAME, GroqClient

    return {SHAPE_JSON: GroqClient.from_config()}, DISPLAY_NAME, 0


def _build_gemini(_mail):
    """Both halves of Gemini, sharing one pacer.

    Summary:
        Build Gemini's classification and research clients as one provider.

    Parameters:
        _mail: Unused; Gemini's ceiling is requests, enforced by `Budget`.

    Returns:
        tuple: `(clients_by_shape, display, daily_limit)`.

    Raises:
        ProviderNotConfigured: When the packages are missing or no key
            resolves.

    Note:
        Two client objects, one provider. Grounded research and JSON-mode
        classification cannot share a request body - the API rejects a search
        tool alongside a JSON response type - so they cannot share a client.
        But they spend one project quota and one rate limit, so they must
        share a budget, a cooldown, and a pacer. Registering them as two
        providers would let a classification 429 leave research merrily
        hammering the same exhausted key.
    """
    from clients.providers import gemini

    clients = {SHAPE_JSON: gemini.GeminiClient.from_config()}
    try:
        from clients.gemini_research import GeminiResearchClient

        research = GeminiResearchClient.from_config()
        research.pacer = clients[SHAPE_JSON].pacer
        clients[SHAPE_RESEARCH] = research
    except Exception as exc:
        # Classification is still perfectly usable without the research half.
        log.info("Gemini research is unavailable: %s", exc)

    return clients, gemini.DISPLAY_NAME, gemini.requests_per_day()


def _build_anthropic(mail):
    from clients.research_client import ResearchClient, SpendLimiter

    limiter = SpendLimiter(mail) if mail is not None else None
    # No daily *request* ceiling: Anthropic's constraint is monetary, and
    # SpendLimiter already enforces it in output tokens against the database.
    return {SHAPE_RESEARCH: ResearchClient.from_config(limiter=limiter)}, "Claude", 0


def _build_claude_cli(_mail):
    """Both shapes over the local `claude` binary.

    Summary:
        Build the Claude Code CLI's classification and research clients.

    Parameters:
        _mail: Unused, deliberately. See the note.

    Returns:
        tuple: `(clients_by_shape, display, daily_limit)`.

    Raises:
        ProviderNotConfigured: When no binary resolves.

    Note:
        No `SpendLimiter`, following `_build_gemini` rather than
        `_build_anthropic`. The limiter reads `job_research` through the
        MailStore's connection, and a client is called from an executor
        thread, so checking it there raises `sqlite3.ProgrammingError` -
        sqlite connections belong to the thread that made them. Anthropic has
        the same wiring and has simply never been configured, so it has never
        fired.

        Nothing is lost by dropping it here: `--max-budget-usd` caps a single
        run inside the CLI, and `CLAUDE_CLI_REQUESTS_PER_DAY` gives the pool's
        own `Budget` a daily ceiling, which is read and written on the thread
        that owns the connection.
    """
    from clients.providers import claude_cli

    # Resolved once, here, so a missing binary raises ProviderNotConfigured
    # before either client is constructed - the same "one check, then build"
    # shape the key-based builders get from `api_key()`.
    binary = claude_cli.binary_path()
    model = claude_cli.model_name()
    clients = {
        SHAPE_JSON: claude_cli.ClaudeCliClient(model=model, binary=binary),
        SHAPE_RESEARCH: claude_cli.ClaudeCliResearchClient(
            model=model, binary=binary,
            timeout=claude_cli.timeout_seconds(claude_cli.RESEARCH_TIMEOUT)),
    }
    return clients, claude_cli.DISPLAY_NAME, claude_cli.requests_per_day()


#: Provider name -> the function that builds it. Construction knowledge lives
#: here rather than in the provider modules so those stay unaware of the pool,
#: and so the modules keep working standalone (`cli.py`, the Settings test
#: button) exactly as before.
#:
#: Each builder returns `(clients_by_shape, display, daily_limit)`.
BUILDERS = {
    "groq": _build_groq,
    "gemini": _build_gemini,
    "anthropic": _build_anthropic,
    "claude_cli": _build_claude_cli,
}

#: What each provider *could* do, independent of whether it is configured.
#: Needed because an unconfigured provider has no clients to infer from, and
#: Settings must still offer it - with its state in the label - rather than
#: omit it, or the missing key becomes undiscoverable.
PROVIDER_SHAPES = {
    "groq": frozenset({SHAPE_JSON}),
    "gemini": frozenset({SHAPE_JSON, SHAPE_RESEARCH}),
    "anthropic": frozenset({SHAPE_RESEARCH}),
    # Both, so any task can be pointed at it from Settings. Nothing routes
    # here by default: an agent loop takes tens of seconds where Groq takes
    # under one, so which work is worth that is the operator's call.
    "claude_cli": frozenset({SHAPE_JSON, SHAPE_RESEARCH}),
}

#: What to call each provider in the UI. Declared here as well as returned by
#: the builders, because a provider with no key never reaches its builder's
#: return statement - and "Anthropic" appearing where a configured one says
#: "Claude" reads as two different things rather than one unconfigured one.
PROVIDER_DISPLAY = {
    "groq": "Groq",
    "gemini": "Gemini",
    "anthropic": "Claude",
    "claude_cli": "Claude Code",
}


class ProviderState:
    """One provider's clients, and everything known about its current capacity.

    A provider may hold more than one client - Gemini needs a different request
    shape for grounded research than for JSON-mode classification - but it has
    exactly one budget, one cooldown and one pacer, because those describe the
    account rather than the endpoint. A classification 429 must stop research
    on the same key too.
    """

    def __init__(self, name, clients=None, display="", daily_limit=0,
                 declared_shapes=None, clock=time.monotonic):
        self.name = name
        self.display = display or PROVIDER_DISPLAY.get(name) or name.title()
        self.clients = dict(clients or {})
        #: What this provider could serve if configured. Falls back to what it
        #: actually built, so a test double needs only pass clients.
        self.declared_shapes = frozenset(
            declared_shapes
            if declared_shapes is not None
            else PROVIDER_SHAPES.get(name, frozenset(self.clients))
        )
        self.budget = Budget(daily_limit=daily_limit, clock=clock)
        self.cooldown_until = 0.0
        self.last_error = ""
        self._clock = clock

    @property
    def client(self):
        """
        Summary:
            A representative client, for reading the model name and status.

        Returns:
            The classification client when there is one, else any client, else
                None.
        """
        if not self.clients:
            return None
        return self.clients.get(SHAPE_JSON) or next(iter(self.clients.values()))

    @property
    def pacer(self):
        """
        Summary:
            The provider's pacer, or None when it is unconfigured.

        Returns:
            Pacer | None: One rolling token window for the whole provider.
                Shared across its clients rather than duplicated, so two
                endpoints on one key cannot each believe they have the full
                per-minute allowance.
        """
        return getattr(self.client, "pacer", None)

    def configured(self):
        """
        Summary:
            Whether this provider has any usable client.

        Returns:
            bool: True when at least one client was built successfully.
        """
        return bool(self.clients)

    def supports(self, shape):
        """
        Summary:
            Whether this provider can do a given kind of work.

        Parameters:
            shape (str): `SHAPE_JSON` or `SHAPE_RESEARCH`.

        Returns:
            bool: True when a client exists for that shape.
        """
        return shape in self.clients

    def client_for(self, shape):
        """
        Summary:
            The client that serves a given kind of work.

        Parameters:
            shape (str): `SHAPE_JSON` or `SHAPE_RESEARCH`.

        Returns:
            The matching client, or None when this provider cannot do it.
        """
        return self.clients.get(shape)

    def cooling_down(self, now):
        """
        Summary:
            Whether a recent refusal is still in force.

        Parameters:
            now (float): Current monotonic time.

        Returns:
            bool: True while the cooldown has not elapsed.
        """
        return now < self.cooldown_until

    def cool_down(self, until, reason=""):
        """
        Summary:
            Stop offering this provider until a moment has passed.

        Parameters:
            until (float): Monotonic time the cooldown expires.
            reason (str): Human-readable cause, shown in Settings.

        Note:
            Extends an existing cooldown but never shortens it. A second
            refusal arriving with a smaller hint must not make a provider look
            available sooner than the first one said.
        """
        self.cooldown_until = max(self.cooldown_until, until)
        if reason:
            self.last_error = reason

    def delay(self, now, projected_tokens):
        """The longest of every pacing rule that applies to this provider.

        Summary:
            Report how long this provider would make a caller wait.

        Parameters:
            now (float): Current monotonic time.
            projected_tokens (int): Expected cost of the pending request.

        Returns:
            float: Seconds. 0.0 when the request could go out immediately.
        """
        pacer = self.pacer
        delays = [self.budget.spread_delay(now)]
        if pacer is not None:
            delays.append(pacer.interval_delay(now))
            delays.append(pacer.token_delay(now, projected_tokens))
        return max(delays)

    def snapshot(self, now):
        """
        Summary:
            A plain dict describing this provider, for the Settings card.

        Parameters:
            now (float): Current monotonic time.

        Returns:
            dict: Keys `name`, `display`, `configured`, `model`, `shapes`,
                `cooling`, `cooldown_seconds`, `last_error`, plus the budget's
                `used`, `limit` and `remaining`.
        """
        state = {
            "name": self.name,
            "display": self.display,
            "configured": self.configured(),
            "model": getattr(self.client, "model", None),
            # What this provider can be routed to. The Settings dropdowns
            # filter on it, so Groq is never offered for research. Declared
            # rather than built, so an unconfigured provider is still offered
            # with a "no key" label instead of quietly disappearing.
            "shapes": self.declared_shapes,
            "cooling": self.cooling_down(now),
            "cooldown_seconds": max(0, int(self.cooldown_until - now)),
            "last_error": self.last_error,
        }
        state.update(self.budget.snapshot())
        return state


class ProviderPool:
    """Every configured provider, and the routing that picks between them.

    Built once per process and hung off `AppState`, not rebuilt per cycle. A
    cooldown has to outlive the cycle that earned it: a Gemini daily denial at
    14:00 is still in force at 14:10, and a pool rebuilt each pass would
    cheerfully retry it every ten minutes until midnight.
    """

    def __init__(self, mail=None, names=None, builders=None, clock=time.monotonic,
                 sleep=time.sleep):
        self.mail = mail
        self.ledger = BudgetLedger(mail) if mail is not None else None
        self._clock = clock
        self._sleep = sleep
        self._builders = builders if builders is not None else BUILDERS
        self.providers = {}
        self.routes = {}
        self.pending_usage = []
        #: Task id -> (provider name, model) for the most recent call. Read by
        #: the two stages that write a classification row.
        self.attribution = {}
        self._build(names if names is not None else list(self._builders))
        self.reload_routes()

    # Construction ---------------------------------------------------------

    def _build(self, names):
        """Build every provider, recording rather than raising on failure.

        Summary:
            Populate `providers` with a state per configured provider.

        Parameters:
            names (list[str]): Provider names to attempt.

        Note:
            An unconfigured provider still gets a `ProviderState`, with no
            client. Settings needs to say "Gemini: no key" rather than not
            mention Gemini, or the fix is undiscoverable.
        """
        for name in names:
            builder = self._builders.get(name)
            if builder is None:
                continue
            try:
                clients, display, daily_limit = builder(self.mail)
            except ProviderNotConfigured as exc:
                log.info("Provider %s is not configured: %s", name, exc)
                self.providers[name] = ProviderState(name, clock=self._clock)
                self.providers[name].last_error = str(exc)
                continue
            except Exception as exc:
                log.warning("Provider %s could not be built: %s", name, exc)
                self.providers[name] = ProviderState(name, clock=self._clock)
                self.providers[name].last_error = str(exc)
                continue
            self.providers[name] = ProviderState(
                name, clients=clients, display=display,
                daily_limit=daily_limit, clock=self._clock,
            )

    def reload_routes(self):
        """Re-read saved routing so a Settings change lands without a restart.

        Summary:
            Refresh the cached per-task routing from the database.
        """
        if self.mail is None:
            self.routes = {}
            return
        try:
            self.routes = self.mail.provider_routes()
        except Exception:
            log.exception("Could not read saved provider routing; using defaults")
            self.routes = {}

    def configured_names(self):
        """
        Summary:
            Names of providers that have a usable client.

        Returns:
            list[str]: Configured provider names, in build order.
        """
        return [name for name, state in self.providers.items() if state.configured()]

    # Cycle boundaries ------------------------------------------------------

    def begin_cycle(self):
        """Seed every budget from the usage ledger.

        Called on the thread that owns the connection, before any stage runs.
        This is what makes a daily ceiling survive a restart: the in-memory
        counters are rebuilt from rows, and a recorded per-day denial is
        re-applied.

        Summary:
            Load persisted spend into the in-memory budgets.

        Note:
            A ledger that cannot be read is logged and ignored. Budgets then
            start at zero, which is permissive - but refusing to run the
            pipeline because a usage table was unreadable would trade a
            possible overspend for a certain outage.
        """
        if self.ledger is None:
            return
        now = self._clock()
        for name, state in self.providers.items():
            try:
                self.ledger.load(name, state.budget, now=now)
            except Exception:
                log.exception("Could not load the usage ledger for %s", name)

    def flush(self):
        """Write the cycle's recorded calls to the usage ledger.

        Summary:
            Persist and clear the pending usage rows.

        Returns:
            int: How many rows were written.

        Note:
            Never raises. A failed ledger write must not fail a cycle that
            actually did the work - the rows are bookkeeping, and losing them
            costs accuracy in the next budget seed, not correctness.
        """
        if self.ledger is None or not self.pending_usage:
            self.pending_usage = []
            return 0
        rows, self.pending_usage = self.pending_usage, []
        try:
            return self.ledger.flush(rows)
        except Exception:
            log.exception("Could not record provider usage for %d call(s)", len(rows))
            return 0

    def record(self, provider, task, outcome, model=None, tokens=0):
        """
        Summary:
            Queue one call's cost for the next flush.

        Parameters:
            provider (str): Which provider served or refused the call.
            task (str): The canonical task id.
            outcome (str): 'ok', 'rate_limited', 'denied_day', or 'error'.
            model (str | None): The model used, when known.
            tokens (int): Total tokens the call cost.
        """
        self.pending_usage.append({
            "provider": provider, "task": task, "outcome": outcome,
            "model": model, "total_tokens": tokens or 0,
        })

    # Status ----------------------------------------------------------------

    def status(self):
        """
        Summary:
            A snapshot of every provider, for the Settings card.

        Returns:
            list[dict]: One `ProviderState.snapshot` per provider.
        """
        now = self._clock()
        return [state.snapshot(now) for state in self.providers.values()]

    def next_available_in(self, now=None):
        """How long until any provider could take a model call again.

        Summary:
            Report the shortest wait before some configured provider is out of
            cooldown and inside its daily budget.

        Parameters:
            now (float | None): Monotonic time to evaluate against. Defaults to
                the pool's clock.

        Returns:
            float: Seconds to wait. 0.0 when a provider can take a call now,
                and also when nothing is configured at all - "cannot ever" is
                not a wait, and the caller already handles an absent provider.

        Note:
            Deliberately ignores the task chain and the shape. A caller asking
            this is deciding whether to attempt the model stages at all, and
            answering per-task would mean claiming a provider is free for work
            its chain does not route to it. The pessimistic reading is the
            useful one here.
        """
        now = self._clock() if now is None else now
        waits = []
        for state in self.providers.values():
            if not state.configured():
                continue
            if state.cooling_down(now):
                waits.append(state.cooldown_until - now)
                continue
            if not state.budget.has_headroom(now):
                waits.append(state.budget.reset_in(now))
                continue
            return 0.0
        return min(waits) if waits else 0.0

    def next_available_for(self, task, now=None):
        """How long until this particular task's chain could take a call.

        The per-task counterpart to `next_available_in`. That one answers "is
        any provider alive at all", which is the right question for the stage
        group but the wrong one for a task whose chain is narrower than the
        pool: research routes to Gemini and Anthropic only, and Groq being
        healthy says nothing about whether research can run.

        Summary:
            Report the shortest wait before some provider in a task's chain
            could serve it.

        Parameters:
            task (str): A task id or alias.
            now (float | None): Monotonic time to evaluate against. Defaults to
                the pool's clock.

        Returns:
            float: Seconds to wait. 0.0 when a provider can take the call now,
                and also when every blocker is permanent - "cannot ever" is not
                a wait, and the caller already handles an absent client.

        Raises:
            KeyError: If the task id is unknown.

        Note:
            Evaluated against `THREAD_MAX_WAIT`, because every caller is a
            stage whose calls go to the executor and so may wait the longer
            budget. Asking with the short budget would report a pacing gap as
            unavailability.
        """
        canonical = resolve(task)
        now = self._clock() if now is None else now
        ready, blocked = self.candidates(
            canonical,
            TASKS[canonical].shape,
            TASKS[canonical].max_tokens,
            THREAD_MAX_WAIT,
            now,
        )
        if ready:
            return 0.0
        waits = [seconds for _name, _why, seconds in blocked if seconds > 0]
        return min(waits) if waits else 0.0

    def signature(self):
        """A cheap value that changes when the displayed state would.

        Summary:
            Summarise pool state for the Settings page's change detector.

        Returns:
            tuple: Comparable, touches no database, safe to compute several
                times a second.
        """
        now = self._clock()
        return tuple(
            (name, state.configured(), state.cooling_down(now),
             state.budget.requests_today)
            for name, state in sorted(self.providers.items())
        )

    # Task binding ----------------------------------------------------------

    def chain(self, task):
        """
        Summary:
            The provider names that serve a task, in order.

        Parameters:
            task (str): A task id or alias.

        Returns:
            tuple[str, ...]: Provider names.
        """
        return chain_for(task, self.routes)

    def for_task(self, task, max_wait=None):
        """Bind a client to one task.

        Summary:
            Return a client that serves one task through the pool.

        Parameters:
            task (str): A task id or alias.
            max_wait (float | None): Longest this client may block. None picks
                a budget from the calling thread - see the module docstring.

        Returns:
            TaskClient | ResearchTaskClient | None: A client of the shape the
                task declares, or None when no provider in its chain is
                configured.

        Raises:
            KeyError: If the task id is unknown.

        Note:
            Returns None rather than raising for an unconfigured chain, so the
            existing `if client is not None:` degrade paths keep working and a
            missing key does not surface as a logged traceback.
        """
        canonical = resolve(task)
        chain = self.chain(canonical)
        if not any(
            self.providers.get(name) is not None
            and self.providers[name].configured()
            and self.providers[name].supports(TASKS[canonical].shape)
            for name in chain
        ):
            return None
        if TASKS[canonical].shape == SHAPE_RESEARCH:
            return ResearchTaskClient(self, canonical, max_wait=max_wait)
        return TaskClient(self, canonical, max_wait=max_wait)

    # The failover loop -----------------------------------------------------

    def sleep_budget(self, max_wait=None):
        """How long a call may block, given where it is running.

        Summary:
            Choose the sleep budget for the current call.

        Parameters:
            max_wait (float | None): An explicit budget, which always wins.

        Returns:
            float: Seconds a call may spend waiting.

        Note:
            The thread check is a safety net, not the primary mechanism. The
            orchestrator passes `max_wait` explicitly at the wiring points that
            matter, because "am I on the event loop" is a question that should
            not be guessed where the answer is already known.
        """
        if max_wait is not None:
            return max_wait
        on_loop = threading.current_thread() is threading.main_thread()
        return LOOP_MAX_WAIT if on_loop else THREAD_MAX_WAIT

    def candidates(self, task, shape, projected_tokens, budget_s, now):
        """Split a task's chain into who can take the call and who cannot.

        Summary:
            Evaluate every provider in a task's chain against the four rules.

        Parameters:
            task (str): The canonical task id.
            shape (str): The client shape the task needs.
            projected_tokens (int): Expected cost of the pending request.
            budget_s (float): Longest the caller may wait.
            now (float): Current monotonic time.

        Returns:
            tuple[list, list]: Ready `ProviderState`s in chain order, and
                `(name, reason, seconds)` for each one that was skipped. The
                second half exists so the eventual failure can say when
                something will free up rather than only that nothing did.
        """
        ready, blocked = [], []
        for name in self.chain(task):
            state = self.providers.get(name)
            if state is None or not state.configured():
                blocked.append((name, "not configured", 0.0))
                continue
            if not state.supports(shape):
                blocked.append((name, "cannot do this task", 0.0))
                continue
            if state.cooling_down(now):
                blocked.append((name, "cooling down", state.cooldown_until - now))
                continue
            if not state.budget.has_headroom(now):
                blocked.append((name, "daily limit reached",
                                state.budget.reset_in(now)))
                continue
            delay = state.delay(now, projected_tokens)
            if delay > budget_s:
                blocked.append((name, "pacing", delay))
                continue
            ready.append(state)
        return ready, blocked

    def call(self, task, shape, send, projected_tokens, max_wait=None):
        """Run one model call against the first provider that will take it.

        Summary:
            Execute a call with budget-aware failover.

        Parameters:
            task (str): The canonical task id.
            shape (str): The client shape required.
            send (Callable[[Any], Any]): Given a provider's client, performs
                the call. Supplied by the task client so this loop stays
                indifferent to whether it is `complete_json` or `research`.
            projected_tokens (int): Expected cost, for pacing and reconciling.
            max_wait (float | None): Explicit sleep budget.

        Returns:
            Any: Whatever `send` returned.

        Raises:
            ProviderRateLimited: When nothing in the chain, nor the default
                provider, could take the call. `retry_after` is the soonest any
                of them frees up, so the handler's log line stays useful.
            ProviderBudgetExhausted: When every provider that could serve a
                research task has spent its ceiling.

        Note:
            `ProviderRequestTooLarge` is a failover trigger rather than an
            error, and deliberately does not cool the provider down. It says
            "this payload is too big for me", not "I am unavailable", and the
            very next message may be a tenth the size.
        """
        budget_s = self.sleep_budget(max_wait)
        now = self._clock()
        ready, blocked = self.candidates(task, shape, projected_tokens, budget_s, now)

        exhausted = None
        for state in ready:
            try:
                return self._send(state, task, shape, send, projected_tokens,
                                  budget_s)
            except ProviderRateLimited as exc:
                self._on_rate_limit(state, task, shape, exc)
                blocked.append((state.name, "rate limited", float(exc.retry_after)))
                continue
            except ProviderBudgetExhausted as exc:
                # Anthropic's monetary ceiling. Not a rate limit: it will not
                # clear on its own, so the provider is out for the window.
                state.cool_down(self._clock() + state.budget.window_seconds, str(exc))
                self.record(state.name, task, "denied_day",
                            model=self._model_of(state, shape))
                blocked.append((state.name, "spend ceiling reached", 0.0))
                exhausted = exc
                continue
            except ProviderRequestTooLarge as exc:
                # This payload, not this provider. No cooldown and no client
                # removed: the next message may be a tenth the size and this
                # provider is the right one for it. Just move along the chain,
                # which is the one thing that can actually serve the request -
                # the fallback usually has a far larger allowance.
                state.last_error = str(exc)
                self.record(state.name, task, "error",
                            model=self._model_of(state, shape))
                blocked.append((state.name, "request too large", 0.0))
                continue
            except ProviderNotConfigured as exc:
                state.clients.pop(shape, None)
                state.last_error = str(exc)
                blocked.append((state.name, "not configured", 0.0))
                continue
            except ProviderUnavailable as exc:
                # The request was not served: a decommissioned model, a revoked
                # key, a 5xx. Before this it arrived as a bare exception and
                # stopped the whole call with the next provider in the chain
                # sitting idle behind it - a dead model name in `.env` took the
                # pipeline down while a working provider went unasked.
                #
                # Cooled down as well as skipped, because the common causes are
                # persistent and re-discovering them once per message costs a
                # round trip each time.
                state.cool_down(self._clock() + UNAVAILABLE_COOLDOWN, str(exc))
                state.last_error = str(exc)
                self.record(state.name, task, "error",
                            model=self._model_of(state, shape))
                # Logged, not just recorded. `provider_usage` stores the
                # outcome but not the reason, so without this line a provider
                # failing on every cycle shows up as a row saying "error" and
                # nothing anywhere says what it returned - which is exactly the
                # position this left the pipeline in for an hour.
                log.warning(
                    "%s could not serve %s and is cooling down for %ds: %s",
                    state.name, task, int(UNAVAILABLE_COOLDOWN), exc,
                )
                blocked.append((state.name, f"unavailable ({exc.status or 'no response'})",
                                UNAVAILABLE_COOLDOWN))
                continue

        return self._default_and_wait(
            task, shape, send, projected_tokens, budget_s, blocked, exhausted
        )

    @staticmethod
    def _model_of(state, shape):
        """
        Summary:
            The model name for one provider's client of a given shape.

        Parameters:
            state (ProviderState): The provider.
            shape (str): The client shape.

        Returns:
            str | None: The model name, or None when unknown.
        """
        return getattr(state.client_for(shape), "model", None)

    def _send(self, state, task, shape, send, projected_tokens, budget_s):
        """Wait out this provider's pacing, then send.

        Summary:
            Perform one call against one provider, booking and reconciling.

        Parameters:
            state (ProviderState): The provider to use.
            task (str): The canonical task id.
            shape (str): Which of the provider's clients to use.
            send (Callable): Performs the call, given the client.
            projected_tokens (int): Expected cost.
            budget_s (float): Longest this call may wait.

        Returns:
            Any: Whatever `send` returned.

        Note:
            The request is booked against the daily allowance *before* it is
            sent, mirroring how `complete_json` books tokens before the call.
            A request in flight has to count, or two stages could each see the
            last slot as free.
        """
        client = state.client_for(shape)
        now = self._clock()
        delay = state.delay(now, projected_tokens)
        if delay > 0:
            self._sleep(min(delay, budget_s))
        state.budget.book()
        value = send(client)
        tokens = getattr(client, "last_total_tokens", 0)
        model = getattr(client, "model", None)
        self.record(state.name, task, "ok", model=model, tokens=tokens)
        self.attribution[task] = (state.name, model)
        state.last_error = ""
        return value

    def _on_rate_limit(self, state, task, shape, exc):
        """
        Summary:
            Apply a provider's refusal to its cooldown and budget.

        Parameters:
            state (ProviderState): The provider that refused.
            task (str): The canonical task id.
            shape (str): Which client refused, for the recorded model name.
            exc (ProviderRateLimited): The refusal.

        Note:
            A 429 is ground truth. Whatever the local counters believed, the
            provider has said no, so its answer overrides the arithmetic - and
            a day-scoped refusal is written to the ledger so a restart cannot
            un-exhaust it. The cooldown lands on the provider, not the client,
            so a classification limit also stops research on the same key.
        """
        now = self._clock()
        state.cool_down(now + max(exc.retry_after, MIN_COOLDOWN), str(exc))
        state.budget.deny(exc.scope, now)
        outcome = "denied_day" if exc.scope == "day" else "rate_limited"
        self.record(state.name, task, outcome, model=self._model_of(state, shape))
        log.info("%s refused %s (%s); trying the next provider",
                 state.display, task, exc.scope)

    def _default_and_wait(self, task, shape, send, projected_tokens, budget_s,
                          blocked, exhausted=None):
        """Last resort: the default provider, waited for if the wait fits.

        Summary:
            Fall back to the configured default provider, or give up cleanly.

        Parameters:
            task (str): The canonical task id.
            shape (str): The client shape required.
            send (Callable): Performs the call, given the client.
            projected_tokens (int): Expected cost.
            budget_s (float): Longest this call may wait.
            blocked (list): `(name, reason, seconds)` for everything skipped.
            exhausted (Exception | None): A spend-ceiling error seen earlier,
                re-raised in preference to a rate limit because the two mean
                different things to `pipeline/prepare.py`.

        Returns:
            Any: Whatever `send` returned, if the default provider took it.

        Raises:
            ProviderBudgetExhausted: When a spend ceiling was what stopped us.
            ProviderRateLimited: Otherwise. `retry_after` is the soonest any
                candidate frees up.
        """
        state = self.providers.get(default_provider())
        if (
            state is not None
            and state.configured()
            and state.supports(shape)
            and not state.cooling_down(self._clock())
            and state.budget.has_headroom(self._clock())
            and state.delay(self._clock(), projected_tokens) <= budget_s
        ):
            try:
                return self._send(state, task, shape, send, projected_tokens,
                                  budget_s)
            except ProviderRateLimited as exc:
                self._on_rate_limit(state, task, shape, exc)
                blocked.append((state.name, "rate limited", float(exc.retry_after)))

        if exhausted is not None:
            raise exhausted

        waits = [seconds for _n, _why, seconds in blocked if seconds > 0]
        soonest = int(min(waits)) if waits else 60
        detail = ", ".join(f"{name} ({why})" for name, why, _s in blocked) or "none"
        raise ProviderRateLimited(
            f"No model is available for {TASKS[task].label.lower()}: {detail}.",
            retry_after=soonest,
            provider=(state.display if state is not None else ""),
        )


class TaskClient:
    """A model bound to one task, shaped exactly like `GroqClient`.

    Every pipeline stage holds one of these without knowing it. The surface is
    deliberately identical - same method, same argument order, same `model`
    attribute - because that identity is what let this land without touching
    six of the seven modules that call a model.
    """

    def __init__(self, pool, task, max_wait=None):
        self.pool = pool
        self.task = task
        self.max_wait = max_wait

    @property
    def model(self):
        """
        Summary:
            The model that served the most recent call for this task.

        Returns:
            str | None: The model name, or None before the first call.
        """
        seen = self.pool.attribution.get(self.task)
        return seen[1] if seen else None

    @property
    def last_model(self):
        """
        Summary:
            The model that served the most recent call for this task.

        Returns:
            str | None: The model name, or None before the first call.

        Note:
            Valid only until the next call on this task, which is enough
            because every stage is sequential and records the model in the same
            loop iteration as the call that produced it.
        """
        return self.model

    @property
    def provider(self):
        """
        Summary:
            The provider that served the most recent call for this task.

        Returns:
            str | None: The provider name, or None before the first call.
        """
        seen = self.pool.attribution.get(self.task)
        return seen[0] if seen else None

    def available_in(self):
        """
        Summary:
            How long until some provider could serve this task.

        Returns:
            float: Seconds to wait, 0.0 when a call could go out now. See
                `ProviderPool.next_available_for`.

        Note:
            Exposed on the client so a stage can ask before it starts a batch,
            without being handed the pool it was deliberately kept away from.
        """
        return self.pool.next_available_for(self.task)

    def complete_json(self, messages, parser, fallback, max_tokens=200):
        """
        Summary:
            Send one JSON completion through whichever provider can take it.

        Parameters:
            messages (list[dict]): The chat messages to send.
            parser (Callable[[str], Any]): Validates the model's reply text.
            fallback (Any): Returned when the model produces nothing usable.
            max_tokens (int): Output ceiling for the request.

        Returns:
            Any: Whatever `parser` returns, or `fallback`.

        Raises:
            ProviderRateLimited: When no provider could take the call.
        """
        return self.pool.call(
            self.task,
            SHAPE_JSON,
            lambda client: client.complete_json(
                messages, parser, fallback, max_tokens
            ),
            estimate_tokens(messages, max_tokens),
            max_wait=self.max_wait,
        )

    def classify(self, match):
        """
        Summary:
            Classify one matched reply, for the legacy per-job classifier.

        Parameters:
            match (dict): The match payload.

        Returns:
            dict: Keys `label`, `confidence`, `reason`.

        Raises:
            ProviderRateLimited: When no provider could take the call.
        """
        from clients.llm_client import build_messages, parse_classification, unclear

        return self.complete_json(
            build_messages(match),
            parse_classification,
            unclear("Model returned no choices."),
        )


class ResearchTaskClient:
    """The research-shaped counterpart, matching what `ArtifactBuilder` expects.

    `pipeline/generate.py` calls `research(lead)` and reads `.model` off the
    client to store alongside the result, so this exposes exactly that and
    nothing more.
    """

    def __init__(self, pool, task, max_wait=None):
        self.pool = pool
        self.task = task
        self.max_wait = max_wait

    @property
    def model(self):
        """
        Summary:
            The model that produced the most recent research.

        Returns:
            str | None: The model name, or None before the first call.
        """
        seen = self.pool.attribution.get(self.task)
        return seen[1] if seen else None

    def available_in(self):
        """
        Summary:
            How long until some provider could serve this task.

        Returns:
            float: Seconds to wait, 0.0 when a call could go out now. See
                `ProviderPool.next_available_for`.

        Note:
            The reason this exists. Research is the one task whose chain can be
            entirely blocked while the pool as a whole looks healthy, so the
            preparer asks this before spending a batch discovering it.
        """
        return self.pool.next_available_for(self.task)

    def research(self, lead):
        """
        Summary:
            Research one lead through whichever provider can take it.

        Parameters:
            lead (dict | sqlite3.Row): The lead to research.

        Returns:
            tuple: `(payload, input_tokens, output_tokens)`.

        Raises:
            ProviderBudgetExhausted: When every provider has spent its ceiling.
            ProviderRateLimited: When no provider could take the call.

        Note:
            Projected at the task's declared `max_tokens` rather than measured.
            Research prompts are small and the reply is the expensive half, so
            there is no request to measure the way `estimate_tokens` measures a
            chat payload.
        """
        return self.pool.call(
            self.task,
            SHAPE_RESEARCH,
            lambda client: client.research(lead),
            TASKS[self.task].max_tokens,
            max_wait=self.max_wait,
        )

    def find_openings(self, contact):
        """
        Summary:
            Check one company for current openings through whichever provider
            can take it.

        Parameters:
            contact (dict | sqlite3.Row): The contact whose employer to check.

        Returns:
            tuple: `(openings, input_tokens, output_tokens)`.

        Raises:
            ProviderBudgetExhausted: When every provider has spent its ceiling.
            ProviderRateLimited: When no provider could take the call.

        Note:
            Projected at the task's declared `max_tokens` for the same reason
            `research` is: there is no chat payload to measure, and the reply is
            the expensive half.
        """
        return self.pool.call(
            self.task,
            SHAPE_RESEARCH,
            lambda client: client.find_openings(contact),
            TASKS[self.task].max_tokens,
            max_wait=self.max_wait,
        )
