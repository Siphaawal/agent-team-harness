#!/usr/bin/env python3
"""test_team_gates.py - the team gates (team-gate, team-stall-gate, team-watchdog) and the review / heredoc rules,
driven end to end as the harness drives them: JSON on stdin, verdict on stdout / stderr / exit code.

Everything runs in a throwaway copy of .claude/hooks + tools, with TEAM_CLAUDE_HOME pointing at a temporary "claude
home" that holds the task list and teammate transcripts, so no real home directory or state dir is touched.

MUTATION tests: each guard is "compiled out" in a scratch copy (one exact source line replaced) and its self-test must
then FAIL. A guard whose test still passes without the guard is not tested at all. If a refactor moves one of the
mutated lines, the test fails loudly ("mutation target not found") instead of silently testing nothing.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "python3"
IGNORE = shutil.ignore_patterns("__pycache__", "state", "leak_patterns.txt")
SID = "5e551011-aaaa-4bbb-8ccc-000000000001"
TEAM = "session-5e551011"


def copy_tree(dst):
    os.makedirs(os.path.join(dst, ".claude"))
    shutil.copytree(os.path.join(SRC, ".claude", "hooks"), os.path.join(dst, ".claude", "hooks"), ignore=IGNORE)
    shutil.copytree(os.path.join(SRC, "tools"), os.path.join(dst, "tools"), ignore=IGNORE)
    shutil.copy(os.path.join(SRC, ".claude", "settings.json"), os.path.join(dst, ".claude", "settings.json"))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="team-gates-test-")
        cls.root = os.path.join(cls.tmp, "repo")
        copy_tree(cls.root)
        cls.home = os.path.join(cls.tmp, "home")
        cls.tasks = os.path.join(cls.home, "tasks", TEAM)
        cls.sub = os.path.join(cls.home, "projects", "proj", SID, "subagents")
        os.makedirs(cls.tasks)
        os.makedirs(cls.sub)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def env(self):
        e = dict(os.environ)
        for k in ("TEAM_TASKS_DIR", "TEAM_PROJECTS_DIR", "CLAUDE_CONFIG_DIR"):
            e.pop(k, None)
        e["TEAM_CLAUDE_HOME"] = self.home
        e["TEAM_WATCHDOG_TICK"] = "0.05"
        return e

    def hook(self, script, payload, *args):
        r = subprocess.run([PY, "-I", os.path.join(self.root, ".claude", "hooks", script)] + list(args), cwd=self.root,
                           input=json.dumps(payload), capture_output=True, text=True, timeout=120, env=self.env())
        return r.returncode, r.stdout, r.stderr

    def state(self, name):
        return os.path.join(self.root, ".claude", "state", name)

    def calls(self, sid, n):
        os.makedirs(os.path.dirname(self.state("x")), exist_ok=True)
        with open(self.state("skills-%s.json" % sid), "w") as fh:
            json.dump({"calls": n}, fh)

    def task(self, i, owner, status):
        with open(os.path.join(self.tasks, "%s.json" % i), "w") as fh:
            json.dump({"id": str(i), "owner": owner, "status": status, "subject": "s"}, fh)

    def teammate(self, name, age_s=0, text="on it"):
        p = os.path.join(self.sub, "agent-a%s-77aa.jsonl" % name)
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        with open(p[:-6] + ".meta.json", "w") as fh:
            json.dump({"name": name, "teamName": TEAM}, fh)
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p


class TestTeamGate(Base):
    def test_block_waive_expire_spawn(self):
        sid = "tg-1"
        self.calls(sid, 45)
        rc, out, _ = self.hook("team-gate.py", {"session_id": sid}, "stop")
        self.assertEqual(json.loads(out)["decision"], "block")                    # no teammate past 40 calls
        self.assertIn("team-gate.py\" waive " + sid, json.loads(out)["reason"])   # tells the lead how to waive
        rc, out, err = self.hook("team-gate.py", {}, "waive", sid, "a one-file fix")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.hook("team-gate.py", {}, "status", sid)
        self.assertEqual(rc, 0)
        with open(self.state("waivers.log")) as fh:
            self.assertIn("a one-file fix", fh.read())                            # audited
        for i, f in enumerate(("a.py", "b.py", "c.py")):
            rc, out, _ = self.hook("team-gate.py", {"session_id": sid, "tool_name": "Edit",
                                                    "tool_input": {"file_path": os.path.join(self.root, "src", f)}},
                                   "nag")
        self.assertIn("EXPIRED", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        rc, out, _ = self.hook("team-gate.py", {}, "status", sid)
        self.assertEqual(rc, 1)                                                   # gate open again
        self.hook("team-gate.py", {"session_id": sid, "tool_name": "Agent",
                                   "tool_input": {"name": "builder-api", "subagent_type": "general-purpose"}}, "mark")
        rc, out, _ = self.hook("team-gate.py", {"session_id": sid}, "stop")
        self.assertEqual(out.strip(), "")

    def test_teammate_never_gated(self):
        self.calls("tg-2", 99)
        rc, out, _ = self.hook("team-gate.py", {"session_id": "tg-2", "agent_id": "a1", "agent_type": "x"}, "stop")
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_bad_payload_fails_open(self):
        r = subprocess.run([PY, "-I", os.path.join(self.root, ".claude", "hooks", "team-gate.py"), "stop"],
                           cwd=self.root, input="{not json", capture_output=True, text=True, timeout=60, env=self.env())
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


class TestStallAndWatchdog(Base):
    def test_idle_refused_then_lead_told(self):
        self.task(1, "builder-api", "in_progress")
        idle = {"session_id": SID, "teammate_name": "builder-api", "team_name": TEAM, "agent_id": "x",
                "hook_event_name": "TeammateIdle"}
        for _ in range(3):
            rc, _, err = self.hook("team-stall-gate.py", idle, "idle")
            self.assertEqual(rc, 2)
            self.assertIn("#1", err)
        rc, _, _ = self.hook("team-stall-gate.py", idle, "idle")
        self.assertEqual(rc, 0)                                                   # allowed, recorded as a stall
        rc, out, _ = self.hook("team-stall-gate.py", {"session_id": SID}, "stop")
        self.assertIn("builder-api", json.loads(out)["reason"])
        self.task(1, "builder-api", "completed")

    def test_watchdog_gate_and_taskstop(self):
        self.task(2, "reviewer-docs", "in_progress")
        p = self.teammate("reviewer-docs", age_s=5)
        rc, out, _ = self.hook("team-stall-gate.py", {"session_id": SID}, "stop")
        reason = json.loads(out)["reason"]
        self.assertIn('python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/team-watchdog.py" ' + SID, reason)
        self.assertIn("run_in_background", reason)
        # the watchdog run as the lead would: it names the quiet teammate and its last words
        os.utime(p, None)                                                         # active "now" for the short window
        r = subprocess.run([PY, "-I", os.path.join(self.root, ".claude", "hooks", "team-watchdog.py"), SID,
                            "--minutes", "0.02", "--max", "1"], cwd=self.root, capture_output=True, text=True,
                           timeout=120, env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STALE: reviewer-docs", r.stdout)
        self.assertIn("on it", r.stdout)
        # TaskStop through the PostToolUse hook: from then on neither the gate nor the watchdog waits on it
        self.hook("team-stall-gate.py", {"session_id": SID, "tool_name": "TaskStop",
                                         "tool_input": {"task_id": "reviewer-docs"},
                                         "tool_response": {"message": "Successfully stopped task: t9 (review)"}},
                  "stopped")
        os.utime(p, None)
        rc, out, _ = self.hook("team-stall-gate.py", {"session_id": SID}, "stop")
        self.assertEqual(out.strip(), "")
        r = subprocess.run([PY, "-I", os.path.join(self.root, ".claude", "hooks", "team-watchdog.py"), SID,
                            "--minutes", "0.02", "--max", "1"], cwd=self.root, capture_output=True, text=True,
                           timeout=120, env=self.env(), stdin=subprocess.DEVNULL)
        self.assertIn("QUIET", r.stdout)
        self.assertNotIn("STALE", r.stdout)
        os.remove(p)
        os.remove(p[:-6] + ".meta.json")
        self.task(2, "reviewer-docs", "completed")

    def test_missing_locations_fail_open(self):
        e = self.env()
        e["TEAM_CLAUDE_HOME"] = os.path.join(self.tmp, "does-not-exist")
        for mode, payload in (("idle", {"session_id": SID, "teammate_name": "x"}), ("stop", {"session_id": SID})):
            r = subprocess.run([PY, "-I", os.path.join(self.root, ".claude", "hooks", "team-stall-gate.py"), mode],
                               cwd=self.root, input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                               env=e)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), mode)


class TestReviewRule(Base):
    def test_large_unreviewed_commit_blocked_end_to_end(self):
        repo = os.path.join(self.tmp, "work")
        git = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True, timeout=60,
                                        stdin=subprocess.DEVNULL)
        subprocess.run(["git", "init", "-q", repo], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        with open(os.path.join(repo, "big.py"), "w") as fh:
            fh.write("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "init")
        with open(os.path.join(repo, "big.py"), "w") as fh:
            fh.write("".join("v%d = %d\n" % (i, i) for i in range(300)))
        tr = os.path.join(self.tmp, "lead.jsonl")
        open(tr, "w").close()
        pay = lambda cmd: {"session_id": "rv", "cwd": repo, "transcript_path": tr, "tool_name": "Bash",
                           "tool_input": {"command": cmd}}
        rc, _, err = self.hook("git-guard.py", pay('git commit -m "add the parser" big.py'))
        self.assertEqual(rc, 2)                                       # pathspec commit: 301 lines, no review
        self.assertIn("review rule", err)
        rc, _, _ = self.hook("git-guard.py", pay('git commit -m "add the parser" big.py  # no-review-ok: generated'))
        self.assertEqual(rc, 0)
        with open(self.state("git-guard-overrides.log")) as fh:
            self.assertIn("no-review-ok", fh.read())                  # the override is audited
        with open(tr, "w") as fh:
            fh.write(json.dumps({"message": {"content": [{"type": "tool_use", "id": "r1", "name": "Agent",
                                                          "input": {"subagent_type": "reviewer"}}]}}) + "\n")
            fh.write(json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "r1",
                                                          "content": "ok"}]}}) + "\n")
        rc, _, err = self.hook("git-guard.py", pay('git commit -m "add the parser" big.py'))
        self.assertEqual(rc, 0, err)                                  # reviewed since the last commit

    def test_new_files_added_in_the_same_line_are_counted(self):
        repo = os.path.join(self.tmp, "work-new")
        git = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True, timeout=60,
                                        stdin=subprocess.DEVNULL)
        subprocess.run(["git", "init", "-q", repo], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        for f in ("big.py", "small.txt"):
            with open(os.path.join(repo, f), "w") as fh:
                fh.write("x\n")
        git("add", "-A")
        git("commit", "-qm", "init")
        with open(os.path.join(repo, "small.txt"), "w") as fh:
            fh.write("y\n")
        with open(os.path.join(repo, "parser.py"), "w") as fh:       # a NEW 500-line file; nothing else is large
            fh.write("".join("w%d = 2\n" % i for i in range(500)))
        tr = os.path.join(self.tmp, "lead-new.jsonl")
        open(tr, "w").close()
        pay = lambda cmd: {"session_id": "rv2", "cwd": repo, "transcript_path": tr, "tool_name": "Bash",
                           "tool_input": {"command": cmd}}
        for cmd in ("git add parser.py && git commit -m 'feat: parser'", "git add -A && git commit -m x",
                    "git add . ; git commit -m x"):
            rc, _, err = self.hook("git-guard.py", pay(cmd))
            self.assertEqual(rc, 2, cmd)
            self.assertIn("review rule", err)
        os.remove(os.path.join(repo, "parser.py"))
        with open(os.path.join(repo, "big.py"), "w") as fh:          # 300 unstaged lines NOT in the commits below
            fh.write("".join("v%d = 1\n" % i for i in range(300)))
        for cmd in ("git add small.txt && git commit -m 'fix typo'",
                    "git add small.txt\ngit commit -m \"$(cat <<'EOF'\nfix: typo\nEOF\n)\""):
            rc, _, err = self.hook("git-guard.py", pay(cmd))
            self.assertEqual(rc, 0, cmd + "\n" + err)                 # the unrelated big.py change is not counted


class TestTeamGateFixes(Base):
    def test_taskstop_real_response_with_error_in_description(self):
        sid = "77777777-0000-4000-8000-000000000000"
        sub = os.path.join(self.home, "projects", "proj", sid, "subagents")
        os.makedirs(sub)
        p = os.path.join(sub, "agent-areviewer-x-1.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n")
        with open(p[:-6] + ".meta.json", "w") as fh:
            json.dump({"name": "reviewer-x"}, fh)
        rc, out, _ = self.hook("team-stall-gate.py", {"session_id": sid}, "stop")
        self.assertIn('"block"', out)                                 # positive control: active, no watchdog
        desc = "Review error handling and failure paths"
        self.hook("team-stall-gate.py", {"session_id": sid, "tool_name": "TaskStop",
                                         "tool_input": {"task_id": "reviewer-x"},
                                         "tool_response": {"message": "Successfully stopped task: t1 (%s)" % desc,
                                                           "task_id": "t1", "task_type": "in_process_teammate",
                                                           "command": desc}}, "stopped")
        rc, out, _ = self.hook("team-stall-gate.py", {"session_id": sid}, "stop")
        self.assertEqual(out.strip(), "")

    def test_gate_then_small_minutes_watchdog_does_not_ping_pong(self):
        sid = "88888888-0000-4000-8000-000000000000"
        sub = os.path.join(self.home, "projects", "proj", sid, "subagents")
        os.makedirs(sub)
        p = os.path.join(sub, "agent-abuilder-y-1.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n")
        with open(p[:-6] + ".meta.json", "w") as fh:
            json.dump({"name": "builder-y"}, fh)
        t = time.time() - 180                                        # active 3 minutes ago: inside the gate window
        os.utime(p, (t, t))
        rc, out, _ = self.hook("team-stall-gate.py", {"session_id": sid}, "stop")
        self.assertIn('"block"', out)
        self.assertNotIn("--minutes", json.loads(out)["reason"])      # the suggested command carries no smaller value
        r = subprocess.run([PY, "-I", os.path.join(self.root, ".claude", "hooks", "team-watchdog.py"), sid,
                            "--minutes", "2", "--max", "0.05"], cwd=self.root, capture_output=True, text=True,
                           timeout=120, env=self.env(), stdin=subprocess.DEVNULL)
        self.assertIn("STALE: builder-y", r.stdout)                  # not QUIET at once
        self.assertNotIn("QUIET", r.stdout)

    def test_watchdog_malformed_record_does_not_crash(self):
        sid = "99999999-0000-4000-8000-000000000000"
        sub = os.path.join(self.home, "projects", "proj", sid, "subagents")
        os.makedirs(sub)
        p = os.path.join(sub, "agent-abuilder-z-1.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": "oops"}) + "\n")
        with open(p[:-6] + ".meta.json", "w") as fh:
            json.dump({"name": "builder-z"}, fh)
        r = subprocess.run([PY, "-I", os.path.join(self.root, ".claude", "hooks", "team-watchdog.py"), sid,
                            "--minutes", "0.02", "--max", "0.1"], cwd=self.root, capture_output=True, text=True,
                           timeout=120, env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STALE: builder-z", r.stdout)
        self.assertNotIn("Traceback", r.stderr)


class TestMutations(unittest.TestCase):
    """Each guard compiled out -> its self-test must FAIL."""
    MUTATIONS = [
        ("team-stall-gate.py", "        return 2\n    if not any(", "        return 0\n    if not any(",
         "team-stall-gate.py"),
        ("_teamlib.py", " and not is_stopped(n, r, stops)}", "}", "team-watchdog.py"),
        ("_teamlib.py", " and not is_stopped(n, r, stops)}", "}", "team-stall-gate.py"),
        ("_teamlib.py", "if not r[\"finished\"] and n not in done", "if n not in done", "team-watchdog.py"),
        ("team-watchdog.py", "        if over:\n            for n, t in over:", "        if False:\n            for n, t in over:",
         "team-watchdog.py"),
        ("team-gate.py", "    print(json.dumps({\"decision\": \"block\", \"reason\": NAG % (calls, sid)}))", "    pass",
         "team-gate.py"),
        ("team-gate.py", "    if len(seen) < WAIVER_BUILD_FILES:", "    if True:", "team-gate.py"),
        ("team-gate.py", "    return bool(name) and str(", "    return str(", "team-gate.py"),
        # a lead-call counter that went backwards (state reset) silences the nag until it passes the old value
        ("team-gate.py", "0 <= calls - last < NAG_EVERY", "calls < last + NAG_EVERY", "team-gate.py"),
        ("git-guard.py", "        if n != UNCOUNTED and n < REVIEW_MIN_LINES:", "        if True:", "git-guard.py"),
        ("git-guard.py", "        carry = numstat(repo, [\"HEAD\", \"--\"] + list(paths))\n        # tracked",
         "        carry = numstat(repo, [\"--cached\"])\n        # tracked", "git-guard.py"),
        # a NEW file staged by `git add` in the same line counts 0 (the untracked-file bypass)
        ("git-guard.py", "            out[p] = file_lines(full, None if _STOP_AT[0] is None else _STOP_AT[0] - total)",
         "            out[p] = 0", "git-guard.py"),
        # a `git add` in the line counts the whole tree again (an unrelated unstaged file blocks a small commit, and
        # a whole-tree diff runs on every add + commit)
        ("git-guard.py", "        head = None\n        if all_flag:", "        head = None\n        if all_flag or add_argvs:",
         "git-guard.py"),
        # a plain commit diffs the whole tree (slow on a dirty tree, and it counts unstaged work)
        ("git-guard.py", "        head = None\n        if all_flag:", "        head = None\n        if True:", "git-guard.py"),
        # the add dry run on the REAL index (takes .git/index.lock; fails while another git holds it)
        ("git-guard.py", "env=dict(os.environ, GIT_INDEX_FILE=private)", "env=None", "git-guard.py"),
        # git status / diff run without GIT_OPTIONAL_LOCKS=0 (an opportunistic index refresh takes .git/index.lock)
        ("git-guard.py", "    return dict(os.environ if env is None else env, GIT_OPTIONAL_LOCKS=\"0\")", "    return env",
         "git-guard.py"),
        # a failed dry run counts as nothing instead of `git add -A`
        ("git-guard.py", "        return None\n    finally:\n        if tmpdir:",
         "        return set()\n    finally:\n        if tmpdir:", "git-guard.py"),
        # the index copy gets a fresh mtime: git's racy check is lost and a same-size edit counts 0
        ("git-guard.py", "shutil.copy2(real, private)", "shutil.copyfile(real, private)", "git-guard.py"),
        # a count that failed or timed out is allowed instead of treated as over the threshold
        ("git-guard.py", "            n = UNCOUNTED\n", "            n = None\n", "git-guard.py"),
        ("git-guard.py", "    _DEADLINE[0] = time.time() + COUNT_BUDGET_S", "    _DEADLINE[0] = None", "git-guard.py"),
        # --no-renames: a pure `git mv` of a 400-line file counts 800
        ("git-guard.py", "[\"diff\", \"--numstat\", \"-z\", \"-M\"]", "[\"diff\", \"--numstat\", \"-z\", \"--no-renames\"]",
         "git-guard.py"),
        # a renamed-then-edited file counts as entirely new (the rename source is left out of the diff)
        ("git-guard.py", "    sources = sorted({renames[f] for f in files if renames and f in renames} - set(files))",
         "    sources = []", "git-guard.py"),
        # adds in OTHER repos counted as adds in this one
        ("git-guard.py", " and (r == UNRESOLVED or same_repo(r, repo, cache))]", "]", "git-guard.py"),
        # a glob pathspec commit does not cover the new file the add staged
        ("git-guard.py", " or fnmatch.fnmatchcase(nf, norm(s))", "", "git-guard.py"),
        ("git-guard.py", "                from_file = True", "                pass", "git-guard.py"),
        ("git-guard.py", "            log(\"git-guard-errors.log\", \"review rule: commit repo unresolved (cd to a "
         "variable?), not counted\")", "            pass", "git-guard.py"),
        # adds compared by DIRECTORY again: an add in a subdirectory of the commit's repo counts 0
        ("git-guard.py", "    if same_dir(a, b):\n        return True\n    ta, tb = _toplevel(a, cache), _toplevel(b, cache)\n"
         "    return ta is None or tb is None or ta == tb", "    return same_dir(a, b)", "git-guard.py"),
        # an add whose repo cannot be resolved is dropped instead of counted
        ("git-guard.py", "    return ta is None or tb is None or ta == tb", "    return ta is not None and ta == tb",
         "git-guard.py"),
        # bare braces stay in the segment ("}" and what follows it read as pathspecs)
        ("git-guard.py", "    if \"{\" not in seg and \"}\" not in seg:\n        return [seg]",
         "    if True:\n        return [seg]", "git-guard.py"),
        # braces inside a quoted message taken as tokens
        ("git-guard.py", "        elif c in \"'\\\"\":\n            q = c", "        elif False:\n            q = c", "git-guard.py"),
        # a trailing `}` / `fi` / `)` taken as a pathspec
        ("git-guard.py", "    while paths and paths[-1] in SHELL_TRAILERS:", "    while False:", "git-guard.py"),
        # a magic pathspec or quote fragment no longer counts the whole tree
        ("git-guard.py", "    odd = [p for p in paths if p.startswith(\":\") or \"'\" in p or '\"' in p]", "    odd = []",
         "git-guard.py"),
        # a file in HEAD, out of the index and written again counts only its deletion
        ("git-guard.py", "    regrown = [p for p in files if p in head and p not in known]", "    regrown = []", "git-guard.py"),
        # dry-run output that cannot be parsed is skipped instead of counting the add as -A
        ("git-guard.py", "        else:\n            return None\n    return paths", "    return paths", "git-guard.py"),
        ("git-guard.py", "    if chr(0xFFFD) in text:", "    if False:", "git-guard.py"),
        # no read cap per file / per set of new files, no deadline inside a file
        ("git-guard.py", "                if cap is not None and n >= cap:", "                if False:", "git-guard.py"),
        ("git-guard.py", "        if _STOP_AT[0] is not None and total >= _STOP_AT[0]:", "        if False:", "git-guard.py"),
        ("git-guard.py", "                if _DEADLINE[0] is not None and time.time() > _DEADLINE[0]:\n                    raise",
         "                if False:\n                    raise", "git-guard.py"),
        # one deadline per commit again, or none from the hook run, or the dirty-tree time not taken off the budget
        ("git-guard.py", "    own = _DEADLINE[0] is None\n", "    own = True\n", "git-guard.py"),
        ("git-guard.py", "    _DEADLINE[0], _STOP_AT[0] = time.time() + count_budget(spent), REVIEW_MIN_LINES",
         "    _STOP_AT[0] = REVIEW_MIN_LINES", "git-guard.py"),
        ("git-guard.py", "min(COUNT_BUDGET_S, HOOK_BUDGET_S - spent))", "COUNT_BUDGET_S)", "git-guard.py"),
        # the dirty-tree status calls of one hook run do not share a budget
        ("git-guard.py", "        t = min(t, _STATUS_DEADLINE[0] - time.time())", "        pass", "git-guard.py"),
        # the private index copy is not removed after the dry run
        ("git-guard.py", "        if tmpdir:\n            shutil.rmtree(tmpdir, ignore_errors=True)\n    paths = parse_dry_run",
         "        if False:\n            shutil.rmtree(tmpdir, ignore_errors=True)\n    paths = parse_dry_run", "git-guard.py"),
        # the stale-copy sweep removes young copies too / removes nothing
        ("git-guard.py", "and now - e.stat(follow_symlinks=False).st_mtime > STALE_INDEX_COPY_S", "", "git-guard.py"),
        ("git-guard.py", "                        shutil.rmtree(e.path, ignore_errors=True)", "                        pass",
         "git-guard.py"),
        # the dirty-tree git status runs without GIT_OPTIONAL_LOCKS=0
        ("git-guard.py", "stdin=subprocess.DEVNULL, env=_git_env())", "stdin=subprocess.DEVNULL, env=None)", "git-guard.py"),
        # a directory pathspec does not cover the new file under it
        ("git-guard.py", " or nf.startswith(norm(s) + \"/\")", "", "git-guard.py"),
        ("heredoc-guard.py", "        if not fed:\n            return True", "        if False:\n            return True",
         "heredoc-guard.py"),
        # back to the whole-text regex: a commit message or echo that MENTIONS `python -` is blocked
        ("heredoc-guard.py", "    starved = starved_python(cmd)\n", "    starved = None\n", "heredoc-guard.py"),
        # an empty heredoc on python's own segment counted as stdin
        ("heredoc-guard.py", " and i + 1 < len(w) and w[i + 1] != \"HD_EMPTY\":", ":", "heredoc-guard.py"),
        ("heredoc-guard.py", "    if writers and", "    if False and writers and", "heredoc-guard.py"),
        # wrappers not skipped: `timeout 60 python -`, `uv run python -` ... pass again
        ("heredoc-guard.py", "        if head not in WRAP:\n            return w, True",
         "        if True:\n            return w, True", "heredoc-guard.py"),
        # a wrapper's positional argument (timeout's DURATION) taken for the command word
        ("heredoc-guard.py", "    return i + positional, True", "    return i, True", "heredoc-guard.py"),
        # an unknown wrapper flag lets the segment through instead of falling back to the whole-text rule
        ("heredoc-guard.py", "            if not piped and PY_STDIN.search(\" \".join(w)) and not fed_by(w):",
         "            if False:", "heredoc-guard.py"),
        # no work cap: a command full of unclosed '<<' runs the heredoc scans (and is allowed)
        ("heredoc-guard.py", "    if markers * len(cmd) > MAX_HEREDOC_WORK:", "    if False:", "heredoc-guard.py"),
        # a command with no '<<' runs the heredoc scan anyway
        ("heredoc-guard.py", "    if \"<<\" not in cmd:\n        return slash, doubled, long_, writers\n", "",
         "heredoc-guard.py"),
        # the heredoc regex not anchored at a line start: quadratic on one long line
        ("heredoc-guard.py", "HEREDOC = re.compile(r\"(?m)^(?P<head>", "HEREDOC = re.compile(r\"(?P<head>",
         "heredoc-guard.py"),
        # no newline lookahead after the tag's first letter (each of the three heredoc regexes): a long tag with no
        # newline after it is retried at every shorter length
        ("heredoc-guard.py", "(?P<tag>[A-Za-z_](?=[^\\n]*\\n)[A-Za-z0-9_]*)", "(?P<tag>[A-Za-z_][A-Za-z0-9_]*)",
         "heredoc-guard.py"),
        ("heredoc-guard.py", "([A-Za-z_](?=[^\\n]*\\n)[A-Za-z0-9_]*)\\1(?P<tail>", "([A-Za-z_][A-Za-z0-9_]*)\\1(?P<tail>",
         "heredoc-guard.py"),
        ("heredoc-guard.py", "([A-Za-z_](?=[^\\n]*\\n)[A-Za-z0-9_]*)\\1[^\\n]*\\n\\s*\\2",
         "([A-Za-z_][A-Za-z0-9_]*)\\1[^\\n]*\\n\\s*\\2", "heredoc-guard.py"),
        # the heredoc body tried at every newline of a run of blank lines
        ("heredoc-guard.py", "(?P<body>(?:.*?\\S)??[^\\S\\n]*)", "(?P<body>.*?)", "heredoc-guard.py"),
        # the tag not capped at the longest word alone on a line
        ("heredoc-guard.py", "rx.pattern.replace(TAG_REST, \"[A-Za-z0-9_]{0,%d}\" % (n - 1))", "rx.pattern",
         "heredoc-guard.py"),
        # over the '<<' cap, '# heredoc-long-ok' allows before any other rule runs
        ("heredoc-guard.py", "        why = whole_text_rules(cmd)", "        why = None", "heredoc-guard.py"),
        ("heredoc-guard.py", "    if py and \"\\\\\\\\\" in cmd:\n", "    if False:\n", "heredoc-guard.py"),
        ("heredoc-guard.py", "        if \"|\" not in cmd[start:m.start()] and", "        if False and",
         "heredoc-guard.py"),
        # wrappers outside the first table: setsid, `pyenv exec`, `flock <file> -c`, an unquoted Windows python path
        ("heredoc-guard.py", "    \"setsid\": ({\"-c\", \"--ctty\", \"-f\", \"--fork\", \"-w\", \"--wait\"}, set(), set(), 0),\n",
         "", "heredoc-guard.py"),
        ("heredoc-guard.py", "\"pixi\": \"run\", \"pyenv\": \"exec\"}", "\"pixi\": \"run\"}", "heredoc-guard.py"),
        ("heredoc-guard.py", "        if head in (\"-c\", \"--command\") and len(w) > 1 and depth < 3:", "        if False:",
         "heredoc-guard.py"),
        ("heredoc-guard.py", " and not EATEN_PY.fullmatch(w[0]):", ":", "heredoc-guard.py"),
        # TaskStop success judged by a word search: a description saying "error" is never recorded
        ("team-stall-gate.py", "    return isinstance(msg, str) and msg.strip().lower().startswith(STOP_OK)",
         "    return \"stopped\" in json.dumps(resp).lower() and \"error\" not in json.dumps(resp).lower()",
         "team-stall-gate.py"),
        # a malformed transcript record crashes the watchdog
        ("team-watchdog.py", "        content = msg.get(\"content\") if rec.get(\"type\") == \"assistant\" and "
         "isinstance(msg, dict) else None", "        content = (msg or {}).get(\"content\") if rec.get(\"type\") == "
         "\"assistant\" else None", "team-watchdog.py"),
        ("team-watchdog.py", "        H.fail_open(\"team-watchdog\", exc)", "        raise", "team-watchdog.py"),
        # gate / watchdog ping-pong: `--minutes 2` exits QUIET for a teammate the gate counts as active
        ("team-watchdog.py", "    first = max(minutes, T.ACTIVE_MINUTES) * 60", "    first = minutes * 60",
         "team-watchdog.py"),
        # --also never lets the watchdog exit QUIET
        ("team-watchdog.py", "    if not watched and not any(a < first for a in extra.values()):",
         "    if not watched and not also:", "team-watchdog.py"),
        ("team-watchdog.py", "        if not watched and not any(a < window for a in extra.values()):",
         "        if not watched and not also:", "team-watchdog.py"),
        # --transcript containment: a path flag must not make the watchdog read any directory
        ("_teamlib.py", "if os.path.realpath(d).startswith(root + os.sep) and os.path.isdir(d):",
         "if os.path.isdir(d):", "team-watchdog.py"),
        # a `$`-anchored match lets "name\n" through as a path component
        ("_teamlib.py", "SAFE_NAME.fullmatch(name)", "re.match(SAFE_NAME.pattern + \"$\", name)", "team-stall-gate.py"),
        # a teammate stopped while the watchdog runs stays in its watch set
        ("team-watchdog.py", "        watched &= names  ", "        pass  # ", "team-watchdog.py"),
        # no grace after a stop: the transcript's last write right after TaskStop makes the teammate live again
        ("_teamlib.py", "STOP_GRACE_S = 15.0 ", "STOP_GRACE_S = 0.0 ", "team-stall-gate.py"),
        # TaskStop success judged by "contains" instead of "opens with"
        ("team-stall-gate.py", "msg.strip().lower().startswith(STOP_OK)", "STOP_OK in msg.strip().lower()",
         "team-stall-gate.py"),
    ]

    def test_each_guard_removed_fails_its_selftest(self):
        for target, old, new, selftest in self.MUTATIONS:
            with self.subTest(target=target, old=old[:40], selftest=selftest):
                tmp = tempfile.mkdtemp(prefix="team-mut-")
                try:
                    copy_tree(tmp)
                    path = os.path.join(tmp, ".claude", "hooks", target)
                    with open(path, encoding="utf-8") as fh:
                        src = fh.read()
                    self.assertEqual(src.count(old), 1, "mutation target not found exactly once in " + target)
                    with open(path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(src.replace(old, new))
                    r = subprocess.run([PY, "-I", os.path.join(tmp, ".claude", "hooks", selftest), "test"], cwd=tmp,
                                       capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
                    self.assertNotEqual(r.returncode, 0, "self-test still passes with the guard removed")
                    self.assertIn("BAD ", r.stdout)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

    def test_unmutated_selftests_pass(self):
        """The positive control for the mutation test: the same copy, unmutated, passes."""
        tmp = tempfile.mkdtemp(prefix="team-mut-")
        try:
            copy_tree(tmp)
            for selftest in ("team-gate.py", "team-stall-gate.py", "team-watchdog.py"):
                r = subprocess.run([PY, "-I", os.path.join(tmp, ".claude", "hooks", selftest), "test"], cwd=tmp,
                                   capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, selftest + "\n" + r.stdout[-2000:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
