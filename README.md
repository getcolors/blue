# blue

`blue` is the uv/Python implementation of the green workflow model: a library
for building idempotent devops CLIs — desired state in YAML, workflows as step
graphs threaded by a plain dict, template-scaffolded config files, and optional
event-aware OpenTofu and Ansible steps — a package can use them, but does not need them.

The port is **behavioral**: green is the canonical model, and this package
locks its Python behavior into pytest while preserving red-compatible APIs.
Conventions stay structurally identical — a red project ports to blue by
mechanical rename.

## Naming

Everything called `green` (Clojure/Babashka) is called `red` in the
TypeScript/Bun implementation and `blue` here:

| green                      | red                         | blue                        |
|----------------------------|-----------------------------|-----------------------------|
| `:green/exit` etc.         | `"red/exit"` etc.           | `"blue/exit"` etc.          |
| `:green.scaffold/written`  | `"red.scaffold/written"`    | `"blue.scaffold/written"`   |
| `green.edn`                | `red.yml`                   | `blue.yml`                  |
| `./green` (babashka)       | `./red` (Bun)               | `./blue` (uv script)        |
| `green.workflow` ns        | `red/workflow` module       | `blue.workflow` module      |

Namespaced keywords stay namespaced **string keys**: `"zk/servers"`,
`"tofu/outputs"`. The engine reserves `blue/*` and `blue.*/*` for its control
keys (`"blue/exit"`, `"blue/err"`, `"blue/trace"`, `"blue/event"`,
`"blue/dry-run"`, `"blue/step"`, `"blue/branches"`, plus private keys such as
`"blue.workflow/inherited"`); everything project-specific uses its own
namespace.

## Commands

```sh
uv sync          # install dependencies
uv run pytest    # run the test suite
```

The ZooKeeper end-to-end tests drive real `tofu` over HCL containing only
`locals`/`output` blocks and skip when `tofu` is not on `PATH`.

Try examples end-to-end:

```sh
cd examples/zookeeper
./blue create --dry-run
./blue create
./blue delete

cd ../multi-zookeeper
./blue create --dry-run
./blue create
./blue delete

cd ../once
./blue create --dry-run
./blue create
./blue delete

cd ../multi-once
./blue create --dry-run   # offline path; real create needs a real S3 bucket
./blue create
./blue delete

cd ../floci-zookeeper
./blue create --dry-run   # offline path; real create needs Linux + Docker + floci
./blue create
./blue delete
```

## Examples

- `examples/zookeeper` — fake 3-node cluster: dynamic fan-out/join, scaffold +
  tofu, backend-as-advice, dry-run.
- `examples/multi-zookeeper` — two fake clusters from one embedded workflow;
  inherited parent advice reaches child step names.
- `examples/once` — ONCE-style single VPS with provider-swap advice,
  `compute ∥ smtp → dns → smtp-post → (ansible-local ∥ ansible-remote)`, and
  scaffold-only Ansible config.
- `examples/multi-once` — many ONCE boxes from one workflow; parent swaps
  provider/backend advice by inherited id and uses S3 backend keys per
  deployment + step. Real create needs a real S3 bucket; dry-run is offline.
- `examples/floci-zookeeper` — real local 3-node ZooKeeper on floci. Linux-only
  for non-dry-run because it connects directly to Docker-bridge IPs. Requires a
  floci container named `floci`, `tofu`, `ansible-playbook`, `docker`, and
  `aws`. Dry-run works offline and should touch nothing. The opt-in e2e test is
  `BLUE_FLOCI_E2E=1 uv run pytest tests/test_floci_zookeeper.py`.

## Architecture

Modules under `src/blue/`, mirroring red's `src/*.ts`:

- `workflow.py` — workflow engine. A step is `(opts) -> opts` (sync or
  async), named by a namespaced string. `wire_fn(step, run_opts)` defines the
  static graph for the run; optional `next_fn(step, default_next, opts)`
  handles fan-out, conditional routing, retries, and cleanup paths.
  Independent successors run in parallel asyncio tasks. Branches that
  converge join once with results under `"blue/branches"`; failed forks
  collapse and propagate the worst exit. The step boundary deep-freezes input
  (`FrozenDict`/`FrozenList` raise on mutation), recursively freezes tuple and
  frozenset members, catches raised exceptions,
  rejects non-dict returns, stamps `"blue/step"`, and removes private
  inherited-advice state from results. `step(wf, in_fn=..., out_fn=...)` embeds
  workflows as ordinary steps. Unsupported mutable values and custom instances
  fail before the step runs. Callables remain usable; their captured state is
  outside the input freezing contract. Joins use the deepest shared fork and
  preserve enclosing forks. Failed joins select the highest exit code and its
  branch diagnostics, choosing the first incoming branch when codes tie.

- `advice.py` — Emacs `nadvice`-style combinators: `around`, `before`,
  `after`, `override`, `before-while`, `before-until`, `after-while`,
  `after-until`, `filter-args`, and `filter-return`. Registries are immutable
  workflow values. `advice_add` targets one step, `advice_add_all` every
  step. Depth controls stack order (`-100..100`, lower = more outward); equal
  depth uses newest-outermost ordering. The while/until combinators use
  Clojure truthiness (only `None`/`False` are falsy). Advice is inherited
  through embedded workflows by flat step name; same-id ancestor advice
  replaces child advice. `advice_plan` shows the composed stack.

