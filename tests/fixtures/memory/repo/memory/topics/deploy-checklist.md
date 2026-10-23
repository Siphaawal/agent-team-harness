---
name: deploy-checklist
description: "Runbook: the five steps before any production deploy, including the canary window."
metadata:
  type: runbook
  tags: [deploy, release]
  status: active
  author: alex
  created: 2026-10-01
  verified: 2026-10-15
---
1. Confirm the build is green on main.
2. Announce the deploy in the team channel.
3. Ship to the canary pool and watch error rates for fifteen minutes.
4. Promote to the full fleet.
5. Record the version in the release log.

See [[staging-freeze]] for when deploys are not allowed at all.
