#!/usr/bin/env python3
"""obsidian_vault.py - a locked-down Obsidian vault for READING the team memory.

    python3 tools/obsidian_vault.py setup [--vault DIR] [--app-dir DIR]
    python3 tools/obsidian_vault.py check [--vault DIR] [--app-dir DIR]
    python3 tools/obsidian_vault.py lock-app [--app-dir DIR]
    python3 tools/obsidian_vault.py path  [--vault DIR]

setup     creates the vault OUTSIDE the repo: links `memory` and `agent` to this repo's memory/ and agent/ (a symlink;
          a directory junction on Windows when symlinks are not permitted), and writes `.obsidian/` BEFORE Obsidian
          ever opens it: Sync, Publish and Web viewer core plugins off, no community plugins (Obsidian keeps a new
          vault in restricted mode), links never rewritten on rename, delete asks first, reading view by default.
          When Obsidian's app config does not exist yet it is created with automatic updates off and the vault
          registered. When it exists it is NOT touched (it is yours and holds your other vaults): setup says so, and
          `lock-app` turns updates off in it on request. Refuses (exit 2) when the vault path already exists or is
          inside the repo, or when the app config is not a readable file. A failure half-way removes what setup
          created, including parent directories it made (links are unlinked, never followed; dirs only if empty).
check     re-reads every lockdown value and the two links; exit 1 and one line per drift. Also flags any community
          plugin folder, any theme or CSS snippet (a theme or snippet can load remote content: `@import url(...)`),
          a non-default cssTheme / enabledCssSnippets in appearance.json, and an app config with updates on (or
          missing: the first launch would create it with updates ON). It cannot see a remote image embedded in a
          note: Obsidian fetches that when the note is opened.
lock-app  sets updateDisabled=true in Obsidian's app config (creates it, or merges the one key into the existing
          file, keeping a .bak copy). This changes Obsidian for every vault on the machine: that is why it is a
          separate, explicit command. Refuses (exit 2) a symlinked app config (edit its target yourself) and an
          existing, different obsidian.json.bak (it never overwrites a backup).
path      prints the vault directory (whether or not it exists).

Defaults: vault = $AGENT_VAULT_DIR, else $XDG_DATA_HOME/agent-team-harness/vault (~/.local/share/...; on Windows
%LOCALAPPDATA%\\agent-team-harness\\vault; on macOS ~/Library/Application Support/agent-team-harness/vault).
App config dir = $AGENT_OBSIDIAN_APP_DIR, else $XDG_CONFIG_HOME/obsidian (~/.config/obsidian; %APPDATA%\\obsidian;
~/Library/Application Support/obsidian). A Flatpak or Snap install keeps its config elsewhere
(~/.var/app/md.obsidian.Obsidian/config/obsidian for Flatpak): pass --app-dir.

WHY outside the repo: `.obsidian/` holds per-user window and workspace state and must never be committed
(.gitignore also lists it, as a belt). THE LINKS ARE LIVE: this is not a copy. An edit made in Obsidian IS an edit
to the team memory in your working tree; a rename leaves the file's `name:` frontmatter wrong (memory_lint reports
it), and a delete deletes. Use the vault to read and to browse the graph; change memory through the normal flow.

This tool never installs Obsidian, never launches it and makes no network call.
Exit: 0 ok, 1 drift (check) or setup failed (what it created was removed), 2 refused (vault exists / inside the
repo / repo has no memory or agent dir / vault missing for check / app config unusable / lock-app would overwrite a
backup or a symlink), 64 usage.
"""
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LINKS = ("memory", "agent")

# what setup writes into <vault>/.obsidian/; LOCKDOWN below is the subset `check` insists on
SETTINGS = {
    "core-plugins.json": {
        "file-explorer": True, "global-search": True, "switcher": True, "graph": True, "backlink": True,
        "outgoing-link": True, "tag-pane": True, "page-preview": True, "outline": True, "command-palette": True,
        "sync": False, "publish": False, "webviewer": False,
        "audio-recorder": False, "daily-notes": False, "templates": False, "note-composer": False,
        "canvas": False, "bases": False,
    },
    "community-plugins.json": [],
    "app.json": {
        "defaultViewMode": "preview", "livePreview": False, "alwaysUpdateLinks": False, "promptDelete": True,
        "spellcheck": False,
    },
    "appearance.json": {"theme": "obsidian"},
}
LOCKDOWN = {
    "core-plugins.json": {"sync": False, "publish": False, "webviewer": False},
    "app.json": {"alwaysUpdateLinks": False, "promptDelete": True},
}
APP_CONFIG = "obsidian.json"
# folders whose content Obsidian loads as CSS (which can `@import` or `url()` a remote host); setup creates neither
CSS_DIRS = ("themes", "snippets")


