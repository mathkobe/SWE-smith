from swesmith.bug_gen.off_by_one.off_by_one import OffByOneModifier
from swesmith.bug_gen.procedural.base import ProceduralModifier

MODIFIERS_PYTHON: list[ProceduralModifier] = [
    OffByOneModifier(likelihood=0.25)
]