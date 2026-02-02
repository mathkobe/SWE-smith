"""
Purpose: Given a repository, generate bug patches for assert statements using LLM.
The asserts will have semantically equivalent but buggy conditions.

Usage:
python -m swesmith.bug_gen.rewrite_assert.rewrite_assert \
    --model <model> \
    --config_file <config_file> \
    repo

Example:
python -m swesmith.bug_gen.rewrite_assert.rewrite_assert Instagram__MonkeyType.70c3acf6 \
    --config_file configs/bug_gen/lm_rewrite_asserts.yml \
    --model claude-3-7-sonnet-20250219 \
    --max_bugs 50
"""

import argparse
import ast
import json
import logging
import os
import random
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import jinja2
import litellm
import yaml
from dotenv import load_dotenv
from litellm import completion
from litellm.cost_calculator import completion_cost
from tqdm.auto import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from swesmith.bug_gen.llm.utils import PROMPT_KEYS, extract_code_block
from swesmith.bug_gen.utils import get_patch
from swesmith.constants import (
    LOG_DIR_BUG_GEN,
    PREFIX_BUG,
    PREFIX_METADATA,
    BugRewrite,
)
from swesmith.profiles import registry


# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------

load_dotenv(dotenv_path=os.getenv("SWEFT_DOTENV_PATH"))

logging.getLogger("LiteLLM").setLevel(logging.WARNING)
litellm.drop_params = True
litellm.suppress_debug_info = True
litellm.modify_params = True  # Auto-add dummy user message if needed

random.seed(24)

LM_REWRITE_ASSERT = "lm_rewrite_assert"

# Default prompts if config is empty
DEFAULT_SYSTEM_PROMPT = """You are an expert Python developer creating buggy assert statements for testing.
Your goal is to rewrite assert statements to introduce subtle bugs while keeping them syntactically valid."""

DEFAULT_USER_PROMPT = """Original assert: {{ assert_original_line }}

Generate a buggy version that:
1. Is syntactically valid
2. Has a subtle logical error
3. Would fail when code is correct

Format:
Brief explanation.

```python
assert buggy_condition
```"""


# -----------------------------------------------------------------------------
# Assert extraction
# -----------------------------------------------------------------------------

def extract_asserts_from_file(file_path: str) -> list[dict[str, Any]]:
    """Extract assert statements from a Python file."""
    try:
        with open(file_path, "r") as f:
            content = f.read()
        tree = ast.parse(content)
    except Exception:
        return []

    lines = content.split("\n")
    asserts = []

    class AssertVisitor(ast.NodeVisitor):
        def visit_Assert(self, node: ast.Assert):
            line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""

            asserts.append(
                {
                    "lineno": node.lineno,
                    "test": ast.unparse(node.test),
                    "msg": ast.unparse(node.msg) if node.msg else None,
                    "original_line": line.strip(),
                    "node": node,
                }
            )
            self.generic_visit(node)

    AssertVisitor().visit(tree)
    return asserts


# -----------------------------------------------------------------------------
# LLM rewrite
# -----------------------------------------------------------------------------

