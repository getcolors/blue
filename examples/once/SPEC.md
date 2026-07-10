# `once` example — spec

A blue example modeling a single-machine PaaS in the style of Basecamp **ONCE**:
one VPS runs dockerized websites, fronted by DNS and backed by a transactional
email sender. The example provisions fake infrastructure with OpenTofu
(`locals`/`output` only) and scaffolds the Ansible files that would configure
the box. It does **not** invoke `ansible-playbook`.

## What this teaches

- **zookeeper** — fan-out/join, scaffold + tofu, backend-as-advice, dry-run.
- **multi-zookeeper** — workflow composition with `step`, inherited advice.
- **once** — provider-swap advice on `once/compute`, a real fork/join
  (`compute ∥ smtp → dns`), threaded opts, isolated tofu state per step, and
  scaffold-only Ansible config.

## Desired state (`blue.yml`)

```yaml
once/workdir: work
once/host: {name: vps-1, region: fra1, size: s-1vcpu-1gb}
once/ssh:
  compute: "ssh-ed25519 AAAA...compute"
  deploy: "ssh-ed25519 AAAA...deploy"
once/website:
  - {name: campfire, image: "37signals/campfire:latest", hostname: chat.example.com}
  - {name: writebook, image: "37signals/writebook:latest", hostname: books.example.com}
```

Provider and backend are intentionally not in YAML: they are selected by advice.

## Events

```sh
./blue create
./blue create --dry-run
./blue delete
```

Every tofu step treats any non-`"delete"` event as create (`init` + `apply`) and
`"delete"` as destroy. Scaffold steps render on create and remove their targets
on delete.

## Workflow graph

```text
compute ─┐                              ┌─► ansible-local
         ├─► dns ──► smtp-post ─────────┤
smtp ────┘                              └─► ansible-remote
```

- `once/compute` and `once/smtp` run in parallel.
- `once/dns` joins both branches: it reads compute outputs (`ip`, host users)
  and smtp outputs (DNS auth records), then carries them forward under
  `"once/server"` and `"once/smtp"`.
- `once/smtp-post` runs after DNS, then forks to two independent scaffold-only
  Ansible steps.

## Advice

### `once/provider`

`once/compute` is provider-agnostic. A `before` advice writes provider-specific
`main.tf` before tofu runs. The default example attaches DigitalOcean mock HCL;
parent workflows can re-add the same advice id to replace it (for example,
`examples/multi-once` picks DigitalOcean or OCI per deployment).

### `once/backend`

Every tofu step gets its backend as `before` advice and has isolated state:

- `server/` for `once/compute`
- `smtp/` for `once/smtp`
- `dns/` for `once/dns`
- `smtp-post/` for `once/smtp-post`

The standalone example uses local backends. `examples/multi-once` swaps the same
advice id to S3 and derives a distinct key per deployment and per step.

## Working directory

```text
work/
  server/        backend.tf main.tf terraform.tfstate
  smtp/          backend.tf main.tf terraform.tfstate
  dns/           backend.tf main.tf terraform.tfstate
  smtp-post/     backend.tf main.tf terraform.tfstate
  ansible-local/config
  ansible-remote/playbook.yml
```

## Mock modules

All infrastructure is fake but exercised by real `tofu` when not in dry-run:

- DigitalOcean/OCI mock for compute, both emitting the same output contract.
- SMTP mock emitting credentials and DNS auth records.
- Cloudflare mock consuming website hostnames plus SMTP auth records.
- SMTP post-verification mock consuming the SMTP identity id.

This keeps the example offline and deterministic while still testing scaffold,
backend advice, tofu invocation, fork/join behavior, and delete cleanup.
