"""Explicit-context kubectl transport with private temporary input files."""

import json
import os
import tempfile
from pathlib import Path
from .runtime import runtime
from .kubernetes import _call


async def request(config, args, *, input=None, raw=False, timeout_ms=None):
    with tempfile.TemporaryDirectory(prefix="blue-kubernetes-") as directory:
        cfg, args = dict(config), list(args)

        def write(name, text):
            path = Path(directory) / name
            path.touch(mode=0o600)
            path.write_text(text)
            return str(path)

        if input is not None:
            index = next(
                (
                    i
                    for i, value in enumerate(args)
                    if value == "-" and i > 0 and args[i - 1] in ("-f", "--filename")
                ),
                None,
            )
            if index is None:
                raise ValueError("request input requires -f -")
            args[index] = write("input.json", input)
        if cfg.get("in_cluster"):
            host = cfg.get(
                "service_host", os.environ.get("KUBERNETES_SERVICE_HOST", "")
            )
            port = cfg.get(
                "service_port", os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            )
            if not host.strip():
                raise ValueError("In-cluster Kubernetes service address is unavailable")
            address = f"[{host}]" if ":" in host else host
            base = "/var/run/secrets/kubernetes.io/serviceaccount"
            data = {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {
                        "name": "in-cluster",
                        "cluster": {
                            "server": f"https://{address}:{port}",
                            "certificate-authority": f"{base}/ca.crt",
                        },
                    }
                ],
                "users": [
                    {"name": "service-account", "user": {"tokenFile": f"{base}/token"}}
                ],
                "contexts": [
                    {
                        "name": "blue-in-cluster",
                        "context": {"cluster": "in-cluster", "user": "service-account"},
                    }
                ],
                "current-context": "blue-in-cluster",
            }
            cfg.update(
                kubeconfig=write("config.json", json.dumps(data)),
                context="blue-in-cluster",
            )
        argv = [cfg.get("kubectl", "kubectl")]
        for key in ("kubeconfig", "context"):
            if cfg.get(key):
                argv.extend([f"--{key}", cfg[key]])
        argv.extend(
            [f"--request-timeout={cfg.get('request_timeout_seconds', 30)}s", *args]
        )
        timeout = timeout_ms if timeout_ms is not None else cfg.get("timeout_ms", 45000)
        result = (
            await _call(cfg["runner"], argv, {}, timeout)
            if "runner" in cfg
            else await runtime.exec(argv, timeout_ms=timeout)
        )
        code = result["exit"] if isinstance(result, dict) else result.exit
        out = result["out"] if isinstance(result, dict) else result.out
        if code != 0:
            error = RuntimeError("Kubernetes request failed")
            error.exit, error.operation = code, args[0] if args else None
            raise error
        if raw:
            return out
        if not out.strip():
            return None
        try:
            return json.loads(out)
        except ValueError:
            raise ValueError("Invalid Kubernetes JSON response") from None


def kubectl_client(config=None, **kwargs):
    config = {**(config or {}), **kwargs}
    context = config.get("context")
    if (
        not (isinstance(context, str) and context.strip())
        and config.get("in_cluster") is not True
    ):
        raise ValueError(
            "An explicit Kubernetes context or in_cluster true is required"
        )

    def resource_type(d):
        return f"{d['plural']}.{d['group']}"

    async def list_resources(d, namespace):
        return (
            await request(
                config, ["get", resource_type(d), "-n", namespace, "-o", "json"]
            )
        )["items"]

    async def get_resource(d, namespace, name):
        return await request(
            config,
            [
                "get",
                resource_type(d),
                name,
                "-n",
                namespace,
                "--ignore-not-found",
                "-o",
                "json",
            ],
        )

    async def update_status(d, r, status):
        meta = r["metadata"]
        path = f"/apis/{d['group']}/{d['version']}/namespaces/{meta['namespace']}/{d['plural']}/{meta['name']}/status"
        data = dict(
            apiVersion=r["apiVersion"],
            kind=r["kind"],
            metadata={
                k: meta[k]
                for k in ("name", "namespace", "uid", "resourceVersion")
                if k in meta
            },
            status=status,
        )
        return await request(
            config, ["replace", "--raw", path, "-f", "-"], input=json.dumps(data)
        )

    async def update_finalizers(d, r, finalizers):
        meta = r["metadata"]
        patch = dict(
            metadata=dict(
                resourceVersion=meta["resourceVersion"], finalizers=list(finalizers)
            )
        )
        return await request(
            config,
            [
                "patch",
                resource_type(d),
                meta["name"],
                "-n",
                meta["namespace"],
                "--type=merge",
                "-p",
                json.dumps(patch),
                "-o",
                "json",
            ],
        )

    return {
        **config,
        "list_resources": list_resources,
        "get_resource": get_resource,
        "update_status": update_status,
        "update_finalizers": update_finalizers,
    }
