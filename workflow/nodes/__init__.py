"""工作流节点类型集合。"""

from workflow.nodes.base import NodeBase, NodeResult
from workflow.nodes.action_node import ActionNode
from workflow.nodes.perceive_node import PerceiveNode
from workflow.nodes.condition_node import ConditionNode
from workflow.nodes.loop_node import LoopNode
from workflow.nodes.wait_node import WaitNode
from workflow.nodes.scene_node import SceneNode
from workflow.nodes.terminate_node import TerminateNode
from workflow.nodes.log_node import LogNode

__all__ = [
    "NodeBase",
    "NodeResult",
    "ActionNode",
    "PerceiveNode",
    "ConditionNode",
    "LoopNode",
    "WaitNode",
    "SceneNode",
    "TerminateNode",
    "LogNode",
]
