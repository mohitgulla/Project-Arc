"""Safe evaluator for trigger conditions in ``config/routines.yaml``.

Conditions are tiny boolean expressions such as
``new_candidates > 0 and session == 'open'``. They are parsed with :mod:`ast`
and only a whitelist of nodes is allowed: names, literals, comparisons,
``and``/``or``/``not`` and unary minus. No calls, attributes, subscripts or
``eval``. Unknown names evaluate to ``None`` so a condition on a metric a job
did not report is simply false (``None > 0`` is treated as false).
"""

from __future__ import annotations

import ast
import operator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


class ConditionError(ValueError):
    """A trigger condition is not a supported expression."""


def parse_condition(expr: str) -> ast.Expression:
    """Parse and whitelist-check *expr*; raise :class:`ConditionError` if unsafe."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        msg = f"invalid condition {expr!r}: {exc.msg}"
        raise ConditionError(msg) from None
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (
                ast.Expression,
                ast.BoolOp,
                ast.And,
                ast.Or,
                ast.UnaryOp,
                ast.Not,
                ast.USub,
                ast.Compare,
                ast.Name,
                ast.Load,
                ast.Constant,
                ast.Tuple,
                ast.List,
                *_CMP,
            ),
        ):
            msg = f"unsupported syntax {type(node).__name__} in condition {expr!r}"
            raise ConditionError(msg)
    return tree


def _eval(node: ast.AST, env: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_eval(e, env) for e in node.elts)
    if isinstance(node, ast.BoolOp):
        values = (_eval(v, env) for v in node.values)
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        val = _eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not val
        return -val if val is not None else None
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, env)
            try:
                ok = _CMP[type(op)](left, right)
            except TypeError:  # e.g. None > 0 when a metric is missing
                return False
            if not ok:
                return False
            left = right
        return True
    msg = f"unsupported node {type(node).__name__}"  # pragma: no cover - parse whitelists
    raise ConditionError(msg)


def evaluate_condition(expr: str | None, env: Mapping[str, Any]) -> bool:
    """Evaluate *expr* against *env*. ``None``/empty means always true."""
    if not expr:
        return True
    return bool(_eval(parse_condition(expr), env))
