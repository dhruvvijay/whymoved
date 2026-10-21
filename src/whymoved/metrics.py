"""Derived metrics from a formula such as `revenue / orders` or `(revenue - cost) / revenue`.

The formula is parsed with Python's `ast` module and checked against a whitelist. It is NEVER
passed to eval(), so a formula typed by a user cannot run code.

What is allowed
    * column names (bare words, or `back-ticked names` when they contain spaces)
    * numbers, + - * and parentheses, and a leading minus
    * division by a plain number (`revenue / 100`)
    * ONE division by an expression that uses columns, at the very top
      (`A / B`): this makes a ratio metric, computed as sum(A) / sum(B)

Why division is limited
    A ratio of totals is not the total of ratios. The engine splits a ratio correctly only
    when it knows the numerator and the denominator, so `revenue / orders` becomes the ratio
    metric (sum of revenue) / (sum of orders). A formula like `(a / b) + (c / d)` or
    `a / b * 100` has no such meaning and is rejected with an explanation.
"""

from __future__ import annotations

import ast
import re

import numpy as np
import pandas as pd

from whymoved.decompose import MetricSpec, additive, ratio

MAX_FORMULA_LENGTH = 200

NAMED_FORMULAS: dict[str, str] = {
    "conversion": "orders / visits",
    "price": "revenue / orders",
    "aov": "revenue / orders",
    "margin": "(revenue - cost) / revenue",
    "cost_ratio": "cost / revenue",
    "gross_profit": "revenue - cost",
}

_TICKS = re.compile(r"`([^`]+)`")
_ALLOWED = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
)


class FormulaError(ValueError):
    """The formula is not allowed or refers to something that does not exist."""


def _has_columns(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) for n in ast.walk(node))


def _is_div(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)


def _parse(expr: str) -> tuple[ast.Expression, dict[str, str]]:
    """Parse a formula; returns (tree, placeholder -> real column name for `ticked` names)."""
    expr = expr.strip()
    if not expr:
        raise FormulaError("the formula is empty")
    if len(expr) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"the formula is longer than {MAX_FORMULA_LENGTH} characters")
    placeholders: dict[str, str] = {}
    by_name: dict[str, str] = {}

    def swap(match: re.Match) -> str:
        name = match.group(1).strip()
        if name not in by_name:
            by_name[name] = f"_col{len(by_name)}_"
            placeholders[by_name[name]] = name
        return by_name[name]

    try:
        tree = ast.parse(_TICKS.sub(swap, expr), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"cannot read the formula {expr!r}: {exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise FormulaError(
                f"{type(node).__name__} is not allowed. Use column names, numbers, "
                "+ - * ( ) and one division."
            )
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise FormulaError(f"only plain numbers are allowed, not {node.value!r}")
    return tree, placeholders


