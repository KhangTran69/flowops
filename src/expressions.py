"""Safe predicate expressions for conditional workflow edges (v1.3).

Workflow authors need edges such as ``output.user_id is not null`` or
``context.department in ["Sales", "Support"]``. Evaluating user-authored text
with ``eval`` would be a remote-code-execution hole, so this module ships a
small, hand-written tokenizer and recursive-descent parser instead.

Safety properties (all covered by tests):

* no calls, no indexing, no attribute access beyond a dotted path;
* identifiers must match ``[A-Za-z_][A-Za-z0-9_]*`` per segment;
* a hard cap on source length and nesting depth;
* every path segment is a plain dictionary lookup — never ``getattr``;
* unknown identifiers evaluate to :data:`MISSING` and comparisons against it
  are false (fail closed), so an edge never opens by accident.

Example::

    predicate = compile_predicate("output.user_id is not null and context.department == 'R&D'")
    predicate.evaluate({"output": {"user_id": "u-1"}, "context": {"department": "R&D"}})
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

MAX_SOURCE_LENGTH = 500
MAX_DEPTH = 24

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")

_KEYWORDS = {"and", "or", "not", "in", "is", "null", "true", "false"}


class ExpressionError(ValueError):
    """Raised when a predicate cannot be parsed or uses unsupported syntax."""


class _Missing:
    """Sentinel for an identifier that is absent from the evaluation scope."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()

Scope = Mapping[str, Any]
#: A compiled predicate evaluates a scope and never raises.
Evaluator = Callable[[Scope], Any]


# ---------------------------------------------------------------------------
# Tokenizer


@dataclass(frozen=True)
class Token:
    kind: str  # literal | ident | op | lparen | rparen | lbracket | rbracket | comma | keyword
    value: Any
    position: int


_TWO_CHAR_OPS = {"==", "!=", "<=", ">="}
_ONE_CHAR_OPS = {"<", ">"}


def _tokenize(source: str) -> list[Token]:
    if not isinstance(source, str):
        raise ExpressionError("Predicate must be a string.")
    if len(source) > MAX_SOURCE_LENGTH:
        raise ExpressionError(
            f"Predicate is longer than {MAX_SOURCE_LENGTH} characters."
        )
    tokens: list[Token] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            start = index
            buffer: list[str] = []
            while index < length and source[index] != quote:
                if source[index] == "\\" and index + 1 < length:
                    buffer.append(source[index + 1])
                    index += 2
                    continue
                buffer.append(source[index])
                index += 1
            if index >= length:
                raise ExpressionError(f"Unterminated string starting at {start - 1}.")
            index += 1  # closing quote
            tokens.append(Token("literal", "".join(buffer), start))
            continue
        if char.isdigit() or (
            char == "-" and index + 1 < length and source[index + 1].isdigit()
        ):
            start = index
            index += 1
            while index < length and (source[index].isdigit() or source[index] == "."):
                index += 1
            text = source[start:index]
            try:
                value: Any = float(text) if "." in text else int(text)
            except ValueError as error:  # pragma: no cover - defensive
                raise ExpressionError(f"Invalid number {text!r}.") from error
            tokens.append(Token("literal", value, start))
            continue
        if char.isalpha() or char == "_":
            start = index
            while index < length and (source[index].isalnum() or source[index] in "_."):
                index += 1
            word = source[start:index]
            lowered = word.lower()
            if lowered in _KEYWORDS:
                tokens.append(Token("keyword", lowered, start))
            else:
                tokens.append(Token("ident", word, start))
            continue
        two = source[index : index + 2]
        if two in _TWO_CHAR_OPS:
            tokens.append(Token("op", two, index))
            index += 2
            continue
        if char in _ONE_CHAR_OPS:
            tokens.append(Token("op", char, index))
            index += 1
            continue
        if char == "(":
            tokens.append(Token("lparen", char, index))
            index += 1
            continue
        if char == ")":
            tokens.append(Token("rparen", char, index))
            index += 1
            continue
        if char == "[":
            tokens.append(Token("lbracket", char, index))
            index += 1
            continue
        if char == "]":
            tokens.append(Token("rbracket", char, index))
            index += 1
            continue
        if char == ",":
            tokens.append(Token("comma", char, index))
            index += 1
            continue
        # "=" alone is a common authoring mistake; say so explicitly.
        if char == "=":
            raise ExpressionError(
                f"Single '=' at position {index}; use '==' for comparison."
            )
        raise ExpressionError(f"Unexpected character {char!r} at position {index}.")
    return tokens


