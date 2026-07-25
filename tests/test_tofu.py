# Backend advices need no tofu binary — they only write backend.tf.json.
import json
from pathlib import Path

from blue.tofu import (
    backend_advice,
    gcs_backend_advice,
    local_backend_advice,
    s3_backend_advice,
)


def backend_config(dir):
    return json.loads(Path(f"{dir}/backend.tf.json").read_text())


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
