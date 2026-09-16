"""Polling controllers with optimistic writes and process-local state locks.

Run one controller process and coordinate external infrastructure writers separately.
Synchronous callbacks run in worker threads so subprocesses do not block polling.
"""

import asyncio
import hashlib
import inspect
import json
import re
import threading
import time
from datetime import datetime, timezone

finalizer = "colors.getcolors.ai/infrastructure"
request_annotation = "colors.getcolors.ai/reconcile-request"
_held = set()
_lock = threading.Lock()


async def _call(fn, *args):
    result = (
        await fn(*args)
        if inspect.iscoroutinefunction(fn)
        else await asyncio.to_thread(fn, *args)
    )
    return await result if inspect.isawaitable(result) else result


def controller(options=None, **kwargs):
    cfg = dict(
        workers=2,
        poll_ms=500,
        namespace="default",
        now=lambda: time.time() * 1000,
        retry_base_ms=1000,
        retry_max_ms=60000,
    )
    cfg.update(options or {})
    cfg.update(kwargs)
    client = cfg.get("client", {})
    packages = cfg.get("packages", [])
    if (
        not packages
        or not isinstance(client, dict)
        or not all(
            callable(client.get(k))
            for k in (
                "list_resources",
                "get_resource",
                "update_status",
                "update_finalizers",
            )
        )
        or not all(
            isinstance(p, dict)
            and all(
                isinstance(p.get("resource", {}).get(k), str)
                for k in ("group", "version", "plural", "kind")
            )
            and all(
                callable(p.get(k))
                for k in ("validate", "identity", "observe", "converge", "delete")
            )
            for p in packages
        )
        or not all(type(cfg[k]) is int and cfg[k] > 0 for k in ("workers", "poll_ms"))
    ):
        raise ValueError("Invalid Kubernetes controller configuration")
    return cfg


def _identity_text(value):
    if isinstance(value, (list, tuple)):
        return "[" + " ".join(map(_identity_text, value)) + "]"
    if value is None:
        return "nil"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                json.dumps(k, ensure_ascii=False) + " " + _identity_text(v)
                for k, v in value.items()
            )
            + "}"
        )
    return json.dumps(value, ensure_ascii=False)


