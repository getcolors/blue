# blue

`blue` is the Python/uv workflow SDK of the Colors family. `green`
(Clojure/Babashka), `red` (TypeScript/Bun) and `blue` implement one model:

- a step is `opts -> opts` over one open dict;
- a workflow is a static `wire_fn` graph plus an optional dynamic `next_fn`,
  with parallel fan-out and joins;
- subworkflows embed reusable operations as ordinary steps;
- behaviour is layered on with Emacs-style advice;
- desired state is YAML, with a shared `COLORS_PAR_*` environment overlay for
  credentials and one-run overrides.

Applications own their CLIs, validation, planning, reporting and durable state.
Blue supplies execution and configuration helpers, not deployment policy.

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
| `green.workflow` ns  | `red/workflow` module | `blue.workflow` module|

Namespaced keywords stay namespaced **string keys** (`"hello/file"`). The
engine reserves `blue/*` and `blue.*/*` for its control keys (`"blue/exit"`,
`"blue/err"`, `"blue/trace"`, `"blue/step"`, `"blue/branches"`, plus private keys such as
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
./blue create             # writes work/<name>.txt per name, then cats them
./blue delete             # removes them and the empty work/ directory
```

## Example

`examples/hello` is an offline application with its own argparse CLI and
`blue.yml` input. `hello/system` runs `uname -s`; `next_fn` fans out one
`hello/person` branch per name. Each branch embeds a two-step greeting
subworkflow that writes/removes and verifies a file. The parent joins at
`hello/report`, which reads `"blue/branches"` and runs `cat` on create.
Application-defined around advice logs every stage, including inherited advice
inside the subworkflow. A relative `workdir` resolves against the selected
file; `COLORS_PAR_WORKDIR` overrides it for one run.
`tests/test_example_hello.py` covers these behaviors and child failure handling.

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
- `cli.py` — configuration helpers only, kept at the existing import path:
  `load_yaml` (YAML 1.2 core-schema scalars: `no`, dates and sexagesimal values
  stay strings; scientific notation, `0o` octal and `0x` hexadecimal are
  numeric), and `read_pars` / `par_name` (the `COLORS_PAR_*` overlay).
  Applications decide which file to read and whether to apply the overlay.

## Composition and boundaries

`step(subworkflow, in_fn=..., out_fn=...)` embeds a workflow. By default the
child result is the parent step result, including failure. An explicit
`out_fn` is responsible for preserving or deliberately handling the child's
exit/error. Namespaced step names avoid accidental advice targeting: parent
advice reaches child steps by flat name, and matching ancestor IDs replace
child entries. Use `advice_plan` to inspect the effective stack.

`workflow(start=..., end=...)` retains inclusive engine-level slicing.
Applications can build their own CLI with argparse or another parser and call
`await run(wf, opts)`. There is no SDK CLI, file discovery, lifecycle preflight,
progress logger or dry-run policy. Applications may implement such behavior
with advice; a skipped step is not a deployment plan. The engine does not
persist runs or supply durable recovery: PocketDeploy owns its journal.

Nothing is published to PyPI. External consumers pin a git reference to a
published commit; the in-repo example uses a relative `[tool.uv.sources]` path.

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
