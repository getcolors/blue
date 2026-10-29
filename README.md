# blue

`blue` is the Python/uv workflow SDK of the Colors family. `green`
(Clojure/Babashka), `red` (TypeScript/Bun) and `blue` implement one model:

- a step is `opts -> opts` over one open dict;
- a workflow is a static `wire_fn` graph plus an optional dynamic `next_fn`,
  with parallel fan-out and joins;
- behaviour is layered on with Emacs-style advice;
- desired state is YAML, with a shared `COLORS_PAR_*` environment overlay for
  credentials and one-run overrides;
- dry-run, progress and lifecycle preflight are optional advice modules.

Blue is the foundation for deployment code written by coding agents.
[PocketDeploy](https://github.com/pocketcontext/pocketdeploy) is the starter
package built on it. Steps call cloud CLIs (`gcloud`, `oci`, `doctl`, ...) or
HTTP APIs directly. OpenTofu and Ansible support was removed on 2026-10-29;
the SDK has no infrastructure-tool integration of its own.

The specification is [`index.html`](index.html), published at
<https://getcolors.github.io/blue/>.

## Naming

| green                | red                   | blue                  |
|----------------------|-----------------------|-----------------------|
| `:green/exit` etc.   | `"red/exit"` etc.     | `"blue/exit"` etc.    |
| `green.edn`          | `red.yml`             | `blue.yml`            |
| `./green` (babashka) | `./red` (Bun)         | `./blue` (uv script)  |
| `green.workflow` ns  | `red/workflow` module | `blue.workflow` module|

Namespaced keywords stay namespaced **string keys** (`"hello/file"`). The
engine reserves `blue/*` and `blue.*/*` for its control keys (`"blue/exit"`,
`"blue/err"`, `"blue/trace"`, `"blue/event"`, `"blue/dry-run"`, `"blue/step"`,
`"blue/branches"`, `"blue/state-file"`, plus private keys such as
`"blue.workflow/inherited"`); everything project-specific uses its own
namespace.

## Commands

```sh
uv sync          # install dependencies
uv run pytest    # run the test suite
```

Try the example:

```sh
cd examples/hello
./blue create --dry-run   # uname runs; greet/report are skipped and logged
./blue create             # writes work/<name>.txt per name, then cats them
./blue delete             # removes them and the empty work/ directory
```

## Example

`examples/hello` is the smallest complete package: a `./blue` uv-script
launcher, desired state in `blue.yml` (flat kebab-case keys `workdir` and
`names`), and three steps that call local CLIs directly. `hello/system` runs
`uname -s`; `next_fn` fans out one `hello/greet` branch per name; the branches
join at `hello/report`, which reads their `hello/file` results from
`"blue/branches"` and runs `cat` over them. A relative `workdir` resolves
against `"blue/state-file"`, and `COLORS_PAR_WORKDIR=/tmp/x ./blue create`
overrides it for one run. `tests/test_example_hello.py` keeps it working.

## Modules

Under `src/blue/`, mirroring red's `src/*.ts`:

- `workflow.py` — the engine. A step is `(opts) -> opts` (sync or async),
  named by a namespaced string. `wire_fn(step, run_opts)` returns
  `(fn, *next_steps)` and defines the static graph for the run; optional
  `next_fn(step, default_next, opts)` returns `[(next_step, opts), ...]` for
  fan-out, conditional routing, retries and cleanup paths. Independent
  successors run in parallel asyncio tasks. Branches that converge join once
  with results under `"blue/branches"`; failed forks collapse and propagate the
  worst exit. The step boundary deep-freezes input (`FrozenDict`/`FrozenList`
  raise on mutation), catches raised exceptions, rejects non-dict returns,
  stamps `"blue/step"`, and removes private inherited-advice state from
  results. `step(wf, in_fn=..., out_fn=...)` embeds a workflow as an ordinary
  step. Joins use the deepest shared fork; failed joins select the highest exit
  code and its branch diagnostics, choosing the first incoming branch on ties.
- `advice.py` — Emacs `nadvice`-style combinators: `around`, `before`,
  `after`, `override`, `before-while`, `before-until`, `after-while`,
  `after-until`, `filter-args`, `filter-return`. Registries are immutable
  workflow values. `advice_add` targets one step, `advice_add_all` every step.
  Depth controls stack order (`-100..100`, lower = more outward); equal depth
  uses newest-outermost ordering. While/until combinators use Clojure
  truthiness (only `None`/`False` are falsy). Advice is inherited through
  embedded workflows by flat step name; same-id ancestor advice replaces child
  advice. `advice_plan` shows the composed stack.
- `cli.py` — `load_yaml` (YAML 1.2 core-schema scalars: `no`, dates and
  sexagesimal values stay strings; scientific notation, `0o` octal and `0x`
  hexadecimal are numeric), `read_pars` / `par_name` (the `COLORS_PAR_*`
  overlay), `find_up`, and `run_cli` / `exec_cli`, which parse
  `./blue <event> [-f blue.yml] [--start step] [--end step] [--dry-run]`,
  stamp `"blue/event"`, `"blue/state-file"` and `"blue/dry-run"`, and run the
  workflow. Exit 2 is reserved for usage/config errors. Explicit file options
  override configured defaults; parent-directory search applies only when the
  file option is omitted.
- `dry_run.py` — `dry_run.advise(wf, steps)` adds around advice that logs and
  skips the listed steps when `"blue/dry-run"` is true.
- `progress.py` — `progress.advise(wf)` logs every step's entry and elapsed
  time.
- `lifecycle.py` — `preflight`: defaults, overlay, ordered validators and an
  optional success callback, returning exit 2 with every validation error.
- `runtime.py` — the mutable log seam: `runtime.log` is what `dry_run` and
  `progress` write through, so tests and packages can capture output.

## The ./blue launcher

A project's `./blue` is a **self-contained uv script**: a
`#!/usr/bin/env -S uv run --script` shebang plus PEP 723 inline metadata — no
virtualenv to manage, no requirements file. It holds the workflow definition,
advice wiring and the `exec_cli` call. The in-repo example depends on blue by
relative path via `[tool.uv.sources]`; external consumers pin a git reference
to a commit (nothing is published to PyPI) — never unpinned.

## Conventions

- All cross-step state is one open dict. Engine keys are `blue/*` and private
  `blue.*/*`; project keys are namespace-like strings such as `"hello/file"`.
  Desired-state keys are flat kebab-case so `COLORS_PAR_<KEY>` can reach them.
- Public constructors and advice-transforming functions are pure: they return
  new workflow values and never mutate existing workflows.
- Steps return new dicts (`{**opts, ...}`); the engine freezes step input so a
  raising step cannot leave partial mutations behind.
- Errors are opts values, not exceptions escaping the engine. Raise
  `StepError` only when a step or advice wants the step boundary to convert it
  to `"blue/exit"`/`"blue/err"`/`"blue/trace"`.
- Keep output keys namespaced (`"my.ns/result"`); never merge tool output into
  top-level opts.
- YAML caveat to document in every project: version-like values (`3.10`) must
  be quoted or YAML reads them as numbers.
