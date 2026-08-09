import os
from pathlib import Path

import pytest

from blue.scaffold import content_spec, render_template, scaffold
from blue.workflow import StepError

RES = Path(__file__).parent / "resources"

# the package owns its templates: content travels with the code
hello = (RES / "bluetest/hello.txt").read_text()
ansible_hello = (RES / "bluetest/ansible-hello.yml").read_text()


def test_scaffold_create_and_delete(tmp_path):
    dir = str(tmp_path)
    specs = [
        {
            "template": {"name": "bluetest/hello.txt", "content": hello},
            "target": f"{dir}/{{{{who}}}}/hello.txt",
            "data": {"who": "world", "name": "blue"},
        }
    ]
    created = scaffold({"blue/event": "create"}, specs)
    # create renders the template into the rendered target
    assert created["blue/exit"] == 0
    assert created["blue.scaffold/written"] == [f"{dir}/world/hello.txt"]
    assert Path(f"{dir}/world/hello.txt").read_text() == "Hello blue!\n"

    # create is idempotent
    scaffold({"blue/event": "create"}, specs)
    assert Path(f"{dir}/world/hello.txt").read_text() == "Hello blue!\n"

    # delete removes targets and prunes emptied directories
    deleted = scaffold({"blue/event": "delete"}, specs)
    assert deleted["blue/exit"] == 0
    assert not os.path.exists(f"{dir}/world/hello.txt")
    assert not os.path.exists(f"{dir}/world")


def test_custom_delimiters_pass_jinja2_through(tmp_path):
    dir = str(tmp_path)
    specs = [
        {
            "template": {"name": "bluetest/ansible-hello.yml", "content": ansible_hello},
            "target": f"{dir}/play.yml",
            "data": {"group": "web"},
            "opts": {"tag_open": "<", "tag_close": ">", "filter_open": "{", "filter_close": "}"},
        }
    ]
    created = scaffold({"blue/event": "create"}, specs)
    assert created["blue/exit"] == 0
    content = Path(f"{dir}/play.yml").read_text()
    # Selmer-style <{group}> is rendered
    assert "hosts: web" in content
    # Jinja2 {{ }} passes through unchanged
    assert "{{ ansible_var }}" in content


def test_renderer_supports_escaping_safe_loops_sort_and_missing_values():
    template = {
        "name": "inline",
        "content": (
            "{{missing}}"
            "{{unsafe}}|{{unsafe|safe}}\n"
            "{% for s in servers | sort(attribute='id') %}{{s.id}}={{s.name}};{% endfor %}"
        ),
    }
    assert (
        render_template(
            template,
            {
                "unsafe": "<tag>&",
                "servers": [
                    {"id": 2, "name": "b"},
                    {"id": 1, "name": "a"},
                ],
            },
        )
        == "&lt;tag&gt;&amp;|<tag>&\n1=a;2=b;"
    )


def test_renderer_supports_if_and_not_empty_with_custom_delimiters():
    template = {
        "name": "inline-if",
        "content": "<% if value|not-empty %><{ value }><% else %>none<% endif %>",
    }
    opts = {"tag_open": "<", "tag_close": ">", "filter_open": "{", "filter_close": "}"}
    assert render_template(template, {"value": "yes"}, opts) == "yes"
    assert render_template(template, {"value": ""}, opts) == "none"


def test_direct_content_is_written_exactly(tmp_path):
    target = tmp_path / "raw.txt"
    scaffold({"blue/event": "create"}, [content_spec(str(target), "{{ untouched }}\n")])
    assert target.read_text() == "{{ untouched }}\n"


def test_a_template_without_content_throws_with_context():
    with pytest.raises(StepError, match="template not found"):
        scaffold(
            {"blue/event": "create"},
            [
                {
                    "template": {"name": "nope/missing.txt", "content": None},
                    "target": "/tmp/x",
                    "data": {},
                }
            ],
        )
