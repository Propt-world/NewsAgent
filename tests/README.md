# Tests Overview

This folder contains automated tests for NewsAgent API, scheduler, and LangGraph workflow behavior.
The suite is designed for fast validation of critical logic paths and deployment safety checks.

## Test Structure

- `conftest.py`: test bootstrap and default environment setup.
- `test_api_main.py`: API endpoint behavior for submission, status lookup, and health.
- `test_security.py`: API key and webhook secret guard validation.
- `test_scheduler_cycle.py`: scheduler due-cycle logic and UTC normalization helper.
- `test_link_discovery.py`: listing URL extraction and filtering rules.
- `test_raw_extraction_helpers.py`: CDN/WAF challenge detection helper behavior.
- `test_langgraph_workflow.py`: LangGraph compile integrity, node/edge wiring, and mocked invoke path.
- `test_env_example_alignment.py`: `.env` and `.env.example` key alignment enforcement.

## Tests Coverage

- API submission and status endpoints (`/submit-job`, `/jobs/{job_id}`, `/health`).
- Security middleware behavior for API and webhook secrets.
- Scheduler cycle eligibility and scheduling triggers.
- Link discovery filtering (same-domain, ad/social/pagination exclusion, pattern matching).
- Extraction helper logic for WAF/challenge detection.
- LangGraph workflow compile-time integrity and route correctness.
- Environment template consistency required for CI and onboarding.
