# Contributing to CustomerWorld

Keep changes focused, explain the behavior they improve, and include relevant validation. For a bug report, describe the expected outcome, the observed outcome, and the smallest reproducible example.

## Local development

Use Python 3.11 or later on macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

The runtime should remain standard-library-only. Tests should use temporary projects and explicit transport fixtures. Normal test runs must not make paid inference requests, open private interfaces, or depend on credentials. Do not present fixture responses as an autonomous research run.

## Changes to behavior

Preserve participant-specific source and memory boundaries, current-date enforcement, resumable daily state, serialized UI execution, and the separation between observations and participant claims. If a change affects a provider schema, broker response, report category, or stored state, update the documentation and include a focused test of the changed boundary.

Do not turn broker errors or action-cap interruptions into voluntary nonvisits. A claimed tool visit must continue to require actual interface evidence. Aggregate exports should remain useful without exposing individual journals or screenshots.

## Safe examples and reports

Use fictional, clearly labeled briefs in public examples. Remove credentials, private source material, personal identifiers, local machine paths, and product screenshots from issues, patches, and test fixtures. A minimal synthetic reproduction is usually sufficient.

Keep documentation precise about what is implemented and what remains a proposal. Describe model-driven participants as simulated customers, and support behavioral claims with the relevant recorded evidence.

By submitting a contribution, you agree to make it available under the project's MIT license.