def gen_buggy_assert_from_lm(
    assert_stmt: dict,
    file_content: str,
    configs: dict,
    model: str,
) -> BugRewrite | None:
    """Use LM to generate buggy assert."""

    def format_prompt(prompt: str | None):
        if not prompt:
            return ""

        # Prepare template variables
        template_vars = {
            "assert_test": assert_stmt["test"],
            "assert_message": assert_stmt["msg"] or "",
            "assert_original_line": assert_stmt["original_line"],
            "file_content": file_content,
            **configs.get("parameters", {}),
        }
        
        try:
            # First try Python format strings (e.g., {assert_test})
            if '{' in prompt and '}' in prompt and '{{' not in prompt:
                return prompt.format(**template_vars)
        except (KeyError, ValueError):
            pass
        
        try:
            # Then try Jinja2 templates (e.g., {{ assert_test }})
            env = jinja2.Environment()
            template = env.from_string(prompt)
            return template.render(**template_vars)
        except Exception as e:
            print(f"⚠ Template rendering failed: {e}")
            return prompt

    # Build messages from config
    # Support two formats:
    # 1. system/user format
    # 2. reasoning/demonstration/instance format (combined into user)
    
    system_content = format_prompt(configs.get("system") or DEFAULT_SYSTEM_PROMPT)
    
    # Check if using old format with reasoning/demonstration/instance
    if not configs.get("user") and any(k in configs for k in ["reasoning", "demonstration", "instance"]):
        # Combine into user message
        user_parts = []
        if configs.get("reasoning"):
            user_parts.append(format_prompt(configs["reasoning"]))
        if configs.get("demonstration"):
            user_parts.append(format_prompt(configs["demonstration"]))
        if configs.get("instance"):
            user_parts.append(format_prompt(configs["instance"]))
        user_content = "\n\n".join(user_parts)
    else:
        user_content = format_prompt(configs.get("user") or DEFAULT_USER_PROMPT)
    
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    messages = [m for m in messages if m["content"]]
    if not messages:
        print(f"⚠ No messages to send for line {assert_stmt['lineno']}")
        return None
    
    # Debug: print first message to verify
    if not hasattr(gen_buggy_assert_from_lm, '_debug_printed'):
        print(f"Debug: First message has {len(messages)} parts")
        print(f"System prompt length: {len(messages[0]['content']) if messages else 0}")
        gen_buggy_assert_from_lm._debug_printed = True

    try:
        response = completion(model=model, messages=messages, temperature=0)
    except Exception as e:
        # Log detailed error information
        error_msg = f"LLM API Error at {assert_stmt['file_path']}:{assert_stmt['lineno']}\n"
        error_msg += f"Error type: {type(e).__name__}\n"
        error_msg += f"Error message: {str(e)}\n"
        error_msg += f"Model: {model}\n"
        error_msg += f"Assert: {assert_stmt['original_line']}\n"
        print(error_msg)
        
        # Save error to log file
        error_log = Path("logs/bug_gen/llm_errors.log")
        error_log.parent.mkdir(parents=True, exist_ok=True)
        with open(error_log, "a") as f:
            f.write(f"\n{'='*70}\n")
            f.write(f"Timestamp: {__import__('datetime').datetime.now().isoformat()}\n")
            f.write(error_msg)
            f.write(f"{'='*70}\n")
        
        return None

    message = response.choices[0].message
    
    # Check if response has content
    if not message.content:
        print(f"⚠ Empty response for {assert_stmt['file_path']}:{assert_stmt['lineno']}")
        return None
    
    code_block = extract_code_block(message.content)
    if not code_block:
        # Log when code extraction fails
        print(f"⚠ No code block found in response for {assert_stmt['file_path']}:{assert_stmt['lineno']}")
        print(f"Response preview: {message.content[:200]}...")
        
        # Save problematic response
        error_log = Path("logs/bug_gen/extraction_failures.log")
        error_log.parent.mkdir(parents=True, exist_ok=True)
        with open(error_log, "a") as f:
            f.write(f"\n{'='*70}\n")
            f.write(f"File: {assert_stmt['file_path']}:{assert_stmt['lineno']}\n")
            f.write(f"Assert: {assert_stmt['original_line']}\n")
            f.write(f"Response:\n{message.content}\n")
            f.write(f"{'='*70}\n")
        
        return None

    explanation = message.content.split("```", 1)[0].strip()

    return BugRewrite(
        rewrite=code_block,
        explanation=explanation,
        strategy=LM_REWRITE_ASSERT,
        cost=completion_cost(response),
        output=message.content,
    )


# -----------------------------------------------------------------------------
# Replace assert
# -----------------------------------------------------------------------------

def replace_assert_in_file(file_path: str, lineno: int, new_assert: str):
    with open(file_path) as f:
        lines = f.readlines()

    indent = len(lines[lineno - 1]) - len(lines[lineno - 1].lstrip())
    lines[lineno - 1] = " " * indent + new_assert + "\n"

    with open(file_path, "w") as f:
        f.writelines(lines)


# -----------------------------------------------------------------------------
# Save patch files (similar to procedural generation)
# -----------------------------------------------------------------------------

def save_bug_patch(
    log_dir: Path,
    assert_stmt: dict,
    rewrite: BugRewrite,
    patch: str,
    repo: str
) -> bool:
    """
    Save bug patch and metadata files.
    
    Structure:
        logs/bug_gen/<repo>/asserts/<file_path>/
            ├── metadata__lm_rewrite_assert__<hash>.json
            └── bug__lm_rewrite_assert__<hash>.diff
    """
    # Create subdirectory based on file path
    file_path = assert_stmt["file_path"]
    relative_path = Path(file_path).relative_to(repo)
    
    # Create entity directory (similar to procedural generation)
    entity_dir = log_dir / relative_path.parent / relative_path.stem
    entity_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate unique identifier
    uuid_str = f"{LM_REWRITE_ASSERT}__{rewrite.get_hash()}"
    
    # Save metadata
    metadata_path = entity_dir / f"{PREFIX_METADATA}__{uuid_str}.json"
    with open(metadata_path, "w") as f:
        json.dump(rewrite.to_dict(), f, indent=2)
    
    # Save patch
    bug_path = entity_dir / f"{PREFIX_BUG}__{uuid_str}.diff"
    with open(bug_path, "w") as f:
        f.write(patch)
    
    return True


# -----------------------------------------------------------------------------
# Main logic
# -----------------------------------------------------------------------------