- `renderer.py` — small internal Selmer-compatible renderer used by
  scaffolding. Supports variables, dotted paths, missing values as empty,
  HTML escaping by default, `safe`, `not-empty`, `sort(attribute='...')`,
  `for` loops, `if` blocks, and
  delimiter overrides (`tag_open`/`tag_close`/`filter_open`/`filter_close`)
  for templates that must preserve Jinja2 `{{ }}` / `{% %}`.

- `scaffold.py` — flat file-spec DSL. Template specs render content owned by
  the package; `content_spec` writes computed content exactly. Delete removes
  the same targets and prunes empty parent directories.
  `PRESERVE_JINJA_DELIMITERS` is the shared alternate-delimiter preset.

- `tofu.py` — event-aware OpenTofu steps. Non-`"delete"` runs `init` +
  `apply` and merges `tofu output -json` under `"tofu/outputs"` by default.
  `"delete"` runs `init` + `destroy`. Backends are `before` advice
  (`local_backend_advice`, `s3_backend_advice`, `gcs_backend_advice`,
  `r2_backend_advice`, `backend_advice`). `conventional_backend_advice`
  supplies package local/S3/R2 policy with explicit directory and state-key
  functions. Commands accept per-run environments;
  `tofu_with_spec` supports build, and deterministic HCL/JSON helpers match
  Green's bytes.

- `ansible.py` — event-aware Ansible steps. Non-`"delete"` runs the create
  playbook, `"delete"` the delete playbook. Parses PLAY RECAP under
  `"ansible/recap"` by default. `inventory_advice` writes deterministic INI
  inventories. `ansible_with_spec` scaffolds then runs (create) or runs then
  removes (delete).

- `dry_run.py` — dry-run advice. `dry_run.advise(wf, steps)` adds around
  advice that logs and skips listed steps when `"blue/dry-run"` is true.

- `progress.py` — all-step progress advice logging entry/exit timings.

- `cli.py` — `run_cli` / `exec_cli`: parses `./blue <event> [-f blue.yml]
  [--start step] [--end step] [--dry-run]`, loads YAML with 1.2 core-schema
  scalars (`no`, dates and sexagesimal values stay strings; scientific notation,
  octal and hexadecimal values are numeric), overlays
  `COLORS_PAR_*`, stamps `"blue/event"`, and runs the workflow. Exit 2 is
  reserved for usage/config errors. Explicit file options override configured
  defaults; parent-directory search applies only when the file option is omitted.

- `lifecycle.py` — configurable package preflight with ordered validation and
  success callbacks.

- `providers.py` — operations over caller-owned provider registries.

- `process.py` — inherited-terminal execution, POSIX quoting, and configurable
  sequential command plans.

- `gates.py` — pydantic schema gates for `before-while` (the port of red's
  Zod gates). Gates validate and return the original opts; they must never
  replace opts with parsed output, which would strip unrelated namespace
  keys. Functional-syntax `TypedDict`s carry namespaced string keys.

- `runtime.py` is the mutable test seam for subprocesses and log lines.
  Captured commands use asynchronous `runtime.exec` with per-command environments
  and timeouts. `run_inherit` uses synchronous `runtime.exec_inherit` and inherits
  terminal input and output. Tests can stub either method. Results use zero for
  success and positive codes for failure: signals become `128 + signal`,
  timeouts return 124, and spawn failures return 127. On POSIX, timeout and
  cancellation of captured commands kill the command's process group. Cleanup
  waits at most one second for output pipes; descendants that start their own session can escape
  the group. Environment overrides set to `None` remove inherited variables.

## The ./blue launcher

A project's `./blue` is a **self-contained uv script**: a
`#!/usr/bin/env -S uv run --script` shebang plus PEP 723 inline metadata —
no virtualenv to manage, no requirements file. It holds the workflow
definition, advice wiring, and the `exec_cli` call, plus reads of its local
templates. The in-repo example depends on blue by relative path via
`[tool.uv.sources]`; external consumers pin a version
(`dependencies = ["blue==x.y.z"]`) or a pinned git reference — never
unpinned.

## Conventions

Same as red's behavioral contract, with `blue/*` in place of `red/*`; see this
repository's [`index.html`](index.html) specification:

- All cross-step state is one open dict. Engine keys are `blue/*` and private
  `blue.*/*`; project/library keys are namespace-like strings such as
  `"zk/servers"`, `"tofu/outputs"`.
- Public constructors and advice-transforming functions are pure: they return
  new workflow values and never mutate existing workflows.
- Steps return new dicts (`{**opts, ...}`); the engine freezes step input so
  a raising step cannot leave partial mutations behind.
- Errors are opts values, not exceptions escaping the engine. Raise
  `StepError` only when a step/advice wants the step boundary to convert it
  to `"blue/exit"`/`"blue/err"`/`"blue/trace"`.
- Keep output keys namespaced (`"my.ns/result"`); never merge provider
  outputs into top-level opts by default.
- YAML caveat to document in every project: version-like values (`3.10`)
  must be quoted or YAML reads them as numbers.

## Kubernetes controllers

`blue.kubernetes` and `blue.kubernetes_client` provide bounded polling workers, state-identity locks,
optimistic status writes, finalization, and explicit-context kubectl transport.
See the [controller contract](docs/kubernetes.md) for callbacks and lifecycle rules.

## Runtime resource scopes

`blue.scope.with_scope` owns ordered process/resource finalizers around a workflow.
See [resource scopes](docs/resource-scopes.md).
