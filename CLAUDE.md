# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`blue` is the uv/Python implementation of the green workflow model: a library
for building idempotent devops CLIs — YAML desired state, workflows as step
graphs threaded by a plain dict, template-scaffolded config files, OpenTofu and
Ansible as the muscle.

**`README.md` is the primary document and it is current.** It covers every
module under `src/blue/`, the `./blue` uv-script launcher, the examples, and the
naming table that maps `green` → `red` → `blue`. Read it before editing; this
file only adds what a reader of the README alone would not know.

## Commands

```sh
uv sync          # install dependencies
uv run pytest    # run the test suite
```

Run one file with `uv run pytest tests/test_workflow.py`, one test with
`-k <name>`. The ZooKeeper end-to-end tests drive real `tofu` and **skip when
`tofu` is not on `PATH`** — a green run does not prove they executed. The floci
suite is opt-in: `BLUE_FLOCI_E2E=1 uv run pytest tests/test_floci_zookeeper.py`.

This repository expects its toolchain on `PATH`; there is no devenv/direnv setup
here, unlike the downstream `once` repositories.

## Blue is one of three, not a standalone library

`green` (Clojure/Babashka), `red` (TypeScript/Bun), and `blue` are three
implementations of one model, and downstream packages built on them are required
to be byte-compatible: the same desired-state file, the same generated files,
the same OpenTofu state. Green is the canonical model — when blue and green
disagree, green is right by default and blue is the thing to fix.

The consequence for work in this repository: **a change to shared behaviour is
not finished here.** The parity harness that would catch a divergence lives
downstream in the `once` monorepo (`scripts/parity.sh`), and it feeds one
fixture to all three colours and diffs the results. A change landed in blue
alone passes every test in this repository and breaks parity there.

### The YAML resolver replacement is load-bearing

`src/blue/cli.py` replaces PyYAML's implicit **bool** and **int** resolvers and
the int constructor, because PyYAML defaults to YAML 1.1 while green (yamlstar)
and red (`Bun.YAML`) follow the 1.2 core schema. The three disagree without it
about whether `no` is a string, whether `012` is octal, and whether `1:30` is a
number.

Do not relax, simplify, or "modernise" that block, and do not swap the YAML
library for one that seems more convenient. `once/test/parity/scalars.yml`
pins the expected answers and asserts all three colours type every scalar
identically. A reader that drifts off the 1.2 core schema is a parity break that
no test in this repository will report.

## Downstream pinning

Nothing here is published to PyPI. Consumers depend on this repository by
**pinned git SHA** — `once/blue/pyproject.toml` has the pin under
`[tool.uv.sources]`, and the installed `package-once-blue` launcher carries a
copy of it. So:

- A commit here is invisible downstream until it is pushed *and* the consumer's
  pin is moved. Never invent a SHA or hand-edit a pin; the `once` monorepo
  stamps launchers with `bb pin`.
- Consumer docs and launcher snippets must use a pinned git reference. Do not
  recommend `blue==x.y.z` until a PyPI release actually exists.
- In-repo examples may keep the relative `[tool.uv.sources]` path for
  development.

**Name collision to watch for:** `once/blue/` in the `once` monorepo is a
different codebase — the ONCE package built *on* this library. Instructions
there are not instructions for here.

## Documentation

`index.html` is this repository's landing page and carries two analytics tags:
GA4 measurement ID `G-4VKP1WY4QJ`, whose explicit `page_title` must exactly
equal the decoded HTML `<title>` and stay distinct and stable so one Analytics
property can separate repositories, and the self-hosted Rybbit snippet
`<script src="https://rybbit.getcolors.ai/api/script.js" data-site-id="9fb9c41a6d49" defer></script>`,
which shares one site ID across every page because `getcolors.github.io/<repo>/`
paths already encode the repository. Never add one tag without the other.
