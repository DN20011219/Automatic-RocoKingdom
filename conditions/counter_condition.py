"""计数条件 — 计数器/变量比较。"""

from __future__ import annotations

import operator
from typing import Any, Dict

from conditions.base import ConditionBase, ConditionRegistry

# 支持的比较运算符
OPERATORS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


@ConditionRegistry.register("counter")
class CounterCondition(ConditionBase):
    """计数条件：比较计数器值。

    配置示例：
        type: counter
        counter: "quest_done_count"
        operator: ">="
        value: 5
    """

    def evaluate(self, context: Any) -> bool:
        counter_name = self._config.get("counter", "")
        op_str = self._config.get("operator", ">=")
        value = self._config.get("value", 0)

        # 支持变量引用
        value = context.resolve_value(value)
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    return False

        current = context.get_counter(counter_name)
        op_func = OPERATORS.get(op_str)

        if op_func is None:
            return False

        return op_func(current, value)


@ConditionRegistry.register("variable")
class VariableCondition(ConditionBase):
    """变量条件：比较工作流变量值。

    配置示例：
        type: variable
        var_name: "max_retry"
        operator: ">"
        value: 0
    """

    def evaluate(self, context: Any) -> bool:
        var_name = self._config.get("var_name", "")
        op_str = self._config.get("operator", "==")
        value = self._config.get("value")

        current = context.get_variable(var_name)
        if current is None:
            return False

        # 支持变量引用
        value = context.resolve_value(value)

        op_func = OPERATORS.get(op_str)
        if op_func is None:
            return False

        try:
            return op_func(current, value)
        except TypeError:
            return False
