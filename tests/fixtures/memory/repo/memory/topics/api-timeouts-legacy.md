---
name: api-timeouts-legacy
description: Old rule - every API call picked its own timeout. Reversed; see api-timeouts.
metadata:
  type: decision
  tags: [api]
  status: superseded
  superseded_by: api-timeouts
  author: sam
  created: 2026-08-02
---
Each caller chose its own timeout. This produced hung workers when an upstream stalled, so the team
moved to one shared client with fixed timeouts.
