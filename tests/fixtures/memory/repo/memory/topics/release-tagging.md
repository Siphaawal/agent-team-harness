---
name: release-tagging
description: How releases are tagged - semantic version tags pushed from main after the changelog is updated.
metadata:
  type: runbook
  tags: [release]
  status: active
  author: sam
  created: 2026-09-12
---
Update the changelog, bump the semantic version, create an annotated tag from main, push the tag,
and let the pipeline build the release artifacts from the tag.
