"""A tiny, safe predicate language for workflow conditions.

This module is deliberately small and dependency-free so beginners can read
it end to end.  Conditions are simple boolean expressions over step outputs,
for example::

    ["output.user_id is not null"]
    ["department == 'it-infrastructure'", "seniority >= 5"]

Security rules (important!):

* We NEVER use ``eval``, ``exec``, ``compile`` or ``ast.literal_eval``.
* Only identifiers on a fixed allow-list (``status``, ``requester``,
  ``assignee``, ``priority``, ``ticket_type``, ``department``, ``seniority``,
  plus anything under the ``output.`` namespace) may appear.
* Unknown identifiers do not raise: they resolve to ``None`` and predicates
  that reference them evaluate to False ("fail closed").  Callers record the
  unknown names so they can write an audit-visible reason.

Grammar (EBNF-ish)::

    predicate := or_expr
    or_expr   := and_expr ('or' and_expr)*
    and_expr  := atom ('and' atom)*
    atom      := '(' predicate ')' | comparison
    comparison:= operand (op operand)?
                 | 'defined' operand
                 | operand 'is' ['not'] ('null' | 'defined')
    op        := '==' | '!=' | '<=' | '>=' | '<' | '>'
                 | 'in' | 'contains'
    operand   := string | number | boolean | null | dotted name

Values compare like JSON: numbers with numbers, strings with strings.
Comparing incompatible types is False (never an exception).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Allow-listed root identifiers.  Anything starting with one of these (or any
# dotted name whose first segment is listed) is legal in a predicate.
# --------------------------------------------------------------------------
ALLOWED_ROOTS = frozenset(
    {
        "status",
        "requester",
        "assignee",
        "priority",
        "ticket_type",
        "department",
        "seniority",
        "output",
        "steps",
    }
)

_MISSING = object()  # sentinel meaning "this path was not present"


class ExpressionSyntaxError(ValueError):
    """Raised when a predicate cannot be parsed.  This is a *definition-time*
    error (the author wrote bad syntax); it must never happen at runtime."""


@dataclass
class EvalResult:
    """Outcome of evaluating one predicate."""

    value: bool
    unknown_identifiers: Tuple[str, ...] = ()
    detail: str = ""


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------
_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)
  | (?P<op>==|!=|<=|>=|<|>)
  | (?P<punct>[(),])
    """,
    re.VERBOSE,
)

_KEYWORDS = {"and", "or", "not", "in", "contains", "is", "null", "true",
             "false", "defined"}


def _tokenize(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise ExpressionSyntaxError(
                f"unexpected character {text[pos]!r} at position {pos}"
            )
        pos = match.end()
        kind = match.lastgroup or ""
        value = match.group()
        if kind == "ws":
            continue
        if kind == "name" and value.lower() in _KEYWORDS:
            kind, value = "keyword", value.lower()
        tokens.append((kind, value))
    return tokens


def _unquote(literal: str) -> str:
    body = literal[1:-1]
    return body.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


# --------------------------------------------------------------------------
# Parser -> small AST of nested tuples
# --------------------------------------------------------------------------
class _Parser:
    def __init__(self, tokens: Sequence[Tuple[str, str]]) -> None:
        self.tokens = list(tokens)
        self.pos = 0

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, kind: str, value: Optional[str] = None) -> Tuple[str, str]:
        token = self.peek()
        if token is None or token[0] != kind or (
            value is not None and token[1] != value
        ):
            raise ExpressionSyntaxError(
                f"expected {value or kind}, got {token}"
            )
        self.pos += 1
        return token

    def parse(self) -> tuple:
        node = self.parse_or()
        if self.peek() is not None:
            raise ExpressionSyntaxError(f"trailing tokens near {self.peek()}")
        return node

    def parse_or(self) -> tuple:
        node = self.parse_and()
        while self.peek() == ("keyword", "or"):
            self.pos += 1
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self) -> tuple:
        node = self.parse_atom()
        while self.peek() == ("keyword", "and"):
            self.pos += 1
            node = ("and", node, self.parse_atom())
        return node

    def parse_atom(self) -> tuple:
        token = self.peek()
        if token is None:
            raise ExpressionSyntaxError("predicate ended unexpectedly")
        if token == ("punct", "("):
            self.pos += 1
            node = self.parse_or()
            self.take("punct", ")")
            return node
        if token == ("keyword", "not"):
            self.pos += 1
            return ("not", self.parse_atom())
        if token == ("keyword", "defined"):
            self.pos += 1
            return ("defined", self.parse_operand())
        left = self.parse_operand()
        nxt = self.peek()
        if nxt is None or nxt[0] == "punct":
            return ("present", left)
        if nxt == ("keyword", "is"):
            self.pos += 1
            negate = False
            if self.peek() == ("keyword", "not"):
                self.pos += 1
                negate = True
            if self.peek() == ("keyword", "null"):
                self.pos += 1
                return ("is_null", left, negate)
            if self.peek() == ("keyword", "defined"):
                self.pos += 1
                return ("is_defined", left, negate)
            raise ExpressionSyntaxError(f"expected null/defined after 'is', got {self.peek()}")
        if nxt[0] == "op":
            self.pos += 1
            return ("compare", nxt[1], left, self.parse_operand())
        if nxt == ("keyword", "in"):
            self.pos += 1
            return ("compare", "in", self.parse_operand(), left)
        if nxt == ("keyword", "contains"):
            self.pos += 1
            return ("compare", "contains", left, self.parse_operand())
        raise ExpressionSyntaxError(f"unexpected token {nxt}")

    def parse_operand(self) -> tuple:
        token = self.peek()
        if token is None:
            raise ExpressionSyntaxError("missing operand")
        kind, value = token
        if kind == "string":
            self.pos += 1
            return ("literal", _unquote(value))
        if kind == "number":
            self.pos += 1
            return ("literal", float(value) if "." in value else int(value))
        if kind == "keyword" and value in {"null", "true", "false"}:
            self.pos += 1
            python = {"null": None, "true": True, "false": False}[value]
            return ("literal", python)
        if kind == "name":
            self.pos += 1
            if value.lower() in _KEYWORDS:
                raise ExpressionSyntaxError(f"reserved word used as identifier: {value}")
            return ("name", value)
        raise ExpressionSyntaxError(f"cannot use token {token} as an operand")