# ---- locations -------------------------------------------------------------------------------------------------

def _home():
    return os.path.expanduser("~")


def vault_dir(override=None, env=None, platform=None):
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if override:
        return os.path.abspath(override)
    if env.get("AGENT_VAULT_DIR"):
        return os.path.abspath(env["AGENT_VAULT_DIR"])
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or os.path.join(_home(), "AppData", "Local")
    elif platform == "darwin":
        base = os.path.join(_home(), "Library", "Application Support")
    else:
        base = env.get("XDG_DATA_HOME") or os.path.join(_home(), ".local", "share")
    return os.path.abspath(os.path.join(base, "agent-team-harness", "vault"))


def app_dir(override=None, env=None, platform=None):
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if override:
        return os.path.abspath(override)
    if env.get("AGENT_OBSIDIAN_APP_DIR"):
        return os.path.abspath(env["AGENT_OBSIDIAN_APP_DIR"])
    if platform.startswith("win"):
        base = env.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")
    elif platform == "darwin":
        base = os.path.join(_home(), "Library", "Application Support")
    else:
        base = env.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")
    return os.path.abspath(os.path.join(base, "obsidian"))


def _real(p):
    return os.path.normcase(os.path.realpath(p))


def _inside(child, parent):
    try:
        return os.path.commonpath([_real(child), _real(parent)]) == _real(parent)
    except ValueError:        # another drive on Windows
        return False


def _is_link(p):
    """A symlink, or (Windows) a directory junction."""
    if os.path.islink(p):
        return True
    isj = getattr(os.path, "isjunction", None)          # Python 3.12+
    if isj and isj(p):
        return True
    # older Pythons on Windows: a junction is a directory whose real path is not "its parent's real path + its name"
    # (comparing with the parent's real path, not abspath, so a symlinked parent such as macOS /tmp is not a link)
    if not (os.path.lexists(p) and os.path.isdir(p)):
        return False
    here = os.path.normcase(os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(p))), os.path.basename(p)))
    return _real(p) != here


def links_to_repo(vault, root):
    """True when `vault` is a set-up vault whose memory link resolves to <root>/memory. Never raises."""
    try:
        link = os.path.join(vault, "memory")
        return (os.path.isdir(os.path.join(vault, ".obsidian")) and _is_link(link)
                and _real(link) == _real(os.path.join(root, "memory")))
    except Exception:
        return False


# ---- making links ----------------------------------------------------------------------------------------------

