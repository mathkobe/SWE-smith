#!/usr/bin/env python3
"""
Custom validation script that actually works.

This script:
1. Runs tests BEFORE applying patch (baseline)
2. Applies the patch
3. Runs tests AFTER applying patch
4. Compares results to find fail-to-pass tests
5. Generates proper reports

Usage:
    python swesmith/local_valid_multiple_process.py <patches.json> --image <image_name> [--workers N]
"""

import json
import argparse
import docker
import sys
import base64
from pathlib import Path
from typing import List, Dict, Any
from multiprocessing import Pool, Manager
from functools import partial
from tqdm import tqdm
import re
from datetime import datetime


class CustomValidator:
    def __init__(self, image_name: str):
        self.client = docker.from_env()
        self.image_name = image_name
        
    def parse_pytest_output(self, output: str) -> Dict[str, Any]:
        """Parse pytest output to extract test results."""
        # Look for the summary line like: "4 failed, 367 passed, 2 skipped"
        lines = output.split('\n')
        
        result = {
            'passed': 0,
            'failed': 0,
            'skipped': 0,
            'errors': 0,
            'failed_tests': [],
            'passed_tests': [],
            'raw_output': output
        }
        
        # Find summary line
        for line in lines:
            # Match patterns like "371 passed" or "4 failed, 367 passed"
            if 'passed' in line or 'failed' in line:
                # Extract numbers
                if match := re.search(r'(\d+)\s+failed', line):
                    result['failed'] = int(match.group(1))
                if match := re.search(r'(\d+)\s+passed', line):
                    result['passed'] = int(match.group(1))
                if match := re.search(r'(\d+)\s+skipped', line):
                    result['skipped'] = int(match.group(1))
                if match := re.search(r'(\d+)\s+error', line):
                    result['errors'] = int(match.group(1))
        
        # Extract individual failed tests
        for line in lines:
            if 'FAILED' in line:
                # Extract test name
                if match := re.search(r'FAILED\s+(\S+)', line):
                    result['failed_tests'].append(match.group(1))
        
        return result
    
    def run_tests_in_container(self, patch_content: str = None, debug: bool = False) -> Dict[str, Any]:
        """
        Run tests in a container, optionally with a patch applied.
        
        Returns:
            Dict with test results
        """
        if patch_content:
            # Use base64 to avoid any escaping issues
            import base64
            patch_b64 = base64.b64encode(patch_content.encode('utf-8')).decode('ascii')
            
            script = f"""
source /opt/miniconda3/etc/profile.d/conda.sh
conda activate testbed
cd /testbed

# Decode patch from base64
echo '{patch_b64}' | base64 -d > /tmp/test.patch

echo "Applying patch..."
git apply /tmp/test.patch 2>&1
patch_result=$?
echo "Patch applied, exit code: $patch_result"

if [ $patch_result -ne 0 ]; then
    echo "ERROR: Failed to apply patch"
    echo "Patch content:"
    cat /tmp/test.patch
    exit 1
fi

pytest tests/ -v --tb=short 2>&1
"""
        else:
            script = """
source /opt/miniconda3/etc/profile.d/conda.sh
conda activate testbed
cd /testbed
pytest tests/ -v --tb=short 2>&1
"""
        
        try:
            # Create and run container
            container = self.client.containers.create(
                self.image_name,
                command=["/bin/bash", "-c", script],
                detach=True,
            )
            container.start()
            
            # Wait for completion
            result = container.wait()
            
            # Get output regardless of exit code
            output = container.logs(stdout=True, stderr=True).decode('utf-8', errors='ignore')
            
            # Remove container
            container.remove()
            
            if debug:
                print(f"\n  --- Container Output (first 1000 chars) ---")
                print(output[:1000])
                print(f"  --- Last 1000 chars ---")
                print(output[-1000:])
                print(f"  --- End Output ---\n")
            
            return self.parse_pytest_output(output)
            
        except docker.errors.ContainerError as e:
            # Container ran but exited with error - that's OK, get the output
            output = e.container.logs(stdout=True, stderr=True).decode('utf-8', errors='ignore')
            if debug:
                print(f"\n  --- Container Error Output ---")
                print(output[:1000])
                print(f"  ---")
            return self.parse_pytest_output(output)
            
        except Exception as e:
            return {
                'passed': 0,
                'failed': 0,
                'error': str(e),
                'failed_tests': [],
                'passed_tests': [],
                'raw_output': str(e)
            }
    
    def validate_single_patch(self, patch_data: Dict[str, Any], output_dir: Path = None) -> Dict[str, Any]:
        """
        Validate a single patch by comparing test results before and after.
        
        Returns:
            Report with fail_to_pass, pass_to_fail, etc.
        """
        instance_id = patch_data.get('instance_id', 'unknown')
        patch = patch_data.get('patch', '')
        
        print(f"\nValidating: {instance_id}")
        
        # Create instance directory
        if output_dir:
            instance_dir = output_dir / instance_id
            instance_dir.mkdir(parents=True, exist_ok=True)
            log_file = instance_dir / "run_instance.log"
            log_lines = []
        else:
            log_lines = None
        
        def log(message):
            print(f"  {message}")
            if log_lines is not None:
                log_lines.append(message)
        
        # Run tests WITHOUT patch (baseline)
        log("Running tests without patch...")
        before_results = self.run_tests_in_container(patch_content=None, debug=False)
        
        if 'error' in before_results:
            log(f"❌ Error running baseline tests: {before_results['error']}")
            return self._create_error_report(instance_id, before_results['error'], log_lines, output_dir)
        
        log(f"Baseline: {before_results['passed']} passed, {before_results['failed']} failed")
        
        # Save baseline test output
        if output_dir:
            test_output_file = instance_dir / "test_output_baseline.txt"
            with open(test_output_file, 'w') as f:
                f.write(before_results.get('raw_output', ''))
        
        # Run tests WITH patch
        log("Running tests with patch...")
        after_results = self.run_tests_in_container(patch_content=patch, debug=False)
        
        if 'error' in after_results:
            log(f"❌ Error running patched tests: {after_results['error']}")
            return self._create_error_report(instance_id, after_results['error'], log_lines, output_dir)
        
        log(f"After patch: {after_results['passed']} passed, {after_results['failed']} failed")
        
        # Save patched test output
        if output_dir:
            test_output_file = instance_dir / "test_output.txt"
            with open(test_output_file, 'w') as f:
                f.write(after_results.get('raw_output', ''))
        
        # Compare results
        before_failed_set = set(before_results['failed_tests'])
        after_failed_set = set(after_results['failed_tests'])
        
        fail_to_pass = list(before_failed_set - after_failed_set)
        pass_to_fail = list(after_failed_set - before_failed_set)
        fail_to_fail = list(before_failed_set & after_failed_set)
        pass_to_pass = []
        
        report = {
            'FAIL_TO_PASS': fail_to_pass,
            'PASS_TO_FAIL': pass_to_fail,
            'FAIL_TO_FAIL': fail_to_fail,
            'PASS_TO_PASS': pass_to_pass,
        }
        
        # Determine if this is a bug that was caught
        if len(pass_to_fail) > 0:
            log(f"✓ Bug detected! {len(pass_to_fail)} test(s) now failing")
            log(f"  Failed tests: {pass_to_fail[:3]}")
        else:
            log(f"⚠ No tests detected this bug")
        
        # Save all files
        if output_dir:
            # Save report.json
            report_file = instance_dir / "report.json"
            with open(report_file, 'w') as f:
                json.dump(report, f, indent=4)
            
            # Save patch.diff
            patch_file = instance_dir / "patch.diff"
            with open(patch_file, 'w') as f:
                f.write(patch)
            
            # Save eval.sh
            eval_sh_file = instance_dir / "eval.sh"
            with open(eval_sh_file, 'w') as f:
                f.write("#!/bin/bash\n")
                f.write("set -e\n\n")
                f.write("source /opt/miniconda3/etc/profile.d/conda.sh\n")
                f.write("conda activate testbed\n")
                f.write("cd /testbed\n\n")
                f.write("# Run tests\n")
                f.write("pytest tests/ -v --tb=short\n")
            
            # Save run_instance.log
            log_file = instance_dir / "run_instance.log"
            with open(log_file, 'w') as f:
                f.write('\n'.join(log_lines))
        
        # Add metadata for summary
        report['instance_id'] = instance_id
        report['status'] = 'completed'
        report['before_summary'] = {
            'passed': before_results['passed'],
            'failed': before_results['failed'],
            'total': before_results['passed'] + before_results['failed']
        }
        report['after_summary'] = {
            'passed': after_results['passed'],
            'failed': after_results['failed'],
            'total': after_results['passed'] + after_results['failed']
        }
        
        return report
    
    def _create_error_report(self, instance_id: str, error: str, log_lines: list, output_dir: Path) -> Dict[str, Any]:
        """Create an error report."""
        report = {
            'instance_id': instance_id,
            'status': 'error',
            'error': error,
            'FAIL_TO_PASS': [],
            'PASS_TO_FAIL': [],
            'FAIL_TO_FAIL': [],
            'PASS_TO_PASS': []
        }
        
        if output_dir and log_lines:
            instance_dir = output_dir / instance_id
            instance_dir.mkdir(parents=True, exist_ok=True)
            
            # Save error report
            report_file = instance_dir / "report.json"
            with open(report_file, 'w') as f:
                json.dump({
                    'FAIL_TO_PASS': [],
                    'PASS_TO_FAIL': [],
                    'FAIL_TO_FAIL': [],
                    'PASS_TO_PASS': [],
                    'error': error
                }, f, indent=4)
            
            # Save log
            log_file = instance_dir / "run_instance.log"
            with open(log_file, 'w') as f:
                f.write('\n'.join(log_lines))
        
        return report
    
    def validate_all(self, patches: List[Dict[str, Any]], workers: int = 1, output_dir: Path = None) -> List[Dict[str, Any]]:
        """
        Validate all patches using multiprocessing.
        
        Args:
            patches: List of patch dicts
            workers: Number of parallel workers
            output_dir: Directory to save reports
            
        Returns:
            List of validation reports
        """
        if output_dir:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        
        if workers == 1:
            # Sequential processing
            reports = []
            for patch in tqdm(patches, desc="Validating"):
                report = self.validate_single_patch(patch, output_dir=output_dir)
                reports.append(report)
            return reports
        else:
            # Multiprocessing
            print(f"Using {workers} parallel workers")
            
            # Create partial function with fixed arguments
            validate_func = partial(_validate_worker, 
                                   image_name=self.image_name, 
                                   output_dir=str(output_dir) if output_dir else None)
            
            # Use multiprocessing Pool
            with Pool(processes=workers) as pool:
                # Use imap_unordered for progress bar
                results = list(tqdm(
                    pool.imap_unordered(validate_func, patches),
                    total=len(patches),
                    desc="Validating"
                ))
            
            return results