def _evaluate(node: ast.AST, env: dict[str, pd.Series], allow_div: bool) -> pd.Series | float:
    """Row-wise evaluation of an already-validated tree."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, env, allow_div)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, env, allow_div)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Div):
            if _has_columns(node.right):
                raise FormulaError(
                    "division by a column is only allowed once, at the top "
                    "(a ratio such as 'revenue / orders')."
                )
            divisor = _evaluate(node.right, env, allow_div)
            if float(divisor) == 0.0:
                raise FormulaError("division by zero")
            return _evaluate(node.left, env, allow_div) / divisor
        left, right = _evaluate(node.left, env, allow_div), _evaluate(node.right, env, allow_div)
        return left + right if isinstance(node.op, ast.Add) else (
            left - right if isinstance(node.op, ast.Sub) else left * right
        )
    raise FormulaError(f"{type(node).__name__} is not allowed")  # unreachable after _parse


def _safe_name(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_") or "metric"


def _unique(df: pd.DataFrame, base: str) -> str:
    name, i = base, 1
    while name in df.columns:
        i += 1
        name = f"{base}_{i}"
    return name


def build_metric(
    df: pd.DataFrame, formula: str, name: str | None = None
) -> tuple[pd.DataFrame, MetricSpec]:
    """Return (df with helper columns added, MetricSpec) for `formula`.

    The input frame is not modified. A bare column name returns that column as an additive
    metric. A top-level division becomes a ratio metric; anything else is an additive,
    row-wise quantity such as `revenue - cost`.
    """
    named = NAMED_FORMULAS.get(formula.strip().lower())
    if named is not None and name is None:
        name = formula.strip().lower()
    formula = named or formula
    tree, placeholders = _parse(formula)

    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    env: dict[str, pd.Series] = {}
    for ident in sorted(used):
        column = placeholders.get(ident, ident)
        if column not in df.columns:
            shown = ", ".join(map(str, list(df.columns)[:15]))
            raise FormulaError(f"unknown column {column!r}. Columns: {shown}")
        series = df[column]
        if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series):
            raise FormulaError(f"column {column!r} is not numeric")
        env[ident] = series.astype(float)

    label = name or formula.strip()
    base = _safe_name(name or formula)
    body = tree.body

    if isinstance(body, ast.Name):
        return df, additive(label, placeholders.get(body.id, body.id))

    if _is_div(body) and _has_columns(body.right):
        num = _evaluate(body.left, env, allow_div=False)
        den = _evaluate(body.right, env, allow_div=False)
        num = num if isinstance(num, pd.Series) else pd.Series(num, index=df.index)
        if float(den.sum()) == 0.0:
            raise FormulaError("the denominator adds up to zero, so the ratio is undefined")
        out = df.copy()
        num_col, den_col = _unique(out, f"__{base}_num"), _unique(out, f"__{base}_den")
        out[num_col], out[den_col] = num.to_numpy(), den.to_numpy()
        return out, ratio(label, num_col, den_col)

    value = _evaluate(body, env, allow_div=False)
    if not isinstance(value, pd.Series):
        raise FormulaError("the formula uses no columns")
    out = df.copy()
    col = _unique(out, f"__{base}")
    out[col] = np.asarray(value, dtype=float)
    return out, additive(label, col)


def build_budget_metric(
    df: pd.DataFrame, formula: str, mapping: dict[str, str], name: str | None = None
) -> tuple[pd.DataFrame, MetricSpec, dict[str, str]]:
    """Like build_metric, but also applies the formula to the BUDGET columns.

    `mapping` says which budget column goes with each column the formula uses, for example
    {"revenue": "budget_revenue", "cost": "budget_cost"}. Returns (frame, spec, helper_mapping);
    pass helper_mapping as the `mapping` of baseline.explain_budget.
    """
    data, spec = build_metric(df, formula, name)
    expr = NAMED_FORMULAS.get(formula.strip().lower()) or formula
    tree, placeholders = _parse(expr)
    env: dict[str, pd.Series] = {}
    for ident in sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}):
        column = placeholders.get(ident, ident)
        if column not in mapping:
            raise FormulaError(f"no budget column given for {column!r}")
        budget_col = mapping[column]
        if budget_col not in df.columns:
            raise FormulaError(f"budget column {budget_col!r} not found")
        series = df[budget_col]
        if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series):
            raise FormulaError(f"budget column {budget_col!r} is not numeric")
        env[ident] = series.astype(float)

    body = tree.body
    if isinstance(body, ast.Name):  # a plain column: its budget column is used directly
        return data, spec, {spec.numerator: mapping[placeholders.get(body.id, body.id)]}
    if _is_div(body) and _has_columns(body.right):
        num = _evaluate(body.left, env, allow_div=False)
        den = _evaluate(body.right, env, allow_div=False)
        num = num if isinstance(num, pd.Series) else pd.Series(num, index=df.index)
        num_b, den_b = _unique(data, f"{spec.numerator}_budget"), _unique(
            data, f"{spec.denominator}_budget")
        data[num_b], data[den_b] = num.to_numpy(), den.to_numpy()
        return data, spec, {spec.numerator: num_b, spec.denominator: den_b}
    value_b = _unique(data, f"{spec.numerator}_budget")
    data[value_b] = np.asarray(_evaluate(body, env, allow_div=False), dtype=float)
    return data, spec, {spec.numerator: value_b}