def main(
    repo: str,
    config_file: str,
    model: str,
    n_workers: int = 1,
    redo_existing: bool = False,
    max_bugs: int | None = None,
):
    configs = yaml.safe_load(open(config_file))

    rp = registry.get(repo)
    rp.clone()

    python_files = []
    for root, _, files in os.walk(repo):
        python_files += [
            os.path.join(root, f) for f in files if f.endswith(".py")
        ]

    all_asserts = []
    for file_path in python_files:
        for a in extract_asserts_from_file(file_path):
            a["file_path"] = file_path
            all_asserts.append(a)

    if max_bugs:
        random.shuffle(all_asserts)
        all_asserts = all_asserts[:max_bugs]

    log_dir = LOG_DIR_BUG_GEN / repo / "asserts"
    log_dir.mkdir(parents=True, exist_ok=True)

    stats = {
        "cost": 0.0,
        "n_bugs_generated": 0,
        "n_generation_failed": 0,
        "n_patch_save_failed": 0
    }

    def process(assert_stmt):
        try:
            file_path = assert_stmt["file_path"]
            lineno = assert_stmt["lineno"]

            content = open(file_path).read()

            # Generate buggy rewrite
            rewrite = gen_buggy_assert_from_lm(assert_stmt, content, configs, model)
            if not rewrite:
                return {"n_generation_failed": 1, "cost": 0, "error": "LLM generation failed"}

            # Apply rewrite
            replace_assert_in_file(file_path, lineno, rewrite.rewrite)

            # Get patch
            subprocess.run(f"cd {repo}; git add -A", shell=True, capture_output=True)
            patch = get_patch(repo, reset_changes=True)

            if not patch:
                error_msg = f"No patch generated for {file_path}:{lineno}"
                print(f"⚠ {error_msg}")
                return {"cost": rewrite.cost, "n_patch_save_failed": 1, "error": error_msg}

            # Save patch and metadata (like procedural generation)
            success = save_bug_patch(log_dir, assert_stmt, rewrite, patch, repo)
            
            if success:
                return {"n_bugs_generated": 1, "cost": rewrite.cost}
            else:
                error_msg = f"Failed to save patch for {file_path}:{lineno}"
                print(f"⚠ {error_msg}")
                return {"cost": rewrite.cost, "n_patch_save_failed": 1, "error": error_msg}

        except Exception as e:
            error_msg = f"Exception processing {assert_stmt.get('file_path', 'unknown')}:{assert_stmt.get('lineno', '?')}"
            print(f"❌ {error_msg}: {type(e).__name__}: {str(e)}")
            
            # Log exception
            error_log = Path("logs/bug_gen/processing_errors.log")
            error_log.parent.mkdir(parents=True, exist_ok=True)
            with open(error_log, "a") as f:
                import traceback
                f.write(f"\n{'='*70}\n")
                f.write(f"Timestamp: {__import__('datetime').datetime.now().isoformat()}\n")
                f.write(f"{error_msg}\n")
                f.write(f"Exception: {type(e).__name__}: {str(e)}\n")
                f.write(f"Traceback:\n{traceback.format_exc()}\n")
                f.write(f"{'='*70}\n")
            
            return {"n_generation_failed": 1, "cost": 0, "error": str(e)}

    # Process with thread pool
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        results = list(
            tqdm(
                ex.map(process, all_asserts),
                total=len(all_asserts),
                desc="Assert statements",
            )
        )
    
    # Aggregate results
    for result in results:
        for key in stats:
            stats[key] += result.get(key, 0)

    # Print summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"Total asserts processed: {len(all_asserts)}")
    print(f"Bugs generated: {stats['n_bugs_generated']}")
    print(f"Generation failed: {stats['n_generation_failed']}")
    print(f"Patch save failed: {stats['n_patch_save_failed']}")
    print(f"Total cost: ${stats['cost']:.2f}")
    print(f"\nOutput directory: {log_dir}")
    print("="*70)

    # Cleanup
    shutil.rmtree(repo)
    
    print(f"\n✓ Generated {stats['n_bugs_generated']} bugs for {repo}")
    print(f"Next step: Run aggregate_patches.py to create all_patches.json")


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate buggy assert rewrites using LLM"
    )
    parser.add_argument("repo", help="Repository name")
    parser.add_argument("-c", "--config_file", required=True, help="Config YAML file")
    parser.add_argument("--model", required=True, help="LLM model name")
    parser.add_argument("-w", "--n_workers", type=int, default=1, help="Number of workers")
    parser.add_argument("--redo_existing", action="store_true", help="Redo existing bugs")
    parser.add_argument("-m", "--max_bugs", type=int, help="Maximum number of bugs to generate")

    main(**vars(parser.parse_args()))