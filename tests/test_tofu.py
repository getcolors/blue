# Backend advices need no tofu binary — they only write backend.tf.
from pathlib import Path

from blue.tofu import gcs_backend_advice, local_backend_advice, s3_backend_advice


def test_local_backend_is_the_default(tmp_path):
    dir = str(tmp_path)
    advice = local_backend_advice(lambda _o: dir)
    opts = {"x": 1}
    # before-advice passes opts through
    assert advice(opts) == opts
    assert Path(f"{dir}/backend.tf").read_text() == 'terraform {\n  backend "local" {\n  }\n}\n'


def test_s3_backend_advice_writes_attributes_sorted(tmp_path):
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
    assert Path(f"{dir}/backend.tf").read_text() == (
        "terraform {\n"
        '  backend "s3" {\n'
        '    bucket = "my-state"\n'
        "    encrypt = true\n"
        '    key = "blue/node-1.tfstate"\n'
        '    region = "eu-west-1"\n'
        "  }\n"
        "}\n"
    )


def test_backend_config_can_be_a_function_of_opts(tmp_path):
    dir = str(tmp_path)
    gcs_backend_advice(lambda _o: dir, lambda opts: {"bucket": "state", "prefix": f"blue/{opts['node']}"})(
        {"node": "n1"}
    )
    assert Path(f"{dir}/backend.tf").read_text() == (
        'terraform {\n  backend "gcs" {\n    bucket = "state"\n    prefix = "blue/n1"\n  }\n}\n'
    )