def parse(expression: str) -> tuple:
    """Parse a predicate into an AST tuple.  Raises ExpressionSyntaxError."""
    text = (expression or "").strip()
    if not text:
        raise ExpressionSyntaxError("empty predicate")
    return _Parser(_tokenize(text)).parse()


# --------------------------------------------------------------------------
# Evaluation against a context dict
# --------------------------------------------------------------------------
def _resolve(node: tuple, context: Dict[str, Any], unknown: List[str]) -> Any:
    kind = node[0]
    if kind == "literal":
        return node[1]
    if kind == "name":
        path = node[1].split(".")
        root = path[0]
        if root not in ALLOWED_ROOTS:
            if node[1] not in unknown:
                unknown.append(node[1])
            return _MISSING
        current: Any = context
        for index, segment in enumerate(path):
            if isinstance(current, dict) and segment in current:
                current = current[segment]
            else:
                # A missing intermediate segment means the predicate refers to
                # a step/output that does not exist at all (e.g. a typo or a
                # renamed step key).  Record it as an unknown identifier so
                # callers can write an audit-visible reason; evaluation still
                # fails closed (False), never raises.
                if index < len(path) - 1 and node[1] not in unknown:
                    unknown.append(node[1])
                return _MISSING
        return current
    raise ExpressionSyntaxError(f"bad AST node {node!r}")


def _values_comparable(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return True
    if isinstance(left, str) and isinstance(right, str):
        return True
    return False


def _compare(op: str, left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return False
    if op == "==":
        return type(left) is type(right) and left == right if not isinstance(
            left, (int, float)
        ) else left == right
    if op == "!=":
        return not _compare("==", left, right)
    if op in {"<", "<=", ">", ">="}:
        if not _values_comparable(left, right):
            return False
        return {
            "<": lambda: left < right,
            "<=": lambda: left <= right,
            ">": lambda: left > right,
            ">=": lambda: left >= right,
        }[op]()
    if op == "in":
        if isinstance(right, (list, tuple, set)):
            return left in right
        if isinstance(right, str):
            return isinstance(left, str) and left in right
        return False
    if op == "contains":
        if isinstance(left, (list, tuple, set)):
            return right in left
        if isinstance(left, str):
            return isinstance(right, str) and right in left
        if isinstance(left, dict):
            return right in left
        return False
    raise ExpressionSyntaxError(f"unknown operator {op!r}")


def _eval_node(node: tuple, context: Dict[str, Any], unknown: List[str]) -> bool:
    kind = node[0]
    if kind == "or":
        left = _eval_node(node[1], context, unknown)
        right = _eval_node(node[2], context, unknown)
        return left or right
    if kind == "and":
        left = _eval_node(node[1], context, unknown)
        right = _eval_node(node[2], context, unknown)
        return left and right
    if kind == "not":
        return not _eval_node(node[1], context, unknown)
    if kind == "compare":
        return _compare(node[1], _resolve(node[2], context, unknown),
                        _resolve(node[3], context, unknown))
    if kind == "is_null":
        value = _resolve(node[1], context, unknown)
        result = value is None or value is _MISSING
        return not result if node[2] else result
    if kind == "is_defined":
        value = _resolve(node[1], context, unknown)
        result = value is not _MISSING
        return not result if node[2] else result
    if kind == "defined":
        return _resolve(node[1], context, unknown) is not _MISSING
    if kind == "present":
        # Bare operand: truthy check (null/false/missing => False).
        value = _resolve(node[1], context, unknown)
        return value not in (_MISSING, None, False)
    raise ExpressionSyntaxError(f"bad AST node {node!r}")


def evaluate(expression: str, context: Dict[str, Any]) -> EvalResult:
    """Evaluate one predicate string against ``context``.

    Never raises for unknown identifiers; those make the predicate False and
    are reported in ``EvalResult.unknown_identifiers`` so callers can fail
    closed with an audit reason instead of a 500.
    """
    unknown: List[str] = []
    try:
        ast = parse(expression)
    except ExpressionSyntaxError as exc:
        return EvalResult(False, (), f"syntax error: {exc}")
    value = _eval_node(ast, context, unknown)
    detail = ""
    if unknown:
        detail = "unknown identifier(s): " + ", ".join(unknown)
    return EvalResult(value, tuple(unknown), detail)


def evaluate_all(expressions: Sequence[str], context: Dict[str, Any]) -> EvalResult:
    """AND over several predicates; aggregates unknown identifiers."""
    combined_unknown: List[str] = []
    details: List[str] = []
    result = True
    for expression in expressions:
        outcome = evaluate(expression, context)
        result = result and outcome.value
        for name in outcome.unknown_identifiers:
            if name not in combined_unknown:
                combined_unknown.append(name)
        if outcome.detail:
            details.append(f"{expression!r}: {outcome.detail}")
    return EvalResult(result, tuple(combined_unknown), "; ".join(details))
