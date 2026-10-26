"""The prompts and the interpreter used for the logged runs must stay
byte-identical in the default configuration, so that the committed logs keep
replaying and the paper's numbers keep meaning what they meant.

The hashes are of the strings/source as they were in the released commit
3742fa8 (before this revision); see review/code_changes.md.
"""
import hashlib
import inspect

from catsec import composer, kernel, planner

ORIGINAL_SYSTEM_TEMPLATE_SHA256 = "122d024001ce743b13816d42e6390bf9f7e010abe00c99a406af58e2d373fb1c"
ORIGINAL_REPAIR_TEMPLATE_SHA256 = "196401083e4c9b1fc5bfd5c3ca4ca638117da10fb9ea4d3b8f0fe18f50e6a830"
COMPOSER_SYSTEM_SHA256 = "a563eec0d6cf8b333cd3d550f7402c840b0ad9e0981eed68f7a4ecdedeb9951d"
INTERPRETER_RUN_SHA256 = "7bda47626546375786468fdf3dc20aaeeccbf13b0f2b4d10ad11d78366bf790b"


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_original_planner_prompt_unchanged():
    assert _h(planner.SYSTEM_TEMPLATE) == ORIGINAL_SYSTEM_TEMPLATE_SHA256


def test_original_repair_prompt_unchanged():
    assert _h(planner.REPAIR_TEMPLATE) == ORIGINAL_REPAIR_TEMPLATE_SHA256


def test_composer_prompt_unchanged():
    assert _h(composer.COMPOSER_SYSTEM) == COMPOSER_SYSTEM_SHA256


def test_interpreter_run_unchanged():
    assert _h(inspect.getsource(kernel.Interpreter.run)) == INTERPRETER_RUN_SHA256
