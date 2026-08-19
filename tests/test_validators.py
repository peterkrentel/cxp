import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.run_tests import (
    validate_python, validate_error_handling, validate_k8s_deployment, validate_security,
    validate_infra_yaml, validate_has_tests, validate_readme, validate_decomposition,
)


def test_type_hints_check_actually_requires_annotations():
    no_hints = "def add(a, b):\n    return a + b\n"
    valid, issues = validate_python(no_hints, require_type_hints=True)
    assert valid is False
    assert any("type hint" in i.lower() for i in issues)

    with_hints = "def add(a: int, b: int) -> int:\n    return a + b\n"
    valid, issues = validate_python(with_hints, require_type_hints=True)
    assert valid is True


def test_error_handling_requires_the_named_exceptions():
    missing_one = (
        "def load(path):\n"
        "    try:\n"
        "        return open(path).read()\n"
        "    except FileNotFoundError:\n"
        "        return None\n"
    )
    valid, issues = validate_error_handling(missing_one, ["FileNotFoundError", "JSONDecodeError"])
    assert valid is False
    assert any("JSONDecodeError" in i for i in issues)

    both = missing_one.replace(
        "except FileNotFoundError:",
        "except (FileNotFoundError, __import__('json').JSONDecodeError):",
    )
    valid, issues = validate_error_handling(both, ["FileNotFoundError", "JSONDecodeError"])
    assert valid is True


def test_k8s_deployment_requires_kind_and_resource_limits():
    bare_yaml = "foo: bar\n"
    valid, issues = validate_k8s_deployment(bare_yaml)
    assert valid is False

    real_deployment = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
        - name: api
          resources:
            limits: { cpu: "500m", memory: "512Mi" }
"""
    valid, issues = validate_k8s_deployment(real_deployment)
    assert valid is True

    # Tier 0 doesn't require resources yet -- parameterized, not post-filtered
    no_resources = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"
    valid, issues = validate_k8s_deployment(no_resources)
    assert valid is False
    valid, issues = validate_k8s_deployment(no_resources, require_resources=False)
    assert valid is True


def test_security_check_flags_unsanitized_path_from_input():
    unsafe = (
        "import requests\n"
        "def fetch(url):\n"
        "    return requests.get(url).content\n"
    )
    valid, issues = validate_security(unsafe)
    assert valid is False

    safer = (
        "import requests\n"
        "from urllib.parse import urlparse\n"
        "def fetch(url):\n"
        "    parsed = urlparse(url)\n"
        "    if parsed.scheme not in ('http', 'https'):\n"
        "        raise ValueError('invalid scheme')\n"
        "    return requests.get(url, timeout=10).content\n"
    )
    valid, issues = validate_security(safer)
    assert valid is True


def test_infra_yaml_required_keys_are_parameterized_not_hardcoded():
    minimal = "persistence:\n  enabled: true\nresources:\n  limits: {cpu: '500m'}\n"
    # default call keeps today's 4-key behavior
    valid, issues = validate_infra_yaml(minimal)
    assert valid is False
    assert any("auth" in i for i in issues)

    # Tier 0 asks for fewer keys -- must be able to narrow the requirement,
    # not just strip matching issue strings out after the fact
    valid, issues = validate_infra_yaml(minimal, required_keys=("persistence", "resources"))
    assert valid is True

    # Tier 2 asks for more keys than today's default 4
    harder = minimal + "auth:\n  enabled: true\nsentinel:\n  enabled: true\ntls:\n  enabled: true\nbackup:\n  schedule: '0 * * * *'\n"
    valid, issues = validate_infra_yaml(harder, required_keys=("persistence", "auth", "sentinel", "resources", "tls", "backup"))
    assert valid is True
    valid, issues = validate_infra_yaml(minimal, required_keys=("persistence", "auth", "sentinel", "resources", "tls", "backup"))
    assert valid is False
    assert any("tls" in i for i in issues) and any("backup" in i for i in issues)


def test_has_tests_min_asserts_is_parameterized():
    two_asserts = "def double(n):\n    return n * 2\n\nassert double(2) == 4\nassert double(0) == 0\n"
    valid, issues = validate_has_tests(two_asserts)  # default min_asserts=2, today's behavior
    assert valid is True
    valid, issues = validate_has_tests(two_asserts, min_asserts=5)
    assert valid is False
    assert any("5" in i for i in issues)


def test_decomposition_checks_artifact_content_not_packet_count():
    # Real packet history (5/5 sampled tasks, trivial through complex) shows
    # this planner always spawns exactly one code-type packet -- min_subtasks
    # was checking something that never varies. This checks the one artifact
    # actually covers each distinct piece the goal asked for instead.
    thin = "def add(a, b):\n    return a + b\n"
    valid, issues = validate_decomposition(thin, required_pieces=("def ", "assert"))
    assert valid is False
    assert any("assert" in i for i in issues)

    real = "def add(a, b):\n    return a + b\n\nassert add(2, 3) == 5\n"
    valid, issues = validate_decomposition(real, required_pieces=("def ", "assert"))
    assert valid is True


def test_readme_checks_markdown_sections_not_python_docstring_syntax():
    # validate_has_docstring would fail this every time -- a real README never
    # contains triple-quoted Python docstrings. This is a distinct check.
    bare = "# My Package\n\nA thing that does stuff.\n"
    valid, issues = validate_readme(bare)
    assert valid is False

    real = (
        "# My Package\n\n"
        "## Installation\n\n```\npip install my-package\n```\n\n"
        "## Usage\n\n```python\nimport my_package\n```\n\n"
        "## API Reference\n\n`my_package.run()` -- runs the thing.\n"
    )
    valid, issues = validate_readme(real)
    assert valid is True
