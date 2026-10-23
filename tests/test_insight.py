#!/usr/bin/env python3
"""Tests for insight/ (the local dashboard). Needs `node` (18+) on PATH; python3 stdlib only.

    python3 tests/test_insight.py          or:   python3 -m unittest tests.test_insight

It starts ONE server (`node insight/server.mjs --port <free> --repo <fixture> --projects <empty dir>`,
stdin=DEVNULL), asserts against it, and kills only the PID it started. Every "must be refused" check has a
positive control next to it (the same request without the fault gets 200), so a server that refuses everything
cannot pass.
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSIGHT = Path(os.environ.get("INSIGHT_DIR") or ROOT / "insight")   # override: mutation checks run a patched copy
FIXTURE = INSIGHT / "test" / "fixture"
NODE = shutil.which("node")
TOKEN = "test-token-" + os.urandom(12).hex()

# Insight makes no outbound call and never reads Claude Code credentials: any non-loopback URL, a credentials file
# or an API host in its source is a regression. (www.w3.org is the SVG namespace string, not a request; a host under
# the reserved .example TLD - e.g. a forged Origin in the security probe - can never resolve.)
FORBIDDEN = re.compile(
    r"https?://(?!127\.0\.0\.1[:/]|localhost[:/'\"]|www\.w3\.org/|[a-z0-9.-]*\.example\b)"
    r"|credentials\.json|api\.[a-z0-9-]+\.com",
    re.IGNORECASE,
)
BINARY_EXT = {".png", ".jpg", ".jpeg", ".webp", ".woff2", ".ico"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scan_tree(root):
    """Every (relative path, reason) in `root` whose file NAME or text matches FORBIDDEN."""
    hits = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if FORBIDDEN.search(rel):
            hits.append((rel, "file name"))
        if p.suffix.lower() in BINARY_EXT:
            continue
        m = FORBIDDEN.search(p.read_text(encoding="utf-8", errors="replace"))
        if m:
            hits.append((rel, "text: " + m.group(0)))
    return hits


def write_session(path):
    """A small synthetic transcript: a persona load, a prompt, two memory reads, one failed tool call."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    base = {"timestamp": ts, "sessionId": "sess-1", "cwd": "/work/proj"}
    persona = ("# AGENT TIER 0 - part 1/1 (tier 0, loaded at session start)\n\n===== IDENTITY =====\nx\n"
               "===== SOUL - how =====\nx\n===== TEAM - who =====\nx\n===== TRIGGERS - q =====\nx\n"
               "===== PROJECTS =====\nx\n")
    usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0}

    def tool(mid, tid, name, inp):
        return {**base, "type": "assistant", "message": {"id": mid, "model": "claude-sonnet-5", "usage": usage,
                                                         "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}

    lines = [
        {**base, "type": "attachment", "attachment": {"type": "hook_success", "hookEvent": "SessionStart", "content": persona}},
        {**base, "type": "user", "message": {"role": "user", "content": "please read the notes"}},
        tool("m1", "t1", "Read", {"file_path": "/work/proj/memory/topics/x.md"}),
        {**base, "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        tool("m2", "t2", "Read", {"file_path": "/work/proj/agent/SOUL.md"}),
        {**base, "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        tool("m3", "t3", "Bash", {"command": "false", "description": "fails on purpose"}),
        {**base, "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "exit 1"}]}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def node_eval(code, *args, env=None):
    """Run an ES-module snippet under node; returns parsed JSON from stdout."""
    r = subprocess.run([NODE, "--input-type=module", "-e", code, *args], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30, env={**os.environ, **(env or {})})
    if r.returncode != 0:
        raise AssertionError("node failed: " + r.stderr[-800:])
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "node not found on PATH - Insight tests need Node 18+")
class InsightServer(unittest.TestCase):
    proc = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="insight-test-")
        cls.projects = Path(cls.tmp) / "projects"
        cls.projects.mkdir()
        write_session(cls.projects / "-work-proj" / "sess-1.jsonl")
        cls.port = free_port()
        cls.log = open(Path(cls.tmp) / "server.log", "wb")
        cls.proc = subprocess.Popen(
            [NODE, str(INSIGHT / "server.mjs"), "--port", str(cls.port), "--repo", str(FIXTURE),
             "--projects", str(cls.projects)],
            stdin=subprocess.DEVNULL, stdout=cls.log, stderr=subprocess.STDOUT, cwd=cls.tmp,
            env={**os.environ, "INSIGHT_TOKEN": TOKEN})      # a pinned token is for TESTS only
        deadline = time.time() + 20
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("server exited early: " + cls.server_log())
            try:
                if cls.get("/")[0] == 200:
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("server did not answer within 20 s: " + cls.server_log())

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.proc and cls.proc.poll() is None:
                cls.proc.terminate()
                try:
                    cls.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cls.proc.kill()
                    cls.proc.wait(timeout=5)
        finally:
            cls.log.close()
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def server_log(cls):
        cls.log.flush()
        return (Path(cls.tmp) / "server.log").read_text(errors="replace")[-1500:]

    @classmethod
    def request(cls, path, headers=None, method="GET", body=None, auth=True, read=True):
        """(status, response headers, body bytes). `path` is sent verbatim (no client-side normalisation).
        auth=True adds `Authorization: Bearer <token>` unless the caller set its own Authorization/Cookie.
        read=False returns b"" without reading the body (for the endless /api/stream)."""
        hdrs = dict(headers or {})
        if auth and "Authorization" not in hdrs and "Cookie" not in hdrs:
            hdrs["Authorization"] = "Bearer " + TOKEN
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host="Host" in hdrs)
            for k, v in hdrs.items():
                c.putheader(k, v)
            if body is not None:
                c.putheader("Content-Length", str(len(body)))
            c.endheaders(body)
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, (r.read() if read else b"")
        finally:
            c.close()

    @classmethod
    def get(cls, path, headers=None, method="GET", body=None, auth=True):
        """(status, body bytes) - see request()."""
        status, _, data = cls.request(path, headers, method, body, auth)
        return status, data

    # ---------------------------------------------------------------- the page and the API

    def test_page_and_assets_serve(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>INSIGHT</title>", body)
        for asset in ("/app.js", "/graph.js", "/agent.js", "/lesson.js", "/style.css", "/assets/ring_core.png"):
            self.assertEqual(self.get(asset)[0], 200, asset)
        # everything the page loads comes from this server: no CDN script, style or font
        self.assertNotRegex(body, rb"(?i)<(script|link)[^>]+(src|href)=[\"']?(https?:)?//")

    def test_state_is_json_with_fixture_lessons_and_agent_name(self):
        status, body = self.get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["agent"]["name"], "Fixturebot")          # from the fixture's IDENTITY.md
        lessons = self.lessons_state()
        keys = [r["key"] for r in lessons["recent"]]
        # newest `created` first; broken-no-trigger (the tool reports errors for it) is hidden
        self.assertEqual(keys, ["comment-after-values", "read-the-live-value", "verify-the-negative-case"])
        by_key = {r["key"]: r for r in lessons["recent"]}
        self.assertEqual(by_key["verify-the-negative-case"]["trigger"]["where"], "tier 0")
        self.assertEqual(by_key["read-the-live-value"]["trigger"]["where"], "on demand")   # tier0: false
        # `tier0: false   # comment` must read as FALSE (the old JS parser read it as true)
        self.assertEqual(by_key["comment-after-values"]["trigger"]["where"], "on demand")
        self.assertEqual(by_key["comment-after-values"]["status"], "active")               # comment not kept in the value
        self.assertEqual(lessons["total"], 3)
        self.assertEqual(lessons["hidden"], 2)   # the broken one and the superseded one
        self.assertEqual(lessons["tier0"], 1)
        self.assertEqual(lessons["withoutTrigger"], 0)
        # the fixture has no .claude/hooks/load-persona.sh: shown as "not found", never a crash
        self.assertFalse(state["tier0"]["available"])
        self.assertEqual(state["tier0"]["note"], "loader not found")
        # removed features are gone from the state
        for gone in ("plan", "voice"):
            self.assertNotIn(gone, state)
        self.assertTrue(all("say" not in a for a in state["alerts"]))

    def lessons_state(self):
        """The lessons block of /api/state once the (asynchronous) tool call has answered."""
        deadline = time.time() + 20
        while time.time() < deadline:
            lessons = json.loads(self.get("/api/state")[1])["lessons"]
            if not lessons.get("loading"):
                return lessons
            time.sleep(0.3)
        self.fail("lessons never finished loading: " + self.server_log())

    def test_transcript_is_folded_into_the_state(self):
        # a regression guard: an exception inside refreshAll() is swallowed and shows up only as "no session"
        state = json.loads(self.get("/api/state")[1])
        self.assertEqual(state["session"]["id"], "sess-1")
        self.assertEqual(state["session"]["turns"], 1)
        counts = {t["name"]: t["count"] for t in state["tools"]["counts"]}
        self.assertEqual(counts, {"Read": 2, "Bash": 1})
        self.assertEqual(state["errors"]["count"], 1)
        tiers = {t["tier"]: t for t in state["memory"]["tiers"]}
        self.assertEqual(tiers[2]["read"], 1)                           # memory/topics/x.md
        self.assertEqual(tiers[0]["read"], 1)                           # agent/SOUL.md
        self.assertEqual(tiers[0]["load"], 1)                           # the SessionStart persona payload
        self.assertEqual(tiers[1]["total"], 0)                          # negative control: nothing touched tier 1
        self.assertEqual(tiers[3]["total"], 0)
        self.assertEqual(state["memory"]["identity"]["status"]["level"], "ok")
        self.assertNotIn("identity.missing", [a["id"] for a in state["alerts"]])
        self.assertEqual(state["sessions"][0]["project"], "-work-proj")

    def test_lesson_detail_by_slug(self):
        status, body = self.get("/api/lesson/verify-the-negative-case")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["trigger"], "What would this check show if the thing were broken?")
        self.assertIn("Feed it the bad input", d["body"])
        self.assertNotIn("---", d["body"])                              # the frontmatter block is cut off
        self.assertTrue(d["tier0"])
        c = json.loads(self.get("/api/lesson/comment-after-values")[1])
        self.assertFalse(c["tier0"])
        self.assertEqual(c["tags"], ["parsing", "yaml"])                # block-list tags survive
        self.assertEqual(self.get("/api/lesson/broken-no-trigger")[0], 404)     # hidden: the tool reported errors
        self.assertEqual(self.get("/api/lesson/old-superseded-lesson")[0], 404)  # hidden: superseded
        # negatives: unknown slug, numeric legacy key shape, traversal shapes, over-long, upper-case
        self.assertEqual(self.get("/api/lesson/no-such-lesson")[0], 404)
        self.assertEqual(self.get("/api/lesson/..%2f..%2fREADME")[0], 404)
        self.assertEqual(self.get("/api/lesson/" + "a" * 65)[0], 404)
        self.assertEqual(self.get("/api/lesson/Verify-The-Negative-Case")[0], 404)

    # ---------------------------------------------------------------- hardening

    def test_forged_host_is_refused(self):
        self.assertEqual(self.get("/api/state", {"Host": f"127.0.0.1:{self.port}"})[0], 200)    # control
        self.assertEqual(self.get("/api/state", {"Host": f"localhost:{self.port}"})[0], 200)    # control
        self.assertEqual(self.get("/api/state", {"Host": f"evil.example:{self.port}"})[0], 403)
        self.assertEqual(self.get("/api/state", {"Host": "127.0.0.1:1"})[0], 403)
        self.assertEqual(self.get("/", {"Host": f"evil.example:{self.port}"})[0], 403)          # pages too
        self.assertEqual(self.get("/api/lesson/verify-the-negative-case", {"Host": f"evil.example:{self.port}"})[0], 403)

    def test_cross_site_api_is_refused_but_static_stays_linkable(self):
        self.assertEqual(self.get("/api/state", {"Sec-Fetch-Site": "same-origin"})[0], 200)     # control
        self.assertEqual(self.get("/api/state", {"Sec-Fetch-Site": "none"})[0], 200)            # typed URL
        self.assertEqual(self.get("/api/state", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.get("/api/state", {"Sec-Fetch-Site": "same-site"})[0], 403)
        self.assertEqual(self.get("/api/state", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.get("/api/state", {"Origin": "null"})[0], 403)
        self.assertEqual(self.get("/api/state", {"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)   # control
        self.assertEqual(self.get("/api/lesson/verify-the-negative-case", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.get("/", {"Sec-Fetch-Site": "cross-site"})[0], 200)               # static is linkable

    def test_removed_routes_are_404(self):
        self.assertEqual(self.get("/api/say?t=hi")[0], 404)
        # no endpoint writes to disk: an upload-shaped POST to an unknown API route is simply not found
        self.assertEqual(self.get("/api/upload", method="POST", body=b"\x89PNG\r\n\x1a\nxxxxxxxx")[0], 404)
        self.assertEqual(self.get("/no-such-file.js")[0], 404)
        self.assertEqual(self.get("/assets/no-such-image.jpg")[0], 404)
        # a cross-site hit on an unknown API route is refused BEFORE routing (403, not 404)
        self.assertEqual(self.get("/api/no-such-route?t=hi", {"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_static_confinement(self):
        # sibling directory sharing the prefix, parent directory, and an absolute-looking path
        self.assertEqual(self.get("/..%2Fpublic-zz%2Fx")[0], 403)
        self.assertEqual(self.get("/..%2Fserver.mjs")[0], 403)
        self.assertEqual(self.get("/%2e%2e%2f%2e%2e%2fREADME.md")[0], 403)
        self.assertEqual(self.get("/..%5Cserver.mjs")[0] in (403, 404), True)   # backslash: never a 200
        # control: an in-tree file under the same prefix works, a missing in-tree file is 404 not 403
        self.assertEqual(self.get("/assets/corner.png")[0], 200)
        self.assertEqual(self.get("/assets/nope.png")[0], 404)

    def test_malformed_escape_does_not_crash_the_server(self):
        self.assertEqual(self.get("/%E0%A4%A")[0], 400)
        self.assertEqual(self.get("/a%00b")[0], 400)
        self.assertIsNone(self.proc.poll(), "server died on a malformed request: " + self.server_log())
        self.assertEqual(self.get("/api/state")[0], 200)               # still serving afterwards

    def test_binds_loopback_only(self):
        out = self.server_log()
        self.assertIn(f"http://127.0.0.1:{self.port}/?token={TOKEN}", out)       # the URL carries the token
        self.assertIn("loopback only", out)
        self.assertIn("agent repo:", out)
        self.assertNotIn("plan usage", out)

    # ---------------------------------------------------------------- token auth

    def test_api_requires_the_token(self):
        for path in ("/api/state", "/api/stream", "/api/lesson/verify-the-negative-case", "/api/agent/x"):
            self.assertEqual(self.request(path, auth=False, read=False)[0], 401, path + " without a token")
            self.assertEqual(self.request(path, {"Authorization": "Bearer " + TOKEN[:-1] + "x"}, read=False)[0], 401, path + " wrong token")
            self.assertEqual(self.request(path, {"Cookie": "insight=" + TOKEN + "zz"}, read=False)[0], 401, path + " long cookie")
        # a token of a different length (short, empty, huge) is 401 and does not crash the server
        for bad in ("a", "", "x" * 5000, TOKEN[: len(TOKEN) // 2]):
            self.assertEqual(self.request("/api/state", {"Authorization": "Bearer " + bad})[0], 401, repr(bad[:10]))
            self.assertEqual(self.request("/api/state", {"Cookie": "insight=" + bad})[0], 401, repr(bad[:10]))
        self.assertEqual(self.request("/api/state", {"Authorization": TOKEN})[0], 401)         # not the Bearer scheme
        self.assertEqual(self.request("/api/state", {"Authorization": "bearer " + TOKEN})[0], 401)
        self.assertEqual(self.request("/api/state?token=" + TOKEN, auth=False)[0], 401)        # a query token does not count on the API
        self.assertIsNone(self.proc.poll(), "server died on odd tokens: " + self.server_log())

    def test_bearer_and_cookie_are_accepted(self):
        self.assertEqual(self.request("/api/state", {"Authorization": "Bearer " + TOKEN})[0], 200)
        self.assertEqual(self.request("/api/state", {"Cookie": "other=1; insight=" + TOKEN + "; x=2"})[0], 200)
        self.assertEqual(self.request("/api/stream", {"Authorization": "Bearer " + TOKEN}, read=False)[0], 200)

    def test_token_url_sets_cookie_and_redirects(self):
        status, h, _ = self.request("/?token=" + TOKEN, auth=False)
        self.assertEqual(status, 302)
        self.assertEqual(h["location"], "/")                            # the token leaves the address bar
        cookie = h["set-cookie"]
        self.assertEqual(cookie, f"insight={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
        # the cookie it issued opens the API (the browser flow end to end)
        self.assertEqual(self.request("/api/state", {"Cookie": cookie.split(";")[0]}, auth=False)[0], 200)
        # a wrong token: no cookie, no redirect, plain page, and the API stays closed
        status, h, body = self.request("/?token=" + TOKEN[:-1] + "x", auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn("set-cookie", h)
        self.assertIn(b"<title>INSIGHT</title>", body)
        status, h, _ = self.request("/?token=", auth=False)
        self.assertEqual((status, "set-cookie" in h), (200, False))

    def test_static_files_stay_public(self):
        for p in ("/", "/app.js", "/style.css", "/assets/corner.png"):
            self.assertEqual(self.request(p, auth=False)[0], 200, p)

    def test_token_is_not_in_any_file_the_server_writes(self):
        # the server writes nothing under the repo or the projects dir
        for root in (FIXTURE, self.projects):
            for p in Path(root).rglob("*"):
                if p.is_file():
                    self.assertNotIn(TOKEN.encode(), p.read_bytes(), str(p))

    # ---------------------------------------------------------------- response headers

    def test_security_headers_on_every_kind_of_response(self):
        want = {
            "content-security-policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            "x-frame-options": "DENY",
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
        }
        cases = [("/", None, 200), ("/app.js", None, 200), ("/api/state", None, 200), ("/api/state", {"Authorization": "Bearer no"}, 401),
                 ("/nope", None, 404), ("/%E0%A4%A", None, 400), ("/..%2Fserver.mjs", None, 403),
                 ("/api/state", {"Host": "evil.example:1"}, 403), ("/?token=" + TOKEN, {"Authorization": "x"}, 302)]
        for path, hdrs, code in cases:
            status, h, _ = self.request(path, hdrs, auth=hdrs is None)
            self.assertEqual(status, code, path)
            for k, v in want.items():
                self.assertEqual(h.get(k), v, f"{k} on {path} ({status})")

    def test_page_is_csp_clean(self):
        # nothing the policy blocks: no inline script/style, no on* handlers, no javascript: URLs, no remote origins
        pub = INSIGHT / "public"
        html = (pub / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [])
        self.assertNotIn("<style", html)
        self.assertEqual(re.findall(r"\sstyle\s*=", html), [])
        self.assertEqual(re.findall(r"\son[a-z]+\s*=", html), [])
        self.assertNotIn("javascript:", html)
        for js in pub.glob("*.js"):
            src = js.read_text(encoding="utf-8")
            for bad in ("innerHTML", "insertAdjacentHTML", "eval(", "new Function", "setAttribute('style'", 'setAttribute("style"',
                        "cssText", "document.write", "setAttribute('on", "srcdoc"):
                self.assertNotIn(bad, src, f"{js.name}: {bad}")
        css = (pub / "style.css").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"url\(\s*['\"]?https?:", css), [])
        self.assertNotIn("@import", css)


@unittest.skipUnless(NODE, "node not found on PATH")
class MemtierUnits(unittest.TestCase):
    MEMTIER = (INSIGHT / "lib" / "memtier.mjs").as_uri()
    TEAM = (INSIGHT / "lib" / "team.mjs").as_uri()
    LESSONS = (INSIGHT / "lib" / "lessons.mjs").as_uri()

    def test_persona_parts_from_settings(self):
        settings = json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/load-persona.sh" 1 5'},
            {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/load-persona.sh" 5 12'},
            {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/tools/memory_index.py"'}]}]},
            "permissions": {"allow": ["Bash(bash .claude/hooks/load-persona.sh check:*)"]}})
        code = ("const {personaPartsFromSettings: f} = await import(process.argv[1]); "
                "console.log(JSON.stringify([f(process.argv[2]), f('{}'), f('not json'), f(process.argv[3])]))")
        only_check = json.dumps({"x": "bash .claude/hooks/load-persona.sh check 3"})        # `check 3` is not `K N`
        self.assertEqual(node_eval(code, self.MEMTIER, settings, only_check), [12, None, None, None])

    def test_role_definitions_are_looked_up_in_the_repo_first(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".claude" / "agents").mkdir(parents=True)
            (Path(d) / ".claude" / "agents" / "zz-repo-only-role.md").write_text("x")
            code = ("const t = await import(process.argv[1]); "
                    "const before = t.roleDefined('zz-repo-only-role'); t.setRepo(process.argv[2]); "
                    "console.log(JSON.stringify([before, t.roleDefined('zz-repo-only-role'), t.roleDefined('zz-no-such-role'), "
                    "t.roleDefined('../zz-repo-only-role'), t.roleDefined(null)]))")
            self.assertEqual(node_eval(code, self.TEAM, d), [False, True, False, False, False])

    def test_classify_new_layout(self):
        paths = ["/work/proj/agent/SOUL.md", "/work/proj/memory/_gen/TRIGGERS.md", "/work/proj/memory/topics/x.md",
                 "/work/proj/memory/lessons/y.md", "/work/proj/CLAUDE.md", "/work/proj/docs/SPEC.md",
                 "/work/proj/.claude/agents/reviewer.md", "/work/proj/agents-src/CORE-AGENTS.md",
                 "/work/proj/.claude/agent-memory/reviewer/MEMORY.md",
                 "/home/<user>/.claude/projects/-work-proj/memory/MEMORY.md",
                 "memory/topics/relative.md", "/work/proj/src/main.py", "/work/proj/agent/notes.txt"]
        code = ("const {classify} = await import(process.argv[1]); "
                "console.log(JSON.stringify(process.argv.slice(2).map(p => { const c = classify(p); return c ? c.tier : null; })))")
        tiers = node_eval(code, self.MEMTIER, *paths)
        self.assertEqual(tiers, [0, 0, 2, 2, 1, 1, 3, 3, 3, 2, 2, None, None])

    def test_tier0_check_table_parser(self):
        real = ("tier 0: 6430 bytes of sections -> 1 parts needed, 6 hooks configured, cap 9000 bytes/part\n"
                "  part 1:  7175 bytes  OK         IDENTITY | SOUL | TEAM\n"
                "  part 2:   104 bytes  OK         (empty)\n"
                "  part 3:  9500 bytes  OVER CAP   PROJECTS\n"
                "  part 4:   104 bytes  NOT LOADED ROUTING\n")
        code = ("const {parseTier0} = await import(process.argv[1]); "
                "console.log(JSON.stringify([parseTier0(process.argv[2]), parseTier0('no table here\\nat all')]))")
        good, junk = node_eval(code, self.MEMTIER, real)
        self.assertEqual([(p["k"], p["bytes"], p["flag"]) for p in good],
                         [(1, 7175, "OK"), (3, 9500, "OVER CAP"), (4, 104, "NOT LOADED")])   # "(empty)" spare dropped
        self.assertEqual(good[0]["titles"], "IDENTITY | SOUL | TEAM")
        self.assertEqual(junk, [])

    def test_persona_load_is_recognised_and_garbage_is_not(self):
        good = ("# AGENT TIER 0 - part 1/1 (tier 0, loaded at session start)\n\n"
                "===== IDENTITY =====\nx\n===== SOUL - how =====\nx\n===== TEAM - who =====\nx\n"
                "===== TRIGGERS - q =====\nx\n===== PROJECTS =====\nx\n")
        code = ("const m = await import(process.argv[1]); const mem = m.empty(); "
                "const a = m.recordHookLoad(mem, {at: 1, content: process.argv[2]}); "
                "const b = m.recordHookLoad(m.empty(), {at: 1, content: 'hello world, part 1/6 of nothing'}); "
                "console.log(JSON.stringify({a, b, status: m.identityStatus(mem), load: mem.tiers[0].load}))")
        r = node_eval(code, self.MEMTIER, good)
        self.assertTrue(r["a"])
        self.assertFalse(r["b"])                       # negative control: an unrelated hook output
        self.assertEqual(r["load"], 1)
        self.assertEqual(r["status"]["level"], "ok")
        # a payload missing a section is critical, not ok
        short = "# AGENT TIER 0 - part 1/1 (x)\n===== IDENTITY =====\nx\n"
        code2 = ("const m = await import(process.argv[1]); const mem = m.empty(); "
                 "m.recordHookLoad(mem, {at: 1, content: process.argv[2]}); "
                 "console.log(JSON.stringify(m.identityStatus(mem)))")
        self.assertEqual(node_eval(code2, self.MEMTIER, short)["level"], "critical")
        # one of six parts arriving is critical too (a lost part), with the count in the text
        lost = good.replace("part 1/1", "part 1/6")
        st = node_eval(code2, self.MEMTIER, lost)
        self.assertEqual(st["level"], "critical")
        self.assertIn("only 1 of 6", st["text"])

    def test_body_of_cuts_the_frontmatter(self):
        code = ("const {bodyOf} = await import(process.argv[1]); "
                "console.log(JSON.stringify([bodyOf('---\\nname: a\\n---   \\nthe body\\n'), bodyOf('---\\r\\nx: 1\\r\\n---\\r\\nB\\r\\n'), "
                "bodyOf('no frontmatter here'), bodyOf('---\\nnever closed\\n')]))")
        r = node_eval(code, self.LESSONS)
        self.assertEqual(r[0], "the body")                              # closing `---` with trailing spaces is recognised
        self.assertEqual(r[1], "B")                                     # CRLF
        self.assertEqual(r[2], "no frontmatter here")
        self.assertIn("never closed", r[3])

    def test_python_candidates_honour_agent_python(self):
        code = "const {pythonCandidates: p} = await import(process.argv[1]); console.log(JSON.stringify([p({}), p({AGENT_PYTHON: 'py-x'})]))"
        self.assertEqual(node_eval(code, self.LESSONS), [["python3", "python"], ["py-x"]])

    def test_lessons_unavailable_is_an_error_value_not_a_crash(self):
        code = ("const m = await import(process.argv[1]); m.setRepo(process.argv[2]); const r = await m.fetchLessons(process.argv[2]); "
                "console.log(JSON.stringify({r, sum: m.summary(), one: await m.one('verify-the-negative-case')}))")
        # python missing
        r = node_eval(code, self.LESSONS, str(FIXTURE), env={"AGENT_PYTHON": "definitely-not-a-python-xyz"})
        self.assertEqual(r["r"], {"error": "lessons unavailable: definitely-not-a-python-xyz not found"})
        self.assertTrue(r["sum"].get("loading") or "lessons unavailable" in r["sum"].get("error", ""), r["sum"])   # first call may still be loading
        self.assertIsNone(r["one"])
        # the "python" is node itself: it exits non-zero on the .py file - reported, not raised
        r = node_eval(code, self.LESSONS, str(FIXTURE), env={"AGENT_PYTHON": NODE})
        self.assertTrue(r["r"]["error"].startswith("lessons unavailable: "), r)
        self.assertIn("failed", r["r"]["error"])
        self.assertIsNone(r["one"])

    def test_real_tool_gives_the_lessons_the_tool_parser_sees(self):
        code = ("const m = await import(process.argv[1]); m.setRepo(process.argv[2]); const r = await m.fetchLessons(process.argv[2]); "
                "console.log(JSON.stringify(r))")
        r = node_eval(code, self.LESSONS, str(FIXTURE))
        self.assertNotIn("error", r, r)
        by = {x["name"]: x for x in r["lessons"]}
        self.assertEqual(set(by), {"verify-the-negative-case", "read-the-live-value", "comment-after-values", "broken-no-trigger", "old-superseded-lesson"})
        self.assertIs(by["comment-after-values"]["tier0"], False)
        self.assertEqual(by["comment-after-values"]["status"], "active")
        self.assertEqual(by["comment-after-values"]["tags"], ["parsing", "yaml"])
        self.assertEqual(by["verify-the-negative-case"]["errors"], [])          # positive control: a clean lesson has none
        self.assertTrue(by["broken-no-trigger"]["errors"])
        self.assertEqual(by["comment-after-values"]["file"], "lessons/comment-after-values.md")

    def test_tool_failure_class_is_shown_and_file_paths_are_confined(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tools").mkdir()
            (Path(d) / "tools" / "memory_index.py").write_text(
                'import json\nprint(json.dumps({"lessons": [], "error": "KeyError"}))\n', newline="\n")
            code = ("const m = await import(process.argv[1]); m.setRepo(process.argv[2]); "
                    "console.log(JSON.stringify(await m.fetchLessons(process.argv[2])))")
            self.assertEqual(node_eval(code, self.LESSONS, d), {"error": "lessons unavailable: KeyError"})
        code = ("const m = await import(process.argv[1]); const r = process.argv[2]; "
                "console.log(JSON.stringify(['lessons/a.md', 'lessons/sub/a.md', '../secret.md', 'lessons/../../x.md', "
                "'lessons/a.txt', '', null, 'lessons', '/etc/passwd', 'topics/a.md'].map((f) => m.lessonPath(r, f) ? 'in' : 'out')))")
        self.assertEqual(node_eval(code, self.LESSONS, str(FIXTURE)),
                         ["in", "in", "out", "out", "out", "out", "out", "out", "out", "out"])


@unittest.skipUnless(NODE and shutil.which("bash"), "node and bash are needed")
class TierLoaderWiring(unittest.TestCase):
    """The N handed to `load-persona.sh check N` really comes from .claude/settings.json (a fake loader echoes its args)."""

    def _tier0_titles(self, settings_text):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            (repo / ".claude" / "hooks").mkdir(parents=True)
            (repo / "agent").mkdir()
            if settings_text is not None:
                (repo / ".claude" / "settings.json").write_text(settings_text)
            (repo / ".claude" / "hooks" / "load-persona.sh").write_text(
                '#!/usr/bin/env bash\necho "tier 0: 1 bytes of sections"\necho "  part 1:    10 bytes  OK         ARGS=$*"\n', newline="\n")
            projects = Path(d) / "projects"
            projects.mkdir()
            port, token = free_port(), "wiring-token-" + os.urandom(8).hex()
            proc = subprocess.Popen([NODE, str(INSIGHT / "server.mjs"), "--port", str(port), "--repo", str(repo),
                                     "--projects", str(projects)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=d, env={**os.environ, "INSIGHT_TOKEN": token})
            try:
                deadline = time.time() + 25
                while time.time() < deadline:
                    try:
                        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        c.request("GET", "/api/state", headers={"Authorization": "Bearer " + token})
                        t0 = json.loads(c.getresponse().read())["tier0"]
                        c.close()
                        if t0.get("available"):
                            return t0["parts"][0]["titles"]
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(0.3)
                self.fail("tier0 never became available")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_n_comes_from_settings_json(self):
        hook = lambda k, n: {"type": "command", "command": f'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/load-persona.sh" {k} {n}'}
        settings = json.dumps({"hooks": {"SessionStart": [{"hooks": [hook(1, 3), hook(2, 3), hook(3, 3)]}]}})
        self.assertEqual(self._tier0_titles(settings), "ARGS=check 3")
        self.assertEqual(self._tier0_titles(None), "ARGS=check 8")                  # no settings.json -> default 8
        self.assertEqual(self._tier0_titles("{ not json"), "ARGS=check 8")          # unreadable -> default 8


@unittest.skipUnless(NODE, "node not found on PATH")
class ServerArguments(unittest.TestCase):
    def _run(self, *argv):
        r = subprocess.run([NODE, str(INSIGHT / "server.mjs"), *argv], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=20)
        return r.returncode, r.stderr

    def test_bad_ports_exit_64_with_usage(self):
        for bad in ("abc", "80", "1023", "65536", "70000", "1e3", "7717.5", "-1", "", "0"):
            rc, err = self._run("--port", bad, "--projects", tempfile.gettempdir())
            self.assertEqual(rc, 64, f"--port {bad!r}: {err}")
            self.assertIn("usage:", err)
        rc, err = self._run("--port")                                  # flag with no value
        self.assertEqual(rc, 64, err)

    def test_valid_port_boundaries_are_accepted(self):
        # control: the validator does not reject everything. Port 1024 / 65535 start (or fail to bind), never exit 64.
        for ok in ("1024", "65535"):
            with tempfile.TemporaryDirectory() as d:
                proc = subprocess.Popen([NODE, str(INSIGHT / "server.mjs"), "--port", ok, "--projects", d,
                                         "--repo", str(FIXTURE)], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    time.sleep(2.5)
                    self.assertNotEqual(proc.poll(), 64, f"--port {ok} was rejected")
                finally:
                    proc.kill()
                    proc.wait(timeout=5)


@unittest.skipUnless(NODE, "node not found on PATH")
class TailFollowing(unittest.TestCase):
    """Lines appended to a live transcript are folded exactly once (no double fold, no loss)."""

    def test_appended_lines_are_counted_once(self):
        with tempfile.TemporaryDirectory() as d:
            projects = Path(d) / "projects"
            session = projects / "-work-proj" / "sess-1.jsonl"
            write_session(session)
            port, token = free_port(), "tail-token-" + os.urandom(8).hex()
            proc = subprocess.Popen([NODE, str(INSIGHT / "server.mjs"), "--port", str(port), "--repo", str(FIXTURE),
                                     "--projects", str(projects)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=d, env={**os.environ, "INSIGHT_TOKEN": token})

            def counts():
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    c.request("GET", "/api/state", headers={"Authorization": "Bearer " + token})
                    s = json.loads(c.getresponse().read())
                finally:
                    c.close()
                return {t["name"]: t["count"] for t in s["tools"]["counts"]}

            try:
                deadline = time.time() + 25
                while time.time() < deadline:
                    try:
                        if counts().get("Read") == 2:
                            break
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(0.3)
                else:
                    self.fail("server never folded the initial transcript")
                # append in two pieces, the second completing a line that was half written when first seen
                extra = json.dumps({"type": "assistant", "timestamp": "2026-10-22T12:00:00Z", "sessionId": "sess-1",
                                    "message": {"id": "m9", "model": "claude-sonnet-5", "usage": {"input_tokens": 1, "output_tokens": 1},
                                                "content": [{"type": "tool_use", "id": "t9", "name": "Grep", "input": {"pattern": "x"}}]}}) + "\n"
                with open(session, "a", encoding="utf-8", newline="\n") as f:
                    f.write(extra[:40])
                    f.flush()
                    time.sleep(1.5)
                    self.assertNotIn("Grep", counts())                 # a partial line is not folded
                    f.write(extra[40:])
                deadline = time.time() + 15
                while time.time() < deadline and counts().get("Grep") != 1:
                    time.sleep(0.3)
                time.sleep(3)                                           # several more ticks and a rescan-sized wait
                final = counts()
                self.assertEqual(final.get("Grep"), 1)                  # exactly once
                self.assertEqual(final.get("Read"), 2)                  # nothing re-folded
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


class NothingPersonal(unittest.TestCase):
    def test_no_personal_strings_under_insight(self):
        self.assertTrue(INSIGHT.is_dir())
        files = [p for p in INSIGHT.rglob("*") if p.is_file()]
        self.assertGreater(len(files), 20)               # the scan is actually looking at something
        self.assertEqual(scan_tree(INSIGHT), [])

    def test_repo_leak_scan_is_clean_for_insight(self):
        # personal names live only in the gitignored tools/leak_patterns.txt; tools/leak_scan.py applies them
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "leak_scan.py"), "--root", str(ROOT)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)          # 2 = usage error
        mine = [ln for ln in r.stdout.splitlines() if ln.startswith(("insight/", "tests/test_insight"))]
        self.assertEqual(mine, [])

    def test_scanner_catches_a_planted_string(self):       # negative control: the scan can fail
        with tempfile.TemporaryDirectory() as d:
            Path(d, "ok.txt").write_text("nothing to see\n")
            self.assertEqual(scan_tree(d), [])
            Path(d, "a.js").write_text("fetch('https://' + 'example.com/x')\n".replace("' + '", ""))
            Path(d, "b.json").write_text('{"p": "~/.claude/.credentials.json"}\n')
            Path(d, "c.md").write_text("calls api.vendor-name.com directly\n")
            Path(d, "d.js").write_text("ns = 'http://www.w3.org/2000/svg'; u = 'http://127.0.0.1:7717/'\n")
            found = {rel for rel, _ in scan_tree(d)}
            self.assertEqual(found, {"a.js", "b.json", "c.md"})          # d.js: SVG namespace + loopback are fine

    def test_start_script_present(self):
        self.assertTrue((INSIGHT / "start.sh").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
