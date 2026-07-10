# floci-zookeeper — a real ZooKeeper cluster on your machine

Unlike `examples/zookeeper` (fake HCL, nothing real), this example builds a
**real 3-node ZooKeeper ensemble**. OpenTofu talks to
[floci](https://github.com/floci-io/floci), a local AWS emulator on
`localhost:4566`, which backs each `aws_instance` with a Docker container.
Ansible then provisions ZooKeeper over SSH, and a health step asks every node
`srvr` and requires one leader plus two followers.

```sh
./blue create --dry-run   # print the run; touches nothing, needs nothing
./blue create             # tofu -> floci EC2 -> ansible -> health
./blue delete             # ansible delete-node.yml first, then tofu destroy
```

## Requirements

- Linux with Docker. The example connects directly to Docker-bridge IPs;
  Docker Desktop on macOS/Windows usually cannot route to them.
- A floci container named `floci` listening on `localhost:4566`. The workflow
  restarts this existing container for a clean create when no instances are
  running; it does not create the container for you.
- `tofu`, `ansible-playbook`, `docker`, and `aws` on `PATH`.
- Outbound network from the instance containers for `apt` and the ZooKeeper
  tarball download.

Optional: set `TF_PLUGIN_CACHE_DIR` to an existing directory so the per-node
`tofu init` runs share one AWS provider download.

An opt-in e2e test drives real create/idempotent-create/delete:

```sh
BLUE_FLOCI_E2E=1 uv run pytest tests/test_floci_zookeeper.py
```

## What happens on create

1. `zk/start` validates `blue.yml`, checks tools, ensures the SSH keypair under
   `work/ssh/`, restarts floci if there are no running instances, and applies a
   shared key-pair tofu root.
2. The workflow fans out one `zk/node` branch per server. Each branch scaffolds
   `main.tf`, writes a local backend via advice, and runs tofu against floci.
3. `zk/provision` joins the tofu branches and collects observed Docker-bridge
   IPs from `tofu output -json`.
4. The workflow fans out one `zk/ansible` branch per server. Each branch writes
   a single-host inventory and runs `create.yml`, passing the full ensemble as
   extra-vars so `zoo.cfg.j2` lists every peer.
5. `zk/health` joins the Ansible branches, polls ZooKeeper with the `srvr`
   four-letter word, and retries until quorum converges.

A second `create` is idempotent against the existing cluster.

## What happens on delete

`delete` fans out one embedded sub-workflow per node:

```text
zk/ansible (delete-node.yml) -> zk/node (tofu destroy)
```

The embedded workflow keeps per-node teardown branches independent; without it,
branches from different parents would join at `zk/node`. Parent advice on
`zk/ansible` and `zk/node` is inherited into each sub-workflow by step name.

## Files

- `blue` — self-contained uv launcher.
- `blue.yml` — desired state.
- `resources/zk/shared.tf` — shared floci/AWS key pair tofu root.
- `resources/zk/main.tf` — per-node AWS provider + security group + instance.
- `resources/zk/ansible/create.yml` — installs and starts ZooKeeper.
- `resources/zk/ansible/delete-node.yml` — stops/removes ZooKeeper on one host.
- `resources/zk/ansible/zoo.cfg.j2` — real Jinja2 template preserved by blue's
  custom scaffold delimiters.

## Cleanup

`./blue delete` removes the cluster but leaves floci and generated keys in
place. For a full reset:

```sh
docker rm -f floci
docker volume rm floci-data
rm -rf work
```
