# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`blue` is the Python/uv workflow SDK of the Colors family: steps `opts ->
opts`, `wire_fn` graphs with a dynamic `next_fn`, fan-out and joins, layered
advice, and YAML desired state with a `COLORS_PAR_*` overlay. It is the
foundation for deployment code written by coding agents; steps call cloud CLIs
or APIs directly. OpenTofu/Ansible integration, scaffolding, schema gates,
Kubernetes controllers, resource scopes and the subprocess runner were removed
on 2026-10-29 — do not reintroduce them here; they belong in packages.

**`README.md` is the primary document and it is current.** It covers every
module under `src/blue/`, the `./blue` uv-script launcher, the example, and the
naming table that maps `green` → `red` → `blue`. Read it before editing; this
file only adds what a reader of the README alone would not know.

## Commands

```sh
uv sync          # install dependencies
uv run pytest    # run the test suite
```

Run one file with `uv run pytest tests/test_workflow.py`, one test with
`-k <name>`. `tests/test_example_hello.py` keeps `examples/hello` working; it
needs `uname` and `cat` on `PATH`.

This repository expects its toolchain on `PATH`; there is no devenv/direnv setup
here.

## Blue is one of three, not a standalone library

`green` (Clojure/Babashka), `red` (TypeScript/Bun), and `blue` are three
implementations of one model, and downstream packages built on them are required
to behave identically: the same desired-state file, the same opts keys, the
same workflow semantics. Green is the canonical model — when blue and green
disagree, green is right by default and blue is the thing to fix.

The consequence for work in this repository: **a change to shared behaviour is
not finished here.** No test in this repository compares blue with green and
red. The cross-colour parity harness lived in the now-archived `once` monorepo
(`scripts/parity.sh`, read-only on GitHub): it fed one fixture to all three
colours and diffed the results. It remains a reference for what parity means,
but no current consumer runs it, so a change landed in blue alone can pass
every test here and still diverge from green.

### The YAML resolver replacement is load-bearing

`src/blue/cli.py` replaces PyYAML's implicit **bool**, **int**, **float** and **timestamp** resolvers and
the int constructor, because PyYAML defaults to YAML 1.1 while green (yamlstar)
and red (`Bun.YAML`) follow the 1.2 core schema. The three disagree without it
about whether `no` is a string, whether `012` is octal, and whether `1:30` is a
number.

Do not relax, simplify, or "modernise" that block, and do not swap the YAML
library for one that seems more convenient. The archived `once` monorepo's
`test/parity/scalars.yml` records the expected answers and asserted that all
three colours type every scalar identically. A reader that drifts off the 1.2 core schema is a parity break that
no test in this repository will report.

## Downstream pinning

Nothing here is published to PyPI. Consumers depend on this repository by
**pinned git SHA**. The current consumer is PocketDeploy
(`github.com/pocketcontext/pocketdeploy`), which imports `blue.cli` and
`blue.workflow` and pins blue as a direct git reference in the `dependencies`
of its `pyproject.toml`. Its contract is `blue.workflow.workflow`/`run`,
`blue.cli.load_yaml`/`read_pars`/`par_name`, and the `"blue/exit"`/`"blue/err"`
keys; keep those signatures and behaviour stable. The getcolors controllers built on PocketDeploy
(`alice`, `walter`, `rybbit`) receive blue through their `uv.lock`. The
archived ONCE package (`once/blue/pyproject.toml`, pinned under
`[tool.uv.sources]` and copied into the `package-once-blue` launcher) is a
historical example of the same pattern. So:

- A commit here is invisible downstream until it is pushed *and* the consumer's
  pin is moved. Never invent a SHA or hand-edit a pin; move it with the
  consumer's own procedure (for example `uv lock` in that repository).
- Consumer docs and launcher snippets must use a pinned git reference. Do not
  recommend `blue==x.y.z` until a PyPI release actually exists.
- In-repo examples may keep the relative `[tool.uv.sources]` path for
  development.

**Name collision to watch for:** `once/blue/` in the archived `once` monorepo
is a different codebase — the ONCE package built *on* this library.
Instructions there are not instructions for here.

## Documentation

`index.html` is this repository's landing page and carries two analytics tags:
GA4 measurement ID `G-4VKP1WY4QJ`, whose explicit `page_title` must exactly
equal the decoded HTML `<title>` and stay distinct and stable so one Analytics
property can separate repositories, and the self-hosted Rybbit snippet
`<script src="https://rybbit.getcolors.ai/api/script.js" data-site-id="9fb9c41a6d49" defer></script>`,
which shares one site ID across every page because `getcolors.github.io/<repo>/`
paths already encode the repository. Never add one tag without the other.
