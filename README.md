# SWE-smith Bug Generation & Validation

## Overview

We extended SWE-smith with two novel bug generation strategies and a local validation framework:

### 1. Bug Generation Methods

#### (1) Procedural Bug Generation: Off-by-One Errors
Automatically generates boundary condition bugs in Python code:
- **Comparison operators**: Changes `>=` to `>`, `<=` to `<`
- **Loop boundaries**: Modifies `range(n)` to `range(n-1)`, `range(n+1)` to `range(n)`
- **Array indexing**: Introduces off-by-one errors in list/array access

**Motivation**: These methods were developed by prompting ChatGPT/Claude to identify bug patterns most likely to improve SWE-bench coverage. Off-by-one errors are among the most common real-world bugs.

#### (2) LLM-Powered Assertion Rewriting
Uses LLMs to rewrite test assertions with subtle logical errors.

**Motivation**: In RL/ML repositories, assertions are critical for:
- Tensor shape validation
- Data-label alignment (especially in RL rollout)
- Gradient flow verification

These assertions act as the primary safeguard against silent failures, making them ideal targets for realistic bug injection.

### 2. Local Validation

We validate bugs locally using Docker images with proper test environments:
```
REPOSITORY                                                    TAG           IMAGE ID      
swebench/swesmith.x86_64.instagram_1776_monkeytype.70c3acf6   with_pytest   e9abbbd9fe20
```

This enables:
- Fast iteration without API calls
- Reproducible validation
- Parallel processing (multi-worker support)

---

## Setup & Usage

### Prerequisites
```bash
# Navigate to project directory
cd /workspaces/SWE-smith

# Activate virtual environment
source vmax_env/bin/activate

# Set API key for LLM-based generation
export ANTHROPIC_API_KEY="sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

### Step 1: Generate Bugs

#### Procedural Off-by-One Bugs
```bash
python -m swesmith.bug_gen.off_by_one.generate \
    --max_bugs 2 \
    Instagram__MonkeyType.70c3acf6
```

#### LLM Assertion Rewriting
```bash
python -m swesmith.bug_gen.rewrite_assert.rewrite_assert \
    Instagram__MonkeyType.70c3acf6 \
    --config_file configs/bug_gen/lm_rewrite_asserts.yml \
    --model claude-haiku-4-5-20251001 \
    --max_bugs 50
```

### Step 2: Aggregate Patches
```bash
python -m swesmith.bug_gen.collect_patches \
    logs/bug_gen/Instagram__MonkeyType.70c3acf6/
```

**Output**: `logs/bug_gen/Instagram__MonkeyType.70c3acf6_all_patches.json`

### Step 3: Local Validation
```bash
python swesmith/local_valid_multiple_process.py \
    logs/bug_gen/Instagram__MonkeyType.70c3acf6_all_patches.json \
    --image swebench/swesmith.x86_64.instagram_1776_monkeytype.70c3acf6:with_pytest \
    --workers 4 \
    --output logs/custom_validation_results
```

---

## Results

Validation results are saved to `logs/custom_validation_results/`:
```
logs/custom_validation_results/
├── summary.json                    # Overall statistics
└── <instance_id>/                  # Per-bug reports
    ├── eval.sh                     # Test commands
    ├── patch.diff                  # Bug patch
    ├── report.json                 # Test results (FAIL_TO_PASS, PASS_TO_FAIL)
    ├── run_instance.log            # Execution log
    └── test_output.txt             # pytest output
```

### Final Example Results
```
======================================================================
SUMMARY
======================================================================
Total patches: 31
Bugs detected (1+ fail-to-pass): 23  (74.2% detection rate)
Bugs not detected (0 fail-to-pass): 8
Errors: 0
Total failing tests introduced: 330

✓ Results saved to: logs/custom_validation_results/
  - summary.json: Overall summary
  - <instance_id>/: Individual bug reports
======================================================================
```

**Key Metrics**:
- **74.2% detection rate**: 23 out of 31 bugs triggered test failures
- **330 failing tests**: Average of ~14 tests per detected bug
- **0 errors**: All bugs applied cleanly without crashes

