# Project quality requirements

## Formatting and linting

- Run `uv run black --check .` and `uv run ruff check .` from the repository
  root before completing changes.
- Format Python with Black using a maximum line length of 80 characters. Run
  `uv run black .` after changing Python files.
- Enable Ruff's `ALL` rule set, including preview rules. Keep every compatible
  diagnostic enabled and fail on warnings.
- Enable Black's preview and unstable formatting features.
- Ignore a rule only when it is mutually exclusive with another enabled rule or
  conflicts with the formatter. Document the exact reason next to each ignore.
- Do not add per-file ignores, `noqa` comments, or configuration exclusions
  merely to make linting pass. Fix diagnostics introduced by changes and report
  existing failures honestly.

## Readability and domain design

- Give each function one coherent responsibility. Separate external-data
  validation, strategy decisions, account mutation, and output conversion.
- Enforce at most 5 parameters, 3 positional parameters, cyclomatic complexity
  10, 12 branches, 15 local variables, 50 statements, 6 returns, 5 expressions
  per boolean condition, and 3 nested blocks through Ruff.
- Use keyword-only parameters for configuration and boolean options. Group
  related settings into cohesive, named domain types; do not hide unrelated
  inputs in a generic context object, dictionary, or variadic arguments.
- Prefer frozen, keyword-only dataclasses for configuration. Model mutually
  exclusive strategy configurations as distinct types so irrelevant options
  and invalid combinations are not part of their interfaces.
- Use Literal or Enum types for closed sets of modes, sides, and reasons.
  Validate external strings before converting them into domain values.
- Represent related position state with one typed object. Avoid parallel
  optional fields and sentinel values that permit inconsistent states.
- Make state ownership and mutation explicit. Do not use nonlocal mutation
  in trading logic or both mutate state and return duplicate state values.
- Separate independently testable decision calculations from side effects.
  Express a decision as a typed value before applying account changes.
- Use named result types when returned values have distinct domain meanings.
  Keep dataframe and serialization conversions at boundaries; core trading
  logic must consume validated domain types with explicit fields.
- Preserve chronological execution and explicit precedence for competing
  trading actions. Do not replace ordered execution with unordered rules.
- Prefer guard clauses for invalid inputs and early exits. Replace repeated
  branching on the same mode with distinct strategy implementations. Use lookup
  tables for simple value mappings. Preserve explicit conditionals when priority
  or execution order matters. Do not hide branching in nested ternaries, boolean
  expressions, or trivial helpers merely to satisfy complexity limits.
- Name helpers after domain operations. Do not introduce trivial forwarding
  helpers, generic frameworks, or arbitrary function splits to satisfy lint.
- Keep every compatible Ruff rule enabled. Existing violations are work to
  report and resolve, not grounds for legacy exemptions or relaxed limits.
- For explicitly rules-only changes, report newly exposed violations without
  modifying application code or tests to resolve them.

## Type checking

- Run `uv run basedpyright` from the repository root before completing changes.
- Use BasedPyright's `all` preset, the strongest stable preset, for production
  code and tests. Keep every enabled diagnostic and fail on warnings.
- Type function parameters and returns, class attributes, and generic arguments.
  Explicit and implicit `Any`, unknown types, and unsafe optional access are errors.
- Validate external JSON, CSV, database, and exchange data at runtime before
  treating it as typed application data. An annotation or cast is not validation.
- Do not weaken rules, exclude project files, create a diagnostic baseline, or add
  suppression comments or unjustified casts merely to make checks pass.
- `type: ignore` comments are disabled. Any necessary `pyright: ignore` exception
  must identify the diagnostic rule and explain the specific third-party typing
  limitation; unused suppressions are errors.
- Fix diagnostics introduced by changes. Report existing failures honestly;
  a configured checker does not mean the project currently passes it.

## Tests and coverage

- Run `uv run pytest` before completing changes. Use pytest and pytest-cov.
- Preserve or increase the current 92% combined line/branch coverage threshold.
  Current measured scope is `gui_calculator`, `shared.gex`,
  `shared.volume_profile`, and `shared.open_interest`, not all production code.
- Never lower the coverage threshold, narrow measured scope, or add coverage
  exclusions merely to pass checks.
- Test meaningful behavior and failure paths for changed logic, particularly
  trading calculations, persistence, and external-data validation.
- Report command results and unresolved failures; do not claim passing checks
  when tests or type checking fail.

## Browser frontend checks

- Use Python Playwright with real Chromium for GUI Calculator frontend tests.
  Install the locked development dependencies with `uv sync --locked` and the
  browser/system dependencies with
  `uv run playwright install --with-deps chromium`.
- Run `uv run pytest` before completing changes. The full suite includes the
  browser checks and enforces at least 92% combined Python line/branch coverage
  over the production scope above. A missing browser is a setup failure, not a
  reason to skip tests. Use `uv run pytest tests/frontend --no-cov` only for a
  focused browser run; it does not replace full-suite coverage verification.
- Keep a separate minimum 92% V8 executed-character coverage gate for the entire
  production `gui_calculator/oscillator_dashboard/chart.js` renderer. Exclude
  unexecuted nested V8 ranges when calculating executed coverage, then combine
  real browser scenarios. Do not describe this metric as Python or JavaScript
  branch coverage. Preserve and report its explicit source scope.
- Do not lower either coverage threshold, narrow the measured or audited scope,
  add exclusions, disable checks, or suppress failures merely to make tests pass.
- Exercise the production Streamlit UI and embedded HTML/CSS/JavaScript, using
  deterministic exchange fixtures at data boundaries and temporary persistence.
  Canvas mocks and Streamlit AppTest do not replace real-browser verification.
- Verify actual light and dark application backgrounds and readable foregrounds.
  Test phone and desktop chart layouts, including horizontal overflow, empty
  and single observations, data gaps, and both margin and limit-order sources.
- Cover real GEX controls, latest versus selected-snapshot cutoffs, causal hover
  values, zoom, pan, reset, and keyboard focus. Require Reset view to restore the
  original canvas pixels and clear the tooltip; control labels alone are not
  evidence that the underlying chart state is correct.
- Fail on every available axe WCAG A, AA, and AAA rule violation, including text
  foreground/background contrast. Do not filter failures by impact or omit
  inconvenient UI elements from the audit. Account for effective backgrounds
  and opacity, not just CSS color literals.
- Independently check text drawn inside Canvas, which axe cannot read. Use WCAG
  relative luminance against rendered background pixels and require at least
  the AAA 7:1 text contrast ratio. Include a regression proving low-contrast
  text is rejected. Automated checks do not establish complete WCAG conformance.
- Fail browser tests on uncaught JavaScript exceptions, console errors, failed
  requests, HTTP error responses, or unexpected external browser requests.
  Use locator assertions and browser readiness checks instead of fixed sleeps
  or forced interactions that conceal timing or visibility bugs.
- Keep Playwright drivers and browser event loops test-scoped so frontend tests
  do not break other tests using `asyncio.run`. Preserve test isolation and
  normal sidebar collapse/expand behavior when fixing accessibility.
- Retain screenshots and Playwright traces on browser failures. Write the chart
  coverage report to `test-results/frontend-coverage.json` and report actual
  coverage, failures, and limitations honestly. See `tests/frontend/README.md`
  for commands, artifacts, and the current tested scope.

## Setup

Install the locked development environment with `uv sync --locked`. Type-checker
and third-party stub dependencies are maintained in `pyproject.toml` and `uv.lock`.
