import libcst

from swesmith.bug_gen.procedural.base import CommonPMs
from swesmith.bug_gen.procedural.python.base import PythonProceduralModifier
from swesmith.constants import CodeProperty


class OffByOneModifier(PythonProceduralModifier):
    explanation: str = (
        "Off-by-one error introduced: range(n) changed to range(n-1) "
        "or >= changed to >"
    )

    name: str = "func_pm_off_by_one"

    conditions: list = [
        CodeProperty.IS_FUNCTION,
        CodeProperty.HAS_OFF_BY_ONE,
    ]

    class Transformer(PythonProceduralModifier.Transformer):

        def leave_Call(self, original_node, updated_node):
            """Convert range(n) -> range(n-1)"""

            if not self.flip():
                return updated_node

            # only modify range(...)
            if isinstance(updated_node.func, libcst.Name) and updated_node.func.value == "range":
                if len(updated_node.args) == 1:
                    arg = updated_node.args[0].value

                    new_arg = libcst.BinaryOperation(
                        left=arg,
                        operator=libcst.Subtract(),
                        right=libcst.Integer("1"),
                    )

                    return updated_node.with_changes(
                        args=[libcst.Arg(new_arg)]
                    )

            return updated_node

        def leave_ComparisonTarget(self, original_node, updated_node):
            """Convert >= -> >"""

            if not self.flip():
                return updated_node

            if isinstance(updated_node.operator, libcst.GreaterThanEqual):
                return updated_node.with_changes(
                    operator=libcst.GreaterThan()
                )

            return updated_node

