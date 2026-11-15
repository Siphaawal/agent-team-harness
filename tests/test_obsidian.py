#!/usr/bin/env python3
"""Tests for tools/obsidian_vault.py. python3 stdlib only; never installs or launches Obsidian.

    python3 tests/test_obsidian.py

Every test works on a throwaway repo (a copy of the tool + tools/common.py next to its own memory/ and agent/) and a
throwaway vault and app-config dir, all under one temp dir; AGENT_VAULT_DIR / AGENT_OBSIDIAN_APP_DIR are pinned there
as well, so no test can reach a real vault or a real Obsidian config even when a flag is forgotten.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_SRC = ROOT / "tools" / "obsidian_vault.py"
COMMON_SRC = ROOT / "tools" / "common.py"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class VaultTool(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vault-test-"))
        self.repo = self.tmp / "repo"
        (self.repo / "tools").mkdir(parents=True)
        shutil.copy2(TOOL_SRC, self.repo / "tools")
        shutil.copy2(COMMON_SRC, self.repo / "tools")
        (self.repo / "memory" / "topics").mkdir(parents=True)
        (self.repo / "memory" / "topics" / "team-rule.md").write_text("---\nname: team-rule\n---\nkeep me\n", encoding="utf-8")
        (self.repo / "agent").mkdir()
        (self.repo / "agent" / "IDENTITY.md").write_text("# id\n", encoding="utf-8")
        self.vault = self.tmp / "data" / "vault"
        self.app = self.tmp / "config" / "obsidian"
        self.env = {**os.environ, "AGENT_VAULT_DIR": str(self.tmp / "env-vault"),
                    "AGENT_OBSIDIAN_APP_DIR": str(self.tmp / "env-app")}
        self.env.pop("AGENT_TOOLS_ANY_ROOT", None)

    def tearDown(self):
        # remove the links first and without following them: rmtree must never walk into the repo's memory
        for v in (self.vault, self.tmp / "env-vault"):
            for name in ("memory", "agent"):
                p = v / name
                if os.path.islink(p):
                    os.unlink(p)
                elif p.is_dir() and not (p / ".keep-real").exists():
                    try:
                        os.rmdir(p)          # a junction; a real dir that is not empty raises - left to rmtree
                    except OSError:
                        pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, *argv, env=None):
        return subprocess.run([sys.executable, "-I", str(self.repo / "tools" / "obsidian_vault.py"), *argv],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
                              env=env or self.env, cwd=str(self.tmp))

    def setup_vault(self):
        r = self.run_tool("setup", "--vault", str(self.vault), "--app-dir", str(self.app))
        if r.returncode != 0 and "could not create a symlink or a junction" in r.stderr:
            self.skipTest("this OS refused both a directory symlink and a junction: " + r.stderr.strip())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    # ---------------------------------------------------------------- setup

    def test_setup_creates_a_locked_down_vault(self):
        r = self.setup_vault()
        cfg = self.vault / ".obsidian"
        core = json.loads((cfg / "core-plugins.json").read_text())
        self.assertIs(core["sync"], False)
        self.assertIs(core["publish"], False)
        self.assertIs(core["webviewer"], False)
        self.assertIs(core["graph"], True)                                           # positive control
        self.assertEqual(json.loads((cfg / "community-plugins.json").read_text()), [])
        app = json.loads((cfg / "app.json").read_text())
        self.assertIs(app["alwaysUpdateLinks"], False)
        self.assertIs(app["promptDelete"], True)
        # the links resolve to the repo's own dirs
        for name in ("memory", "agent"):
            self.assertEqual(os.path.realpath(self.vault / name), os.path.realpath(self.repo / name))
        self.assertTrue((self.vault / "memory" / "topics" / "team-rule.md").is_file())
        # no app config existed: created with updates off and the vault registered
        ac = json.loads((self.app / "obsidian.json").read_text())
        self.assertIs(ac["updateDisabled"], True)
        self.assertEqual([v["path"] for v in ac["vaults"].values()], [str(self.vault)])
        self.assertIn("obsidian://open?vault=vault", r.stdout)
        c = self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertTrue(c.stdout.startswith("OK "))

    def test_rerun_is_refused_and_changes_nothing(self):
        self.setup_vault()
        before = {p: sha(p) for p in (self.vault / ".obsidian").iterdir()}
        before_app = sha(self.app / "obsidian.json")
        r = self.run_tool("setup", "--vault", str(self.vault), "--app-dir", str(self.app))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("already exists", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual({p: sha(p) for p in (self.vault / ".obsidian").iterdir()}, before)
        self.assertEqual(sha(self.app / "obsidian.json"), before_app)

    def test_vault_inside_the_repo_is_refused(self):
        inside = self.repo / "vault"
        r = self.run_tool("setup", "--vault", str(inside), "--app-dir", str(self.app))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("inside the repo", r.stderr)
        self.assertFalse(inside.exists())
        self.assertFalse((self.app / "obsidian.json").exists())

    def test_existing_app_config_is_not_touched_by_setup(self):
        self.app.mkdir(parents=True)
        mine = {"vaults": {"abc": {"path": "/somewhere/else", "ts": 1}}, "frame": "hidden"}
        (self.app / "obsidian.json").write_text(json.dumps(mine))
        before = sha(self.app / "obsidian.json")
        r = self.setup_vault()
        self.assertIn("WARNING", r.stdout)
        self.assertEqual(sha(self.app / "obsidian.json"), before)
        # check reports updates ON; lock-app fixes it, keeps the other keys and a .bak
        c = self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))
        self.assertEqual(c.returncode, 1)
        self.assertIn("automatic updates are ON", c.stdout)
        L = self.run_tool("lock-app", "--app-dir", str(self.app))
        self.assertEqual(L.returncode, 0, L.stderr)
        after = json.loads((self.app / "obsidian.json").read_text())
        self.assertEqual(after, {**mine, "updateDisabled": True})
        self.assertEqual(json.loads((self.app / "obsidian.json.bak").read_text()), mine)
        self.assertEqual(self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app)).returncode, 0)

    def test_failure_half_way_leaves_nothing_and_keeps_the_memory(self):
        # the second link fails: setup must remove what it made, unlinking (never following) the first link
        driver = self.tmp / "driver.py"
        driver.write_text(
            "import sys\nsys.path.insert(0, sys.argv[1])\nimport obsidian_vault as o\n"
            "real = o.make_dir_link\ncalls = []\n"
            "def flaky(t, l):\n    calls.append(l)\n    if len(calls) == 2:\n        raise OSError('simulated')\n"
            "    return real(t, l)\n"
            "o.make_dir_link = flaky\nsys.exit(o.main(sys.argv[2:]))\n", encoding="utf-8")
        r = subprocess.run([sys.executable, "-I", str(driver), str(self.repo / "tools"), "setup", "--vault",
                            str(self.vault), "--app-dir", str(self.app)], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60, env=self.env)
        if "could not create a symlink or a junction" in r.stderr:
            self.skipTest("no directory links on this OS")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("nothing left behind", r.stderr)
        self.assertFalse(os.path.lexists(self.vault))
        self.assertEqual((self.repo / "memory" / "topics" / "team-rule.md").read_text(encoding="utf-8"),
                         "---\nname: team-rule\n---\nkeep me\n")

    def test_repo_without_memory_is_refused(self):
        shutil.rmtree(self.repo / "agent")
        r = self.run_tool("setup", "--vault", str(self.vault), "--app-dir", str(self.app))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.vault.exists())

    # ---------------------------------------------------------------- check: drift detection (the negative cases)

    def drift_after(self, mutate):
        self.setup_vault()
        self.assertEqual(self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app)).returncode, 0)
        mutate(self.vault / ".obsidian")
        return self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))

    def edit_json(self, path, **kv):
        d = json.loads(path.read_text())
        d.update(kv)
        path.write_text(json.dumps(d))

    def test_check_flags_sync_turned_on(self):
        c = self.drift_after(lambda cfg: self.edit_json(cfg / "core-plugins.json", sync=True))
        self.assertEqual(c.returncode, 1)
        self.assertIn("DRIFT .obsidian/core-plugins.json: sync is true, must be false", c.stdout)

    def test_check_flags_each_lockdown_value(self):
        cases = [("core-plugins.json", "publish", True), ("core-plugins.json", "webviewer", True),
                 ("app.json", "alwaysUpdateLinks", True), ("app.json", "promptDelete", False)]
        self.setup_vault()
        cfg = self.vault / ".obsidian"
        for fn, key, bad in cases:
            good = json.loads((cfg / fn).read_text())[key]
            self.edit_json(cfg / fn, **{key: bad})
            c = self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))
            self.assertEqual(c.returncode, 1, (fn, key))
            self.assertIn(key, c.stdout)
            self.edit_json(cfg / fn, **{key: good})
        self.assertEqual(self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app)).returncode, 0)

    def test_check_flags_community_plugins(self):
        def plant(cfg):
            (cfg / "community-plugins.json").write_text('["some-plugin"]')
            (cfg / "plugins" / "some-plugin").mkdir(parents=True)
        c = self.drift_after(plant)
        self.assertEqual(c.returncode, 1)
        self.assertIn("enables plugins", c.stdout)
        self.assertIn("plugin code", c.stdout)

    def test_check_flags_updates_and_bad_json(self):
        c = self.drift_after(lambda cfg: self.edit_json(self.app / "obsidian.json", updateDisabled=False))
        self.assertEqual(c.returncode, 1)
        self.assertIn("automatic updates are ON", c.stdout)
        (self.vault / ".obsidian" / "app.json").write_text("{not json")
        c = self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))
        self.assertIn("app.json is not valid JSON", c.stdout)
        os.unlink(self.app / "obsidian.json")
        c = self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))
        self.assertIn("first launch creates it with automatic updates ON", c.stdout)

    def test_check_flags_a_link_replaced_by_a_copy(self):
        self.setup_vault()
        link = self.vault / "agent"
        os.unlink(link) if os.path.islink(link) else os.rmdir(link)
        link.mkdir()
        (link / ".keep-real").write_text("")
        c = self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))
        self.assertEqual(c.returncode, 1)
        self.assertIn("agent is not a link", c.stdout)

    def test_check_without_a_vault_is_refused(self):
        c = self.run_tool("check", "--vault", str(self.tmp / "nothing"), "--app-dir", str(self.app))
        self.assertEqual(c.returncode, 2)

    # ---------------------------------------------------------------- check: themes and CSS snippets (remote content)

    def check_rc(self):
        return self.run_tool("check", "--vault", str(self.vault), "--app-dir", str(self.app))

    def test_check_flags_remote_css_themes_and_snippets(self):
        # CSS can @import or url() a remote host: each planted piece alone is drift
        self.setup_vault()
        cfg = self.vault / ".obsidian"
        (cfg / "themes").mkdir()
        (cfg / "snippets").mkdir()
        self.edit_json(cfg / "appearance.json", cssTheme="", enabledCssSnippets=[])
        c = self.check_rc()
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)        # control: empty folders, default values pass
        plants = [
            ("themes", lambda: ((cfg / "themes" / "Fixture").mkdir(), (cfg / "themes" / "Fixture" / "theme.css")
                                .write_text("@import url('https://example.invalid/x.css');\n")),
             lambda: shutil.rmtree(cfg / "themes" / "Fixture")),
            ("snippets", lambda: (cfg / "snippets" / "s.css").write_text("body{background:url(https://example.invalid/p.png)}\n"),
             lambda: os.unlink(cfg / "snippets" / "s.css")),
            ("cssTheme", lambda: self.edit_json(cfg / "appearance.json", cssTheme="Fixture"),
             lambda: self.edit_json(cfg / "appearance.json", cssTheme="")),
            ("enabledCssSnippets", lambda: self.edit_json(cfg / "appearance.json", enabledCssSnippets=["s"]),
             lambda: self.edit_json(cfg / "appearance.json", enabledCssSnippets=[])),
        ]
        for word, plant, undo in plants:
            plant()
            c = self.check_rc()
            self.assertEqual(c.returncode, 1, (word, c.stdout))
            self.assertIn(word, c.stdout)
            undo()
            self.assertEqual(self.check_rc().returncode, 0, word)
        (cfg / "appearance.json").write_text("[1]")
        self.assertEqual(self.check_rc().returncode, 1)

    # ---------------------------------------------------------------- failures leave nothing; unusable app config

    def test_failed_setup_removes_the_parent_dirs_it_made(self):
        deep = self.tmp / "new1" / "deep" / "vault"
        driver = self.tmp / "driver.py"
        driver.write_text("import sys\nsys.path.insert(0, sys.argv[1])\nimport obsidian_vault as o\n"
                          "def boom(t, l):\n    raise OSError('simulated')\n"
                          "o.make_dir_link = boom\nsys.exit(o.main(sys.argv[2:]))\n", encoding="utf-8")
        r = subprocess.run([sys.executable, "-I", str(driver), str(self.repo / "tools"), "setup", "--vault", str(deep),
                            "--app-dir", str(self.app)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=60, env=self.env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("nothing left behind", r.stderr)
        self.assertFalse(os.path.lexists(self.tmp / "new1"))
        self.assertTrue(self.tmp.is_dir())                                       # a parent that existed is kept

    def test_unusable_app_config_is_refused_not_a_traceback(self):
        bad = self.tmp / "bad-app"
        (bad / "obsidian.json").mkdir(parents=True)                             # a directory, not a file
        r = self.run_tool("setup", "--vault", str(self.vault), "--app-dir", str(bad))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.lexists(self.vault))                           # refused BEFORE creating anything
        self.assertFalse(os.path.lexists(self.vault.parent))
        self.setup_vault()                                                       # control: a good app dir works
        for argv in (("check", "--vault", str(self.vault), "--app-dir", str(bad)), ("lock-app", "--app-dir", str(bad))):
            r = self.run_tool(*argv)
            self.assertEqual(r.returncode, 2, (argv, r.stdout + r.stderr))
            self.assertNotIn("Traceback", r.stderr)
        self.assertTrue((bad / "obsidian.json").is_dir())

    def test_app_dir_under_a_file_is_refused_and_leaves_no_vault(self):
        afile = self.tmp / "afile"
        afile.write_text("x")
        r = self.run_tool("setup", "--vault", str(self.vault), "--app-dir", str(afile / "sub"))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.lexists(self.vault))                           # no half-made vault blocks a re-run
        self.assertEqual(afile.read_text(), "x")
        self.setup_vault()                                                       # control: the same vault path works

    def test_dangling_app_config_link_is_refused(self):
        bad = self.tmp / "bad-app"
        bad.mkdir()
        try:
            os.symlink(str(self.tmp / "gone.json"), str(bad / "obsidian.json"))
        except (OSError, NotImplementedError) as e:
            self.skipTest("cannot create a symlink here (%s)" % type(e).__name__)
        r = self.run_tool("setup", "--vault", str(self.vault), "--app-dir", str(bad))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(os.path.lexists(self.vault))
        r = self.run_tool("lock-app", "--app-dir", str(bad))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse(os.path.lexists(self.tmp / "gone.json"))               # never written through the link

    # ---------------------------------------------------------------- lock-app never destroys the user's files

    def test_lock_app_never_overwrites_a_different_backup(self):
        self.app.mkdir(parents=True)
        cfg, bak = self.app / "obsidian.json", self.app / "obsidian.json.bak"
        cfg.write_text(json.dumps({"vaults": {}, "updateDisabled": False}))
        bak.write_text("an older backup the user kept\n")
        before = (sha(cfg), sha(bak))
        r = self.run_tool("lock-app", "--app-dir", str(self.app))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(".bak", r.stderr)
        self.assertEqual((sha(cfg), sha(bak)), before)                           # nothing changed
        bak.write_bytes(cfg.read_bytes())                                        # control: an identical .bak is fine
        r = self.run_tool("lock-app", "--app-dir", str(self.app))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIs(json.loads(cfg.read_text())["updateDisabled"], True)

    def test_lock_app_refuses_a_symlinked_app_config(self):
        self.app.mkdir(parents=True)
        target = self.tmp / "dotfiles-obsidian.json"
        target.write_text(json.dumps({"vaults": {}}))
        try:
            os.symlink(str(target), str(self.app / "obsidian.json"))
        except (OSError, NotImplementedError) as e:
            self.skipTest("cannot create a symlink here (%s)" % type(e).__name__)
        before = sha(target)
        r = self.run_tool("lock-app", "--app-dir", str(self.app))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("symlink", r.stderr)
        self.assertTrue(os.path.islink(self.app / "obsidian.json"))             # the user's link is intact
        self.assertEqual(sha(target), before)
        self.assertFalse(os.path.lexists(self.app / "obsidian.json.bak"))

    def test_docstring_lists_every_exit_code(self):
        doc = TOOL_SRC.read_text(encoding="utf-8").split('"""')[1]
        exit_line = doc[doc.index("Exit:"):]
        for want in ("0 ok", "1 drift", "setup failed", "2 refused", "64 usage"):
            self.assertIn(want, exit_line)

    # ---------------------------------------------------------------- the links are LIVE (documented behaviour)

    def test_an_edit_in_the_vault_is_an_edit_to_team_memory(self):
        self.setup_vault()
        (self.vault / "memory" / "topics" / "team-rule.md").write_text("changed in the vault\n", encoding="utf-8")
        self.assertEqual((self.repo / "memory" / "topics" / "team-rule.md").read_text(encoding="utf-8"),
                         "changed in the vault\n")

    # ---------------------------------------------------------------- CLI contract and defaults

    def test_cli_contract(self):
        h = self.run_tool("--help")
        self.assertEqual(h.returncode, 0)
        self.assertIn("setup|check|lock-app|path", h.stdout)
        self.assertFalse((self.tmp / "env-vault").exists())                          # --help ran nothing
        self.assertEqual(self.run_tool("--no-such-flag").returncode, 64)
        self.assertEqual(self.run_tool().returncode, 64)
        p = self.run_tool("path")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, str(self.tmp / "env-vault")))
        self.assertFalse((self.tmp / "env-vault").exists())                          # path creates nothing

    def test_default_locations(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import obsidian_vault as o
        home = os.path.expanduser("~")
        self.assertEqual(o.vault_dir(env={}, platform="linux"),
                         os.path.abspath(os.path.join(home, ".local", "share", "agent-team-harness", "vault")))
        self.assertEqual(o.vault_dir(env={"XDG_DATA_HOME": str(self.tmp / "xdg")}, platform="linux"),
                         os.path.abspath(str(self.tmp / "xdg" / "agent-team-harness" / "vault")))
        self.assertEqual(o.vault_dir(env={"LOCALAPPDATA": str(self.tmp / "la")}, platform="win32"),
                         os.path.abspath(str(self.tmp / "la" / "agent-team-harness" / "vault")))
        self.assertEqual(o.vault_dir(env={"AGENT_VAULT_DIR": str(self.tmp / "v")}, platform="linux"),
                         os.path.abspath(str(self.tmp / "v")))
        self.assertEqual(o.app_dir(env={"XDG_CONFIG_HOME": str(self.tmp / "c")}, platform="linux"),
                         os.path.abspath(str(self.tmp / "c" / "obsidian")))
        self.assertEqual(o.app_dir(env={"APPDATA": str(self.tmp / "r")}, platform="win32"),
                         os.path.abspath(str(self.tmp / "r" / "obsidian")))

    def test_no_network_and_no_launch_in_the_source(self):
        src = TOOL_SRC.read_text(encoding="utf-8")
        for bad in ("urllib", "http.client", "socket", "requests", "webbrowser", "os.startfile", "Popen"):
            self.assertNotIn(bad, src)
        self.assertIn('"updateDisabled"', src)                                         # positive control


if __name__ == "__main__":
    unittest.main(verbosity=2)