# ---------------------------------------------------------------------------
# Parser


@dataclass
class _Parser:
    tokens: Sequence[Token]
    depth: int = 0
    index: int = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> Token:
        token = self.peek()
        if token is None:
            raise ExpressionError("Unexpected end of predicate.")
        self.index += 1
        return token

    def match_keyword(self, word: str) -> bool:
        token = self.peek()
        if token is not None and token.kind == "keyword" and token.value == word:
            self.index += 1
            return True
        return False

    def expect(self, kind: str, value: Any | None = None) -> Token:
        token = self.next()
        if token.kind != kind or (value is not None and token.value != value):
            expected = value if value is not None else kind
            raise ExpressionError(
                f"Expected {expected!r} at position {token.position}, got {token.value!r}."
            )
        return token

    def parse(self) -> Evaluator:
        if not self.tokens:
            raise ExpressionError("Predicate is empty.")
        evaluator = self.parse_or()
        if self.index != len(self.tokens):
            token = self.tokens[self.index]
            raise ExpressionError(
                f"Unexpected token {token.value!r} at position {token.position}."
            )
        return evaluator

    # or := and ('or' and)*
    def parse_or(self) -> Evaluator:
        left = self.parse_and()
        while self.match_keyword("or"):
            right = self.parse_and()
            left = _or(left, right)
        return left

    # and := not ('and' not)*
    def parse_and(self) -> Evaluator:
        left = self.parse_not()
        while self.match_keyword("and"):
            right = self.parse_not()
            left = _and(left, right)
        return left

    def parse_not(self) -> Evaluator:
        if self.match_keyword("not"):
            inner = self.parse_not()
            return lambda scope: not _truthy(inner(scope))
        return self.parse_comparison()

    def parse_comparison(self) -> Evaluator:
        left = self.parse_primary()
        token = self.peek()
        if token is None:
            return left
        if token.kind == "op":
            self.index += 1
            right = self.parse_primary()
            operator = token.value
            return _comparison(operator, left, right)
        if token.kind == "keyword" and token.value in {"in", "is"}:
            self.index += 1
            if token.value == "in":
                return _membership(left, self.parse_list(), negate=False)
            # is [not] null
            negate = self.match_keyword("not")
            self.expect("keyword", "null")
            return _null_check(left, negate)
        if token.kind == "keyword" and token.value == "not":
            # "not in"
            saved = self.index
            self.index += 1
            if self.match_keyword("in"):
                return _membership(left, self.parse_list(), negate=True)
            self.index = saved
            raise ExpressionError(
                f"Unexpected token 'not' at position {token.position}; "
                "did you mean 'not in'?"
            )
        return left

    def parse_list(self) -> list[Evaluator]:
        self.expect("lbracket")
        items: list[Evaluator] = []
        if self.peek() is not None and self.peek().kind == "rbracket":
            self.index += 1
            return items
        while True:
            items.append(self.parse_primary())
            token = self.peek()
            if token is None:
                raise ExpressionError("Unterminated list literal.")
            if token.kind == "comma":
                self.index += 1
                continue
            self.expect("rbracket")
            return items

    def parse_primary(self) -> Evaluator:
        token = self.peek()
        if token is None:
            raise ExpressionError("Unexpected end of predicate.")
        if token.kind == "lparen":
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise ExpressionError("Predicate nesting is too deep.")
            self.index += 1
            inner = self.parse_or()
            self.expect("rparen")
            self.depth -= 1
            return inner
        if token.kind == "literal":
            self.index += 1
            value = token.value
            return lambda scope, value=value: value
        if token.kind == "ident":
            self.index += 1
            path = token.value
            if not _PATH.match(path):
                raise ExpressionError(f"Invalid identifier {path!r}.")
            return _path_lookup(path)
        if token.kind == "keyword" and token.value in {"true", "false", "null"}:
            self.index += 1
            return lambda scope, value=token.value: _keyword_value(token.value)
        raise ExpressionError(
            f"Unexpected token {token.value!r} at position {token.position}."
        )


