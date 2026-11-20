# ADR 0003 - Tech fingerprints are downloaded at run time, never vendored

Status: accepted (2026-11-20)

## Context
Wappalyzer's technology definitions were MIT-licensed until the project went closed-source in
2023. The maintained community fork used here, `enthec/webappanalyzer`, is licensed
**GPL-3.0** (verified from its LICENSE file on raw.githubusercontent.com on 2026-11-20).
This repository is MIT.

## Decision
* `collectors/techstack.py` downloads `src/technologies/*.json` from the fork at run time into
  the git-ignored cache directory. The JSON is never committed or redistributed.
* Only the derived facts (technology names detected on a public homepage) are stored.
* A short hash of the fingerprint set is stored with each scan (`meta.fp_version`) so a change
  in detections caused by a fingerprint update can be told apart from a real stack change.
* Tests use a six-entry hand-written fixture, not the GPL data.

## Consequences
The collector needs raw.githubusercontent.com at run time (available both in GitHub Actions and
in the build sandbox). If GPL use is unacceptable for a deployment, swap `FP_BASE` for another
source; the matcher only needs the `cats/html/scriptSrc/meta/headers/cookies/implies` keys.
