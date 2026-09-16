# Kubernetes controllers

`blue.kubernetes` and `blue.kubernetes_client` provide the polling controller and kubectl transport from Green.
Register trusted package callbacks in the controller program. Custom resources
supply desired state, never executable code.

`controller`, `reconcile`, `start`, and `stop` construct configuration, reconcile one resource, start polling,
and drain active work on shutdown. Reconciliation and shutdown are asynchronous.

Call `start` inside a running asyncio loop. Callbacks may be synchronous or asynchronous. Synchronous callbacks run in worker threads so subprocesses do not block polling.

A package supplies `resource`, `validate`, `identity`, `observe`, `converge`, and
`delete`. The resource descriptor contains `group`, `version`, `plural`, and
`kind`. Validation returns a collection of errors, or an empty value. Identity
returns the backend boundary and profile. Use a list of strings or null values
for the same state-identity hash as Green. Observation returns `exists`, `matches`,
and `ready` booleans. Convergence and deletion return workflow outcome maps.

The controller copies `spec.config` into callback options, defaults `profile` to
`<namespace>--<name>`, and adds `blue/event` and `blue.kubernetes/resource`.
It does not overlay environment variables. Packages resolve their credentials.

Common fields are `spec.state`, `spec.suspend`, `spec.reconcileInterval`, and
`spec.deletionPolicy`. Defaults are `running`, false, `60s`, and `Retain`.
Intervals accept positive integers with `ms`, `s`, `m`, or `h`. A changed
`colors.getcolors.ai/reconcile-request` annotation requests an early check.

Before infrastructure effects, the controller stores the profile and identity,
adds its finalizer, and publishes Reconciling. Metadata and status writes carry
the resource version. A conflicting update prevents a stale result from
acknowledging new desired state. Matching but unready infrastructure triggers
observation retries without repeated convergence. Retry delays double from one
second up to one minute. Desired-state edits reset retries. Status contains
generic reasons and never copies callback exceptions or validation details.

Destroy obeys `compute-prevent-destroy`, observes absence before removing the
finalizer, and runs even when the resource is suspended. Retain removes only
`colors.getcolors.ai/infrastructure` and requires no package execution.

Locks are process-local and shared across resource types and controller instances.
Run one controller process. Confirm it has stopped before replacement and
coordinate human or CI writers separately. Shutdown waits for active callbacks;
it does not cancel infrastructure operations. Callbacks own their timeouts.

The kubectl client requires an explicit context or in-cluster authentication.
It supports a dedicated kubeconfig, request and process deadlines, private
input files, and tokenFile authentication that follows service-account token
rotation. Temporary files are removed on success and failure. The injected
runner receives argv, environment, and a millisecond timeout. Transport errors
omit stdout, stderr, and arguments.

Polling has a bounded worker pool and coalesces queued resources. It counts
transport failures and continues polling other resource types. Distributed
leases, watch streams, pagination, and durable workflow recovery are not
implemented.

Configuration uses `packages`, `client`, `workers`, `namespace`, `poll_ms`, `now`,
`retry_base_ms`, and `retry_max_ms`. Client callbacks are `list_resources`,
`get_resource`, `update_status`, and `update_finalizers`. Construct a client with
`kubectl_client`. Client options include `context`, `in_cluster`, `kubeconfig`,
`kubectl`, `request_timeout_seconds`, `timeout_ms`, and `runner`. `request` also
accepts `input`, `raw`, and `timeout_ms` keyword arguments.