def make_dir_link(target, link):
    """A directory symlink; on Windows fall back to a junction (no admin or developer mode needed)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        if not sys.platform.startswith("win"):
            raise
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return "junction"
    except Exception:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not os.path.isdir(link):
            raise OSError("could not create a symlink or a junction at %s" % common.shown(link, 200))
        return "junction"


def remove_link(p):
    """Remove a link WITHOUT following it (never rmtree: that could walk into the team memory)."""
    if os.path.islink(p):
        os.unlink(p)
    elif os.path.isdir(p):
        os.rmdir(p)           # a junction: rmdir removes the link only; a real non-empty dir raises, which is right
    elif os.path.lexists(p):
        os.unlink(p)


def _write_json(path, obj):
    common.atomic_write(path, json.dumps(obj, indent=2) + "\n")


# ---- commands --------------------------------------------------------------------------------------------------

def cmd_setup(vault, appd, root):
    if _inside(vault, root) or _inside(root, vault):
        common.die("REFUSED: the vault %s would be inside the repo (or contain it); .obsidian/ must never be "
                   "committed. Use --vault outside the repo." % common.shown(vault, 200), common.REFUSED)
    if os.path.lexists(vault):
        common.die("REFUSED: %s already exists; setup never overwrites a vault. Run `check` to inspect it, or "
                   "remove it yourself." % common.shown(vault, 200), common.REFUSED)
    for name in LINKS:
        src = os.path.join(root, name)
        if not os.path.isdir(src) or os.path.islink(src):
            common.die("REFUSED: the repo has no real %s/ directory to link" % name, common.REFUSED)
    _refuse_unusable_app_config(appd)        # before anything is created: a refusal must leave nothing behind

    created = []                      # undo list, newest last
    try:
        missing, p = [], os.path.dirname(vault)          # the parents makedirs is about to create, deepest first
        while p and not os.path.lexists(p) and os.path.dirname(p) != p:
            missing.append(p)
            p = os.path.dirname(p)
        for d in reversed(missing):                      # outermost first, so the undo removes the deepest first
            os.mkdir(d)
            created.append(("dir", d))
        os.mkdir(vault)
        created.append(("dir", vault))
        cfg = os.path.join(vault, ".obsidian")
        os.mkdir(cfg)
        created.append(("dir", cfg))
        for fn, obj in SETTINGS.items():            # settings first: Obsidian must never see the vault without them
            _write_json(os.path.join(cfg, fn), obj)
            created.append(("file", os.path.join(cfg, fn)))
        kinds = []
        for name in LINKS:
            link = os.path.join(vault, name)
            kinds.append(make_dir_link(os.path.join(root, name), link))
            created.append(("link", link))
            if _real(link) != _real(os.path.join(root, name)):
                raise OSError("link %s does not resolve to the repo's %s/" % (name, name))
        app_note = _app_after_setup(appd, vault)    # last, inside the undo: a failure here must not strand a vault
    except BaseException as e:
        left = []
        for kind, p in reversed(created):
            try:
                if kind == "link":
                    remove_link(p)
                elif kind == "file":
                    os.unlink(p)
                else:
                    os.rmdir(p)       # only an empty dir; never a recursive delete
            except OSError:
                left.append(p)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        if left:
            common.die("setup failed: %s; could NOT remove (check by hand): %s" % (
                common.printable(e), ", ".join(common.shown(p, 200) for p in left)), common.FINDINGS)
        common.die("setup failed, nothing left behind: %s" % common.printable(e), common.FINDINGS)

    print("vault: %s" % vault)
    print("links: memory, agent -> %s (%s)" % (root, kinds[0]))
    print(app_note)
    print("open: obsidian://open?vault=%s  (or Obsidian > Open folder as vault)" % os.path.basename(vault))
    print("NOTE: the links are live - an edit, rename or delete in Obsidian changes the team memory in this clone.")
    return common.OK


def _refuse_unusable_app_config(appd):
    """Exit 2 when the app config dir is not a directory, or obsidian.json exists but is not a readable file (a
    directory, a dangling symlink), or a parent of the app config dir is a file: reading or creating it would raise
    half-way through a command."""
    p = appd
    while p and not os.path.lexists(p) and os.path.dirname(p) != p:      # nearest existing ancestor
        p = os.path.dirname(p)
    if p != appd and os.path.lexists(p) and not os.path.isdir(p):
        common.die("REFUSED: the app config dir %s cannot be created: %s is not a directory; pass --app-dir"
                   % (common.shown(appd, 200), common.shown(p, 200)), common.REFUSED)
    if os.path.lexists(appd) and not os.path.isdir(appd):
        common.die("REFUSED: the app config dir %s is not a directory; fix it by hand or pass --app-dir"
                   % common.shown(appd, 200), common.REFUSED)
    cfg = os.path.join(appd, APP_CONFIG)
    if os.path.lexists(cfg) and not os.path.isfile(cfg):
        common.die("REFUSED: %s exists but is not a readable file (a directory or a dangling link); fix it by hand"
                   % common.shown(cfg, 200), common.REFUSED)


def _read_app_config(cfg):
    """The parsed app config, or None when it is not valid JSON. An unreadable file exits 2 (never a traceback)."""
    try:
        return json.loads(common.read_text(cfg))
    except ValueError:
        return None
    except OSError as e:
        common.die("REFUSED: cannot read %s (%s); fix it by hand" % (common.shown(cfg, 200), type(e).__name__),
                   common.REFUSED)


def _app_after_setup(appd, vault):
    cfg = os.path.join(appd, APP_CONFIG)
    if not os.path.lexists(cfg):
        os.makedirs(appd, exist_ok=True)
        vid = os.urandom(8).hex()
        _write_json(cfg, {"vaults": {vid: {"path": vault, "ts": int(datetime.datetime.now().timestamp() * 1000),
                                           "open": True}},
                          "updateDisabled": True})
        return "app config: created %s with automatic updates OFF and this vault registered" % cfg
    try:
        data = json.loads(common.read_text(cfg))
    except (ValueError, OSError):        # validated before setup began; a race is a warning, not a traceback
        data = None
    if isinstance(data, dict) and data.get("updateDisabled") is True:
        return "app config: %s exists and already has automatic updates off (not modified)" % cfg
    return ("WARNING: Obsidian's app config %s exists and does NOT turn automatic updates off; it was not modified. "
            "Run `obsidian_vault.py lock-app` to set it (affects every vault), or turn off "
            "Settings > General > Automatic updates." % cfg)


def drift(vault, appd, root):
    """Every lockdown finding, as text lines. [] = locked down."""
    out = []
    cfg = os.path.join(vault, ".obsidian")
    for fn, want in LOCKDOWN.items():
        p = os.path.join(cfg, fn)
        try:
            data = json.loads(common.read_text(p))
        except FileNotFoundError:
            out.append(".obsidian/%s is missing" % fn)
            continue
        except ValueError:
            out.append(".obsidian/%s is not valid JSON" % fn)
            continue
        except OSError:
            out.append(".obsidian/%s cannot be read as a file" % fn)
            continue
        if not isinstance(data, dict):
            out.append(".obsidian/%s is not a JSON object" % fn)
            continue
        for k, v in want.items():
            if data.get(k) is not v:
                out.append(".obsidian/%s: %s is %s, must be %s" % (fn, k, json.dumps(data.get(k)), json.dumps(v)))
    p = os.path.join(cfg, "community-plugins.json")
    try:
        enabled = json.loads(common.read_text(p))
        if enabled != []:
            out.append(".obsidian/community-plugins.json enables plugins: %s" % common.printable(json.dumps(enabled))[:200])
    except FileNotFoundError:
        pass                                      # absent = none enabled
    except ValueError:
        out.append(".obsidian/community-plugins.json is not valid JSON")
    except OSError:
        out.append(".obsidian/community-plugins.json cannot be read as a file")
    plugins = os.path.join(cfg, "plugins")
    if os.path.isdir(plugins) and os.listdir(plugins):
        out.append(".obsidian/plugins/ holds community plugin code: %s" % ", ".join(
            common.shown(x, 40) for x in sorted(os.listdir(plugins))[:5]))
    # themes and CSS snippets are CSS that Obsidian loads, and CSS can `@import url(https://...)`: setup creates
    # neither folder and leaves appearance.json at the default theme, so anything here was added after setup
    for d in CSS_DIRS:
        p = os.path.join(cfg, d)
        if os.path.lexists(p) and not (os.path.isdir(p) and not os.path.islink(p) and not os.listdir(p)):
            names = sorted(os.listdir(p))[:5] if os.path.isdir(p) else []
            out.append(".obsidian/%s/ holds CSS that can load remote content: %s" % (
                d, ", ".join(common.shown(x, 40) for x in names) or "(not an empty folder)"))
    p = os.path.join(cfg, "appearance.json")
    try:
        look = json.loads(common.read_text(p))
    except FileNotFoundError:
        look = {}                                 # absent = Obsidian's default theme, no snippets
    except (ValueError, OSError):
        look = None
        out.append(".obsidian/appearance.json is not a readable JSON file")
    if look is not None:
        if not isinstance(look, dict):
            out.append(".obsidian/appearance.json is not a JSON object")
        else:
            if look.get("cssTheme", "") != "":
                out.append(".obsidian/appearance.json: cssTheme is %s, must be the default (\"\" or absent)"
                           % common.printable(json.dumps(look.get("cssTheme")))[:120])
            if look.get("enabledCssSnippets", []) != []:
                out.append(".obsidian/appearance.json: enabledCssSnippets is %s, must be empty"
                           % common.printable(json.dumps(look.get("enabledCssSnippets")))[:120])
    for name in LINKS:
        link = os.path.join(vault, name)
        if not _is_link(link):
            out.append("%s is not a link (a copy goes stale; re-create the vault)" % name)
        elif _real(link) != _real(os.path.join(root, name)):
            out.append("%s links to %s, not this repo's %s/" % (name, common.shown(os.path.realpath(link), 120), name))
    app = os.path.join(appd, APP_CONFIG)
    if not os.path.lexists(app):
        out.append("app config %s is missing: the first launch creates it with automatic updates ON (run lock-app)"
                   % app)
    else:
        data = _read_app_config(app)              # unreadable -> exit 2 (cmd_check validated it first)
        if data is None:
            out.append("app config %s is not valid JSON" % app)
        elif not (isinstance(data, dict) and data.get("updateDisabled") is True):
            out.append("app config %s: automatic updates are ON (run lock-app)" % app)
    return out


def cmd_check(vault, appd, root):
    if not os.path.isdir(vault):
        common.die("REFUSED: no vault at %s (run setup)" % common.shown(vault, 200), common.REFUSED)
    _refuse_unusable_app_config(appd)
    found = drift(vault, appd, root)
    for line in found:
        print("DRIFT " + common.printable(line))
    if found:
        return common.FINDINGS
    print("OK vault %s is locked down (%d settings, %d links, updates off)" % (
        vault, sum(len(v) for v in LOCKDOWN.values()) + 1, len(LINKS)))
    return common.OK


def cmd_lock_app(appd):
    cfg = os.path.join(appd, APP_CONFIG)
    if os.path.islink(cfg):
        # the write below replaces the file it names: a symlink (a dotfiles setup) would become a plain file and its
        # target would never get the key. Say so instead of breaking the user's link.
        common.die("REFUSED: %s is a symlink (to %s); set \"updateDisabled\": true in that file yourself"
                   % (common.shown(cfg, 200), common.shown(os.path.realpath(cfg), 200)), common.REFUSED)
    _refuse_unusable_app_config(appd)
    if os.path.lexists(cfg):
        try:
            with open(cfg, "rb") as f:
                raw = f.read()
        except OSError as e:
            common.die("REFUSED: cannot read %s (%s); fix it by hand" % (common.shown(cfg, 200), type(e).__name__),
                       common.REFUSED)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            common.die("REFUSED: %s is not valid JSON; fix it by hand" % cfg, common.REFUSED)
        if not isinstance(data, dict):
            common.die("REFUSED: %s is not a JSON object" % cfg, common.REFUSED)
        if data.get("updateDisabled") is True:
            print("already off: %s" % cfg)
            return common.OK
        bak = cfg + ".bak"
        if os.path.lexists(bak):
            same = False
            if os.path.isfile(bak) and not os.path.islink(bak):
                try:
                    with open(bak, "rb") as f:
                        same = f.read() == raw
                except OSError:
                    same = False
            if not same:      # never overwrite a backup: it may be the only copy of an older config
                common.die("REFUSED: %s already exists and differs from the current %s; move it aside first"
                           % (common.shown(bak, 200), APP_CONFIG), common.REFUSED)
        else:
            try:
                with open(bak, "xb") as f:          # exclusive create: never follows or replaces anything
                    f.write(raw)
            except OSError as e:
                common.die("REFUSED: cannot write the backup %s (%s)" % (common.shown(bak, 200), type(e).__name__),
                           common.REFUSED)
        data["updateDisabled"] = True
        _write_json(cfg, data)
        print("automatic updates OFF in %s (previous file kept as %s.bak)" % (cfg, APP_CONFIG))
        return common.OK
    os.makedirs(appd, exist_ok=True)
    _write_json(cfg, {"updateDisabled": True})
    print("created %s with automatic updates OFF" % cfg)
    return common.OK


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(prog="obsidian_vault.py", description=__doc__.split("\n\n")[0],
                          epilog="Exit: 0 ok, 1 drift (check) or setup failed, 2 refused, 64 usage. "
                                 "Never installs or launches Obsidian; no network.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="setup|check|lock-app|path")
    for name, hlp in (("setup", "create the vault with locked-down settings (refuses an existing one)"),
                      ("check", "re-read the lockdown values; exit 1 on drift"),
                      ("lock-app", "turn Obsidian's automatic updates off in its app config"),
                      ("path", "print the vault directory")):
        p = sub.add_parser(name, help=hlp)
        if name != "lock-app":
            p.add_argument("--vault", help="vault directory (default: see `path`)")
        if name != "path":
            p.add_argument("--app-dir", help="Obsidian's app config directory (holds obsidian.json)")
    a = ap.parse_args(argv)
    root = common.repo_root()
    if a.cmd == "path":
        print(vault_dir(a.vault))
        return common.OK
    appd = app_dir(a.app_dir)
    if a.cmd == "lock-app":
        return cmd_lock_app(appd)
    vault = vault_dir(a.vault)
    if a.cmd == "setup":
        return cmd_setup(vault, appd, root)
    return cmd_check(vault, appd, root)


if __name__ == "__main__":
    sys.exit(main())
