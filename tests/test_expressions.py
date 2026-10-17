"""Offline tests for the safe workflow predicate evaluator (v1.3)."""

import pytest

from src import expressions
from src.expressions import ExpressionError, MISSING, compile_predicate, evaluate

SCOPE = {
    "context": {"department": "R&D", "employee_name": "Ada", "requester_id": 3},
    "output": {"user_id": "u-42", "temporary": None, "count": 2},
    "step": {"create_user_account": {"status": "completed"}},
}


def test_equality_and_inequality():
    assert evaluate("context.department == 'R&D'", SCOPE) is True
    assert evaluate("context.department != 'Sales'", SCOPE) is True
    assert evaluate("context.requester_id == 3", SCOPE) is True
    assert evaluate("context.requester_id == '3'", SCOPE) is False  # no coercion


def test_numeric_ordering_and_type_guards():
    assert evaluate("output.count >= 2", SCOPE) is True
    assert evaluate("output.count < 2", SCOPE) is False
    assert evaluate("context.employee_name > 1", SCOPE) is False  # str vs int


def test_membership():
    assert evaluate("context.department in ['Sales', 'R&D']", SCOPE) is True
    assert evaluate("context.department not in ['Sales']", SCOPE) is True
    assert evaluate("context.department in []", SCOPE) is False


def test_null_checks_treat_missing_as_null():
    assert evaluate("output.temporary is null", SCOPE) is True
    assert evaluate("output.missing_key is null", SCOPE) is True
    assert evaluate("output.user_id is not null", SCOPE) is True
    assert evaluate("output.user_id is null", SCOPE) is False


def test_and_or_precedence_matches_sql():
    # and binds tighter than or: True or (False and False)
    assert evaluate("context.department == 'R&D' or 1 == 2 and 3 == 4", SCOPE) is True
    assert evaluate("1 == 2 and 3 == 4 or context.department == 'R&D'", SCOPE) is True
    assert evaluate("1 == 2 or 3 == 4", SCOPE) is False


def test_not_and_parentheses():
    assert evaluate("not context.department == 'Sales'", SCOPE) is True
    assert evaluate("not (1 == 1 or 2 == 2)", SCOPE) is False
    assert evaluate("(1 == 1 or 2 == 3) and not (3 == 4)", SCOPE) is True


def test_unknown_identifiers_fail_closed():
    assert evaluate("output.nope == 'x'", SCOPE) is False
    assert evaluate("unknown.scope.value is null", SCOPE) is True  # missing reads as null
    assert evaluate("not output.nope == 'x'", SCOPE) is True


def test_dunder_paths_are_plain_lookups():
    # A dict lookup can never reach object internals.
    assert evaluate("output.__class__ is null", SCOPE) is True
    assert evaluate("output.__class__ == 'dict'", SCOPE) is False


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('id')",
        "output.user_id()",
        "output['user_id'] == 'u-42'",
        "sys.exit(0)",
        "open('/etc/passwd')",
        "output.user_id == ",
        "context.department = 'R&D'",
        "'unterminated",
        "",
        "   ",
        "1 == 1) and (2 == 2",
        "output.user_id is",
        "not",
    ],
)
def test_malformed_or_unsafe_sources_raise(source):
    with pytest.raises(ExpressionError):
        compile_predicate(source)


def test_source_length_and_nesting_are_bounded():
    with pytest.raises(ExpressionError, match="longer than"):
        compile_predicate("1 == 1 and " * 100 + "1 == 1")
    deeply_nested = "(" * 40 + "1 == 1" + ")" * 40
    with pytest.raises(ExpressionError, match="nesting"):
        compile_predicate(deeply_nested)


def test_compiled_predicate_is_reusable_and_callable():
    predicate = compile_predicate("output.count > 1")
    assert predicate(SCOPE) is True
    assert predicate({"output": {"count": 0}}) is False
    assert predicate() is False  # empty scope fails closed


def test_evaluate_all_and_describe():
    assert expressions.evaluate_all([], SCOPE) is True
    assert expressions.evaluate_all(["output.count == 2", "context.requester_id == 3"], SCOPE)
    assert not expressions.evaluate_all(["output.count == 2", "context.requester_id == 9"], SCOPE)
    assert expressions.describe(["a == 1", "b == 2"]) == "a == 1 and b == 2"
    assert expressions.describe([]) == ""


def test_missing_sentinel_semantics():
    assert bool(MISSING) is False
    assert repr(MISSING) == "MISSING"


def test_paths_into_non_mappings_are_missing():
    assert evaluate("context.requester_id.deeper == 1", SCOPE) is False
    assert evaluate("context.requester_id.deeper is null", SCOPE) is True


def test_boolean_literals_and_bool_equality_are_strict():
    assert evaluate("true == true", SCOPE) is True
    assert evaluate("false == false", SCOPE) is True
    assert evaluate("true == 1", SCOPE) is False
    assert evaluate("1 == true", SCOPE) is False


def test_no_code_execution_side_effects(tmp_path):
    marker = tmp_path / "pwned"
    payload = f"__import__('pathlib').Path('{marker}').write_text('x')"
    with pytest.raises(ExpressionError):
        compile_predicate(payload)
    assert not marker.exists()
