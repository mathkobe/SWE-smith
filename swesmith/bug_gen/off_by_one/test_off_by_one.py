import pytest

from swesmith.bug_gen.off_by_one import OffByOneModifier
from swesmith.constants import CodeEntity


class MockCodeEntity(CodeEntity):

    @property
    def name(self):
        return "test_function"

    @property
    def signature(self):
        return "def test_function():"

    @property
    def stub(self):
        return "def test_function(): pass"


def run_modifier(code: str):
    entity = MockCodeEntity(
        file_path="test.py",
        indent_level=0,
        indent_size=4,
        line_end=100,
        line_start=1,
        node=None,
        src_code=code,
    )

    modifier = OffByOneModifier()
    modifier.flip = lambda: True  # deterministic

    return modifier.modify(entity).rewrite


def test_range():
    result = run_modifier("""
def f(n):
    for i in range(n):
        pass
""")
    assert "range(n - 1)" in result


def test_gte():
    result = run_modifier("""
def f(x,y):
    if x >= y:
        pass
""")
    assert ">=" not in result
    assert ">" in result
