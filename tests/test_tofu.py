# Backend advices need no tofu binary — they only write backend.tf.json.
import json
from pathlib import Path

from blue.runtime import ExecResult, runtime
from blue.tofu import (
    backend_advice,
    backends,
    construct,
    constructs_json,
    gcs_backend_advice,
    hcl_list,
    hcl_map,
    local_backend_advice,
    r2_backend_advice,
    s3_backend_advice,
    tofu_step,
)


def backend_config(dir):
    return json.loads(Path(f"{dir}/backend.tf.json").read_text())


async def test_tofu_commands_receive_the_configured_environment():
    original = runtime.exec
    calls = []

    async def fake(cmd, **opts):
        calls.append((cmd, opts))
        return ExecResult(exit=0, out="{}" if "output" in cmd else "", err="")

    runtime.exec = fake
    try:
        result = await tofu_step(
            {"blue/event": "create"}, dir="/tmp/tofu", env={"TOKEN": "secret"}
        )
        assert result["blue/exit"] == 0
        assert len(calls) == 3
        assert all(opts["env"]["TOKEN"] == "secret" for _cmd, opts in calls)
    finally:
        runtime.exec = original


def test_local_backend_is_the_default(tmp_path):
    dir = str(tmp_path)
    advice = local_backend_advice(lambda _o: dir)
    opts = {"x": 1}
    # before-advice passes opts through
    assert advice(opts) == opts
    assert backend_config(dir) == {"terraform": {"backend": {"local": {}}}}


def test_s3_backend_advice_writes_attributes(tmp_path):
    dir = str(tmp_path)
    s3_backend_advice(
        lambda _o: dir,
        {
            "bucket": "my-state",
            "key": "blue/node-1.tfstate",
            "region": "eu-west-1",
            "encrypt": True,
        },
    )({})
    assert backend_config(dir) == {
        "terraform": {
            "backend": {
                "s3": {
                    "bucket": "my-state",
                    "encrypt": True,
                    "key": "blue/node-1.tfstate",
                    "region": "eu-west-1",
                }
            }
        }
    }


def test_backend_config_retains_native_json_shapes(tmp_path):
    dir = str(tmp_path)
    backend_advice(
        lambda _o: dir,
        "test",
        {
            "enabled": True,
            "retries": 3,
            "nested": {"endpoint": "https://example.test"},
            "items": ["one", "two"],
            "unset": None,
        },
    )({})
    assert backend_config(dir) == {
        "terraform": {
            "backend": {
                "test": {
                    "enabled": True,
                    "retries": 3,
                    "nested": {"endpoint": "https://example.test"},
                    "items": ["one", "two"],
                    "unset": None,
                }
            }
        }
    }


def test_backend_config_can_be_a_function_of_opts(tmp_path):
    dir = str(tmp_path)
    gcs_backend_advice(lambda _o: dir, lambda opts: {"bucket": "state", "prefix": f"blue/{opts['node']}"})(
        {"node": "n1"}
    )
    assert backend_config(dir) == {
        "terraform": {"backend": {"gcs": {"bucket": "state", "prefix": "blue/n1"}}}
    }


def test_r2_backend_and_backend_selection(tmp_path):
    dir = str(tmp_path)
    advice = backends(
        lambda opts: opts["backend"],
        {
            "r2": r2_backend_advice(
                lambda _opts: dir,
                {
                    "bucket": "state",
                    "key": "production/dns.tfstate",
                    "endpoint": "https://acct.r2.cloudflarestorage.com",
                },
            )
        },
    )
    advice({"backend": "r2"})
    config = backend_config(dir)["terraform"]["backend"]["s3"]
    assert config["bucket"] == "state"
    assert config["region"] == "auto"
    assert config["endpoints"] == {"s3": "https://acct.r2.cloudflarestorage.com"}
    assert config["skip_credentials_validation"] is True


def test_hcl_and_constructs_are_deterministic_and_green_byte_compatible():
    assert hcl_list(["example.com", "example.net"]) == '["example.com", "example.net"]'
    assert hcl_map({"b": "2", "a": "1"}) == '{\n    "a" : "1",\n    "b" : "2"\n  }'
    a = construct("resource", "dns_record", "once.tools/b", {"ttl": 1, "name": "b"})
    b = construct("resource", "dns_record", "once.tools/a", {"name": "a", "ttl": 1})
    assert constructs_json([a, b]) == constructs_json([b, a])
    assert constructs_json([]) == "{ }"