def _key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _instant(ms):
    return (
        datetime.fromtimestamp(int(ms) / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
        .replace(".000Z", "Z")
    )


def _millis(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except (TypeError, ValueError, AttributeError):
        return 0


def _interval(r):
    value = r.get("spec", {}).get("reconcileInterval", "60s")
    match = (
        re.fullmatch(r"([1-9][0-9]*)(ms|s|m|h)", value)
        if isinstance(value, str)
        else None
    )
    if match:
        result = int(match[1]) * dict(ms=1, s=1000, m=60000, h=3600000)[match[2]]
        return result if result <= 2**53 - 1 else None


def _request(r):
    return r["metadata"].get("annotations", {}).get(request_annotation, "")


def _changed(r):
    s = r.get("status", {})
    return s.get("observedGeneration") != r["metadata"].get("generation", 1) or s.get(
        "lastHandledRequest"
    ) != _request(r)


def _retries(r):
    return 0 if _changed(r) else r.get("status", {}).get("retryCount", 0)


def _due(c, r):
    s = r.get("status", {})
    return (
        _changed(r)
        or (r["metadata"].get("deletionTimestamp") and not s.get("deletionObserved"))
        or (
            s.get("phase") not in ("Invalid", "Suspended")
            and _millis(s.get("nextReconcileTime")) <= c["now"]()
        )
    )


def _options(r):
    spec, meta = r.get("spec", {}), r["metadata"]
    config = spec.get("config", {})
    profile = config.get("profile")
    if profile is None or profile is False:
        profile = f"{meta.get('namespace')}--{meta.get('name')}"
    return {
        **config,
        "profile": profile,
        "blue/event": "delete"
        if meta.get("deletionTimestamp")
        else "stop"
        if spec.get("state") == "stopped"
        else "create",
        "blue.kubernetes/resource": r,
    }


async def _publish(c, p, r, o, identity, phase, reason, ready, extra=None):
    now, generation = _instant(c["now"]()), r["metadata"].get("generation", 1)
    previous = next(
        (
            v
            for v in r.get("status", {}).get("conditions", [])
            if v.get("type") == "Ready"
        ),
        {},
    )
    value = "True" if ready else "False"
    status = dict(
        phase=phase,
        profile=o.get("profile"),
        stateIdentity=identity,
        observedGeneration=generation,
        lastHandledRequest=_request(r),
        lastReconcileTime=now,
        retryCount=0,
        deletionObserved=bool(r["metadata"].get("deletionTimestamp")),
        conditions=[
            dict(
                type="Ready",
                status=value,
                reason=reason,
                observedGeneration=generation,
                lastTransitionTime=previous.get("lastTransitionTime")
                if previous.get("status") == value and previous.get("reason") == reason
                else now,
            )
        ],
    )
    status.update(extra or {})
    return await _call(c["client"]["update_status"], p["resource"], r, status)


async def _failure(c, p, r, o, identity, reason):
    attempt = _retries(r) + 1
    delay = min(c["retry_max_ms"], c["retry_base_ms"] * 2 ** min(20, attempt - 1))
    return await _publish(
        c,
        p,
        r,
        o,
        identity,
        "Failed",
        reason,
        False,
        dict(retryCount=attempt, nextReconcileTime=_instant(c["now"]() + delay)),
    )


def _successful(result):
    if not isinstance(result, dict) or result.get("blue/exit", 0) != 0:
        raise RuntimeError("Package workflow failed")


async def _locked(c, p, r, o, identity):
    spec, meta, old = r.get("spec", {}), r["metadata"], r.get("status", {})
    deleting, policy = (
        bool(meta.get("deletionTimestamp")),
        spec.get("deletionPolicy", "Retain"),
    )
    finals = meta.get("finalizers", [])
    finalized = finalizer in finals
    remaining = [v for v in finals if v != finalizer]
    write = c["client"]["update_finalizers"]
    if deleting and (policy == "Retain" or not finalized):
        if finalized:
            await _call(write, p["resource"], r, remaining)
        return
    if (old.get("profile") is not None and old["profile"] != o["profile"]) or (
        old.get("stateIdentity") is not None and old["stateIdentity"] != identity
    ):
        await _publish(
            c,
            p,
            r,
            {**o, "profile": old.get("profile")},
            old.get("stateIdentity"),
            "Invalid",
            "ImmutableStateIdentity",
            False,
        )
        return
    if (
        spec.get("state", "running") not in ("running", "stopped")
        or policy not in ("Retain", "Destroy")
        or _interval(r) is None
    ):
        await _publish(c, p, r, o, identity, "Invalid", "InvalidConfiguration", False)
        return
    try:
        errors = await _call(p["validate"], o)
    except Exception:
        await _failure(c, p, r, o, identity, "ValidationFailed")
        return
    if errors:
        await _publish(c, p, r, o, identity, "Invalid", "InvalidConfiguration", False)
        return
    if deleting and o.get("compute-prevent-destroy") is True:
        await _publish(
            c,
            p,
            r,
            o,
            identity,
            "Blocked",
            "DestructionProtected",
            False,
            dict(nextReconcileTime=_instant(c["now"]() + _interval(r))),
        )
        return
    if not deleting and spec.get("suspend") is True:
        await _publish(c, p, r, o, identity, "Suspended", "Suspended", False)
        return
    if not deleting and not finalized:
        r = await _call(write, p["resource"], r, [*finals, finalizer])
    phase = "Deleting" if deleting else "Reconciling"
    active = await _publish(
        c, p, r, o, identity, phase, phase, False, dict(retryCount=_retries(r))
    )
    try:
        if deleting:
            if (await _call(p["observe"], o)).get("exists") is True:
                _successful(await _call(p["delete"], o))
            if (await _call(p["observe"], o)).get("exists") is not False:
                raise RuntimeError("Infrastructure remains after deletion")
            await _call(write, p["resource"], active, remaining)
        else:
            if (await _call(p["observe"], o)).get("matches") is not True:
                _successful(await _call(p["converge"], o))
            found = await _call(p["observe"], o)
            if found.get("matches") is True and found.get("ready") is True:
                await _publish(
                    c,
                    p,
                    active,
                    o,
                    identity,
                    "Ready",
                    "Converged",
                    True,
                    dict(nextReconcileTime=_instant(c["now"]() + _interval(r))),
                )
            else:
                await _failure(c, p, active, o, identity, "NotReady")
    except Exception as error:
        if getattr(error, "status", None) != 409:
            await _failure(c, p, active, o, identity, "ExecutionFailed")


async def reconcile(c, package, namespace, name):
    """Reread one resource and reconcile it; return 'busy' for a held identity."""
    get = c["client"]["get_resource"]
    r = await _call(get, package["resource"], namespace, name)
    if not r or not _due(c, r):
        return
    identity = await _call(package["identity"], _options(r))
    key = _key(identity)
    with _lock:
        if key in _held:
            return "busy"
        _held.add(key)
    try:
        latest = await _call(get, package["resource"], namespace, name)
        if (
            latest
            and latest["metadata"].get("uid") == r["metadata"].get("uid")
            and key == _key(await _call(package["identity"], _options(latest)))
            and _due(c, latest)
        ):
            await _locked(
                c,
                package,
                latest,
                _options(latest),
                hashlib.sha256(
                    _identity_text(identity).encode()
                ).hexdigest(),
            )
    finally:
        with _lock:
            _held.remove(key)


def start(c):
    """Start polling on the current asyncio loop with a bounded worker pool."""
    rt = dict(config=c, running=True, errors=0, stop_signal=asyncio.Event())
    queue, pending = asyncio.Queue(), set()

    async def pause(ms):
        try:
            await asyncio.wait_for(rt["stop_signal"].wait(), ms / 1000)
        except TimeoutError:
            pass

    async def worker():
        while rt["running"]:
            try:
                key, p, namespace, name = queue.get_nowait()
            except asyncio.QueueEmpty:
                await pause(10)
                continue
            try:
                await reconcile(c, p, namespace, name)
            except Exception:
                rt["errors"] += 1
            finally:
                pending.remove(key)

    async def poller():
        while rt["running"]:
            for p in c["packages"]:
                if not rt["running"]:
                    break
                try:
                    for r in await _call(
                        c["client"]["list_resources"], p["resource"], c["namespace"]
                    ):
                        if not rt["running"]:
                            break
                        namespace, name = (
                            r["metadata"].get("namespace", c["namespace"]),
                            r["metadata"]["name"],
                        )
                        key = _key([p["resource"], namespace, name])
                        if key not in pending:
                            pending.add(key)
                            queue.put_nowait((key, p, namespace, name))
                except Exception:
                    rt["errors"] += 1
            await pause(c["poll_ms"])

    rt["workers"] = [asyncio.create_task(worker()) for _ in range(c["workers"])]
    rt["poller"] = asyncio.create_task(poller())
    return rt


async def stop(rt):
    """Stop accepting work and wait for active callbacks without cancelling them."""
    rt["running"] = False
    rt["stop_signal"].set()
    await rt["poller"]
    await asyncio.gather(*rt["workers"])
