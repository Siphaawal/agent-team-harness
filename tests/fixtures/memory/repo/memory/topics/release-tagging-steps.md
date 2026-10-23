---
name: release-tagging-steps
description: Steps for tagging a release - update the changelog, bump the semantic version, push an annotated tag from main.
metadata:
  type: runbook
  tags: [release]
  status: active
  author: alex
  created: 2026-09-14
---
Update the changelog, bump the semantic version, create an annotated tag from main, push the tag,
and the pipeline builds the release artifacts from that tag.
