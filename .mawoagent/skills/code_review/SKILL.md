---
name: code_review
description: Review code for bugs, security issues, regressions, maintainability risks, and missing tests.
user-invocable: false
when-to-use: Use when the user asks to review code, code review a file or diff, inspect code quality, find bugs, identify security issues, evaluate maintainability, or check for missing tests.
---

# Code Review Skill

Use this skill when reviewing source code, diffs, or implementation changes.

## Review Focus

Prioritize concrete findings over summaries. Focus on issues that can cause incorrect behavior, security exposure, regressions, data loss, resource leaks, or meaningful maintainability problems.

Check for:

- Correctness bugs and edge cases
- Security vulnerabilities, including injection, unsafe file paths, leaked secrets, and untrusted input handling
- Async, concurrency, lifecycle, and cancellation issues
- Error handling gaps and resource leaks
- API contract mismatches, invalid payload formats, and incompatible parameter names
- Missing or weak tests for changed behavior
- Maintainability risks that are likely to create future defects

## Review Method

Read the relevant files before making claims. If the user provides a file path, review that file directly. If the user provides a diff or describes a change, focus on the changed behavior and affected call sites.

Do not list generic best practices unless they apply to the code being reviewed. If there are no clear issues, say so and mention any remaining test gaps or residual risk.

## Output Format

Put findings first, ordered by severity. Include file and line references when possible.

Use this structure:

1. Findings
   - Severity
   - Location
   - Problem
   - Why it matters
   - Suggested fix
2. Open questions or assumptions
3. Test gaps or residual risk

If no issues are found, write: "No blocking issues found." Then list any test gaps or residual risk.