# Worker function for multiprocessing (must be at module level)
def _validate_worker(patch_data: Dict[str, Any], image_name: str, output_dir: str = None) -> Dict[str, Any]:
    """
    Worker function for multiprocessing.
    Must be at module level for pickling.
    """
    validator = CustomValidator(image_name)
    output_path = Path(output_dir) if output_dir else None
    return validator.validate_single_patch(patch_data, output_dir=output_path)


def main():
    parser = argparse.ArgumentParser(description='Custom validation for bug patches')
    parser.add_argument('patches_json', help='JSON file with patches')
    parser.add_argument('--image', required=True, help='Docker image to use')
    parser.add_argument('--workers', type=int, default=1, help='Number of parallel workers')
    parser.add_argument('--output', '-o', help='Output directory for reports', 
                       default='logs/custom_validation_results')
    args = parser.parse_args()
    
    # Load patches
    with open(args.patches_json, 'r') as f:
        patches = json.load(f)
    
    if not isinstance(patches, list):
        patches = [patches]
    
    print(f"\n{'='*70}")
    print(f"Custom Validation")
    print('='*70)
    print(f"Patches: {len(patches)}")
    print(f"Image: {args.image}")
    print(f"Workers: {args.workers}")
    print(f"Output: {args.output}")
    
    # Verify image
    client = docker.from_env()
    try:
        client.images.get(args.image)
        print(f"✓ Image found")
    except:
        print(f"❌ Image not found: {args.image}")
        sys.exit(1)
    
    # Run validation
    validator = CustomValidator(args.image)
    reports = validator.validate_all(patches, workers=args.workers, output_dir=Path(args.output))
    
    # Generate summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print('='*70)
    
    total = len(reports)
    errors = sum(1 for r in reports if r.get('status') == 'error')
    bugs_detected = sum(1 for r in reports if len(r.get('PASS_TO_FAIL', [])) > 0)
    bugs_not_detected = total - errors - bugs_detected
    
    total_pass_to_fail = sum(len(r.get('PASS_TO_FAIL', [])) for r in reports)
    
    print(f"Total patches: {total}")
    print(f"Bugs detected (1+ fail-to-pass): {bugs_detected}")
    print(f"Bugs not detected (0 fail-to-pass): {bugs_not_detected}")
    print(f"Errors: {errors}")
    print(f"Total failing tests introduced: {total_pass_to_fail}")
    
    # Save summary
    summary = {
        'total_patches': total,
        'bugs_detected': bugs_detected,
        'bugs_not_detected': bugs_not_detected,
        'errors': errors,
        'total_pass_to_fail_tests': total_pass_to_fail,
        'reports': reports
    }
    
    summary_file = Path(args.output) / 'summary.json'
    with open(summary_file, 'w') as f:
        json.dump(summary, f, indent=2)
    
    print(f"\n✓ Results saved to: {args.output}/")
    print(f"  - summary.json: Overall summary")
    print(f"  - <instance_id>.json: Individual reports")
    print('='*70)


if __name__ == '__main__':
    main()