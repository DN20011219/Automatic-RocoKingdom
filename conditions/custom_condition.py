"""自定义条件扩展点 — 用户可在此添加自定义条件类型。

使用方式：
1. 继承 ConditionBase
2. 实现 evaluate() 方法
3. 添加 @ConditionRegistry.register("my_condition") 装饰器

示例：
    @ConditionRegistry.register("my_custom")
    class MyCustomCondition(ConditionBase):
        def evaluate(self, context) -> bool:
            # 自定义逻辑
            return context.get_counter("my_counter") > 10
"""

from __future__ import annotations

from typing import Any, Dict

from conditions.base import ConditionBase, ConditionRegistry


# 在此处添加自定义条件类型
# 示例：
# @ConditionRegistry.register("example")
# class ExampleCondition(ConditionBase):
#     def evaluate(self, context: Any) -> bool:
#         return True