def _keyword_value(word: str) -> Any:
    return {"true": True, "false": False, "null": None}[word]


def _truthy(value: Any) -> bool:
    return bool(value) if value is not MISSING else False


def _or(left: Evaluator, right: Evaluator) -> Evaluator:
    def evaluate(scope: Scope) -> bool:
        return _truthy(left(scope)) or _truthy(right(scope))

    return evaluate


def _and(left: Evaluator, right: Evaluator) -> Evaluator:
    def evaluate(scope: Scope) -> bool:
        return _truthy(left(scope)) and _truthy(right(scope))

    return evaluate


def _path_lookup(path: str) -> Evaluator:
    segments = path.split(".")

    def evaluate(scope: Scope) -> Any:
        current: Any = scope
        for segment in segments:
            if not isinstance(current, Mapping):
                return MISSING
            current = current.get(segment, MISSING)
            if current is MISSING:
                return MISSING
        return current

    return evaluate


def _comparable(left: Any, right: Any) -> bool:
    if left is MISSING or right is MISSING:
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return True
    return isinstance(left, str) and isinstance(right, str)


def _comparison(operator: str, left: Evaluator, right: Evaluator) -> Evaluator:
    def evaluate(scope: Scope) -> bool:
        lhs = left(scope)
        rhs = right(scope)
        if operator == "==":
            if lhs is MISSING or rhs is MISSING:
                return False
            return lhs == rhs and type(lhs) is type(rhs) or (
                isinstance(lhs, (int, float))
                and isinstance(rhs, (int, float))
                and not isinstance(lhs, bool)
                and not isinstance(rhs, bool)
                and lhs == rhs
            )
        if operator == "!=":
            if lhs is MISSING or rhs is MISSING:
                return False
            return not (lhs == rhs)
        if not _comparable(lhs, rhs):
            return False
        if operator == "<":
            return lhs < rhs
        if operator == "<=":
            return lhs <= rhs
        if operator == ">":
            return lhs > rhs
        if operator == ">=":
            return lhs >= rhs
        return False

    return evaluate


def _membership(left: Evaluator, items: Sequence[Evaluator], *, negate: bool) -> Evaluator:
    def evaluate(scope: Scope) -> bool:
        lhs = left(scope)
        if lhs is MISSING:
            return False
        values = [item(scope) for item in items]
        present = any(value is not MISSING and _equal(lhs, value) for value in values)
        return (not present) if negate else present

    return evaluate


def _equal(lhs: Any, rhs: Any) -> bool:
    if isinstance(lhs, bool) != isinstance(rhs, bool):
        return False
    if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
        return lhs == rhs
    return lhs == rhs


def _null_check(left: Evaluator, negate: bool) -> Evaluator:
    def evaluate(scope: Scope) -> bool:
        value = left(scope)
        is_null = value is None or value is MISSING
        return (not is_null) if negate else is_null

    return evaluate


# ---------------------------------------------------------------------------
# Public API


@dataclass(frozen=True)
class Predicate:
    """A compiled, reusable edge condition."""

    source: str
    _evaluator: Evaluator = field(repr=False)

    def evaluate(self, scope: Scope | None = None) -> bool:
        """Evaluate against a scope; malformed runtime data fails closed."""
        try:
            return _truthy(self._evaluator(dict(scope or {})))
        except Exception:  # pragma: no cover - defensive, evaluator is total
            return False

    def __call__(self, scope: Scope | None = None) -> bool:
        return self.evaluate(scope)


def compile_predicate(source: str) -> Predicate:
    """Parse ``source`` once and return a reusable predicate."""
    tokens = _tokenize(source)
    evaluator = _Parser(tokens=tokens).parse()
    return Predicate(source=source, _evaluator=evaluator)


def evaluate(source: str, scope: Scope | None = None) -> bool:
    """Convenience wrapper for one-off evaluation."""
    return compile_predicate(source).evaluate(scope)


def evaluate_all(sources: Sequence[str], scope: Scope | None = None) -> bool:
    """True when every predicate holds; an empty list is an open edge."""
    return all(compile_predicate(source).evaluate(scope) for source in sources)


def describe(sources: Sequence[str]) -> str:
    """Short human-readable summary for audit rows."""
    return " and ".join(source.strip() for source in sources) if sources else ""
