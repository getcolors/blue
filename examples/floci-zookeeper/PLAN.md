# Plan: floci-zookeeper blue port

This example is the real-infrastructure counterpart to the fake ZooKeeper
example. It keeps the same blue workflow semantics while exercising real local
resources through floci.

## Design

- `zk/start` validates desired state, checks runtime tools, ensures the SSH
  keypair, prepares floci, and applies a shared `aws_key_pair` tofu root.
- Create path:
  - fan out `zk/node` once per server;
  - join at `zk/provision` and collect observed IPs;
  - fan out `zk/ansible` once per server;
  - join at `zk/health` and require quorum.
- Delete path:
  - fan out `zk/teardown` once per server;
  - each teardown is an embedded workflow: `zk/ansible -> zk/node`, so branches
    do not join at `zk/node`.

## Advice used

- `before-while` gates on `zk/start` for schema and requirements.
- `before` advice on `zk/start` for floci/SSH setup and shared tofu backend.
- `before` advice on `zk/node` for per-node tofu backends. Backend advice writes
  `backend.tf.json` while preserving native JSON values and nested collections.
- `filter-args` advice on `zk/ansible` to read tofu state during delete.
- `before` advice on `zk/ansible` for per-node inventory and SSH readiness.
- `around` retry advice on `zk/health` while ZooKeeper elects a leader.
- `progress.advise` and `dry_run.advise` for observability/offline dry-runs.

## floci assumptions

- floci runs as a Docker container named `floci` and exposes the LocalStack API
  on `localhost:4566`.
- floci's Docker-bridge IPs are reachable from the host (Linux).
- `aws_instance.private_ip` reports the container IP; no host-port plumbing is
  needed.
- A tiny sshd user-data bootstrap remains in `main.tf` to work around floci
  AMIs starting sshd before its privilege-separation directories exist. All
  real provisioning still happens through Ansible.

## Verified locally

- `./blue create --dry-run` touches nothing.
- `./blue delete --dry-run` touches nothing.
- The pytest suite includes an opt-in real floci end-to-end test.

Real `create`/`delete` require Docker, floci, tofu, AWS CLI, Ansible, and
network access for the instance containers.
