#!/usr/bin/env python3
"""Launchpad for Claude Code: projects, recent Claude Code sessions, one-click launch and token usage.

Independent open-source project, not affiliated with or endorsed by Anthropic.
Licensed under GPL-3.0.
"""
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

__version__ = "1.1.0"
SYSTEM = platform.system()
HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HOME, ".launchpad-cc")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LEGACY_STATE_FILES = [os.path.join(HOME, ".claude_launcher.json")]
ICON_FILE = os.path.join(APP_DIR, "assets", "icon.ico")
APP_ID = "LaunchpadForClaudeCode"
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
SINGLE_PORT = 47823
USAGE_REFRESH_S = 90
# Pinned on purpose: never auto-run whatever "latest" is on npm (supply-chain safety).
CCUSAGE_PKG = "ccusage@20.0.51"
LOG_FILE = os.path.join(DATA_DIR, "launchpad.log")


def log(msg):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 200_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
CREATE_NO_WINDOW = 0x08000000 if SYSTEM == "Windows" else 0
CREATE_NEW_CONSOLE = 0x00000010 if SYSTEM == "Windows" else 0


def default_root():
    for p in (os.path.join(HOME, "Desktop", "Work"), os.path.join(HOME, "Projects")):
        if os.path.isdir(p):
            return p
    return os.path.join(HOME, "Projects")


def key(path):
    return os.path.normcase(os.path.abspath(path))


# ------------------------------------------------------------------ state

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"projects": {}, "settings": {"minimize_after_open": True}}
        raw = {}
        for path in [STATE_FILE] + LEGACY_STATE_FILES:
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                break
            except (OSError, ValueError):
                continue
        projects = raw.get("projects", {})
        if isinstance(projects, list):  # legacy v1 format
            projects = {key(p["path"]): {"name": p.get("name") or os.path.basename(p["path"]),
                                         "path": p["path"], "mode": "terminal", "pinned": False,
                                         "created": 0, "last_opened": 0, "opens": 0}
                        for p in projects if p.get("path")}
        self.data["projects"] = projects
        self.data["settings"].update(raw.get("settings", {}))

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)

    def get(self, path):
        return self.data["projects"].get(key(path))

    def upsert(self, path, **fields):
        with self.lock:
            k = key(path)
            p = self.data["projects"].get(k) or {
                "name": os.path.basename(path.rstrip("\\/")) or path, "path": path, "mode": "terminal",
                "pinned": False, "created": 0, "last_opened": 0, "opens": 0}
            p.update(fields)
            self.data["projects"][k] = p
            self.save()
            return p


# ------------------------------------------------------------------ Claude Code sessions

_session_cache = {}


def _text_of(message):
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return c.get("text", "")
    return ""


def parse_session(path, mtime):
    cached = _session_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    info = {"id": os.path.splitext(os.path.basename(path))[0], "cwd": None, "title": None,
            "summary": None, "branch": None, "entrypoint": None, "mtime": mtime}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 400:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "summary" and obj.get("summary") and not info["summary"]:
                    info["summary"] = obj["summary"]
                info["cwd"] = info["cwd"] or obj.get("cwd")
                info["branch"] = info["branch"] or obj.get("gitBranch")
                info["entrypoint"] = info["entrypoint"] or obj.get("entrypoint")
                if not info["title"] and obj.get("type") == "user" and not obj.get("isMeta"):
                    t = _text_of(obj.get("message", {})).strip()
                    if t and not t.startswith("<") and not t.startswith("Caveat"):
                        info["title"] = re.sub(r"\s+", " ", t)[:140]
                if info["cwd"] and info["title"] and i > 40:
                    break
    except OSError:
        info = None
    _session_cache[path] = (mtime, info)
    return info


def scan_sessions(limit=60):
    files = []
    if not os.path.isdir(CLAUDE_PROJECTS):
        return []
    for d in os.scandir(CLAUDE_PROJECTS):
        if not d.is_dir():
            continue
        try:
            for f in os.scandir(d.path):
                if f.name.endswith(".jsonl") and f.is_file():
                    st = f.stat()
                    if st.st_size > 0:
                        files.append((st.st_mtime, f.path))
        except OSError:
            continue
    files.sort(reverse=True)
    out = []
    for mtime, p in files[: limit * 2]:
        info = parse_session(p, mtime)
        if info and info["cwd"] and (info["title"] or info["summary"]):
            out.append(info)
        if len(out) >= limit:
            break
    return out


def installed_hotkey():
    try:
        with open(os.path.join(DATA_DIR, "install.json"), encoding="utf-8") as f:
            hk = str(json.load(f).get("hotkey", ""))
        return hk if re.fullmatch(r"[A-Za-z0-9+ ]{1,30}", hk) else ""
    except (OSError, ValueError):
        return ""


def git_branch(path):
    head = os.path.join(path, ".git", "HEAD")
    try:
        if os.path.isfile(os.path.join(path, ".git")):  # worktree
            return None
        with open(head, encoding="utf-8") as f:
            ref = f.read().strip()
        return ref.rsplit("/", 1)[-1] if ref.startswith("ref:") else ref[:7]
    except OSError:
        return None


# ------------------------------------------------------------------ launching

def open_claude_desktop():
    if SYSTEM == "Windows":
        exe = os.path.expandvars(r"%LOCALAPPDATA%\AnthropicClaude\claude.exe")
        if os.path.exists(exe):
            subprocess.Popen([exe])
        else:
            os.startfile("claude://")
    elif SYSTEM == "Darwin":
        subprocess.Popen(["open", "-a", "Claude"])
    else:
        subprocess.Popen(["claude-desktop"])


def which(name):
    """Resolve an executable to an absolute path.

    Always called with the app folder as working directory (see main), so a
    claude.bat / claude.exe dropped inside a project folder can never be picked
    up instead of the real one (Windows searches the current directory first).
    """
    return shutil.which(name)


def system32(exe):
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", exe)


def copy_to_clipboard(text):
    if SYSTEM == "Windows":
        subprocess.run([system32("clip.exe")], input=text.encode("utf-16"), creationflags=CREATE_NO_WINDOW)
    elif SYSTEM == "Darwin":
        subprocess.run(["/usr/bin/pbcopy"], input=text.encode())


def open_terminal(path, args):
    """Start Claude Code in a new terminal inside `path`.

    `args` is built only from validated values (--continue, --resume <uuid>).
    The folder is passed as the working directory, never inside a command string.
    """
    claude = which("claude")
    if not claude:
        raise RuntimeError("Claude Code CLI ('claude') not found on PATH")
    if SYSTEM == "Windows":
        wt = which("wt")
        if wt:
            # Windows Terminal treats ";" as a command separator: escape it in the path.
            subprocess.Popen([wt, "-d", path.replace(";", "\\;"), claude, *args])
        else:
            # cmd /k ""C:\path\claude.exe" args": cmd strips the outer quotes, keeps the inner ones.
            line = f'"{system32("cmd.exe")}" /k ""{claude}"{"" if not args else " " + " ".join(args)}"'
            subprocess.Popen(line, cwd=path, creationflags=CREATE_NEW_CONSOLE)
    else:
        import shlex
        cmd = " ".join(shlex.quote(x) for x in [claude, *args])
        if SYSTEM == "Darwin":
            s = f"cd {shlex.quote(path)} && {cmd}".replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["/usr/bin/osascript", "-e", f'tell application "Terminal" to do script "{s}"'])
        else:
            term = which("x-terminal-emulator") or which("gnome-terminal") or which("konsole")
            if not term:
                raise RuntimeError("No terminal emulator found")
            subprocess.Popen([term, "-e", "bash", "-lc", f"cd {shlex.quote(path)} && {cmd}; exec bash"])


# ------------------------------------------------------------------ extensions (plugins, mods, skills)
#
# Per-project switches are written to <project>/.claude/settings.local.json, the
# documented "project local" scope: it overrides ~/.claude/settings.json for that
# project only and stays out of git. Nothing global is ever modified.
#   - plugins and mods  -> "enabledPlugins": {"<name>@<origin>": true|false}
#   - personal skills   -> "skillOverrides": {"<skill>": "on"|"off"}
# Mods loaded through CLAUDE_CODE_PLUGIN_DIRS have the id "<name>@inline".

CLAUDE_DIR = os.path.join(HOME, ".claude")
PLUGIN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}@[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _manifest(root):
    for mf in (os.path.join(root, ".claude-plugin", "plugin.json"), os.path.join(root, "plugin.json")):
        data = read_json(mf)
        if data:
            return data
    return {}


def _is_mod(root):
    return bool(read_json(os.path.join(root, "hooks", "hooks.json")).get("modules"))


def _skill_count(root):
    sk = os.path.join(root, "skills")
    try:
        return sum(1 for e in os.scandir(sk) if os.path.exists(os.path.join(e.path, "SKILL.md")))
    except OSError:
        return 0


def _frontmatter(md):
    try:
        with open(md, encoding="utf-8", errors="replace") as f:
            text = f.read(4000)
    except OSError:
        return {}
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip("\"'")
    return out


def _short(text, n=160):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def extension_inventory():
    """Everything the user has installed, with the state it has by default (from user settings)."""
    user = read_json(os.path.join(CLAUDE_DIR, "settings.json"))
    enabled = user.get("enabledPlugins") if isinstance(user.get("enabledPlugins"), dict) else {}
    overrides = user.get("skillOverrides") if isinstance(user.get("skillOverrides"), dict) else {}
    items, seen = [], set()

    def add(item):
        if item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)

    installed = read_json(os.path.join(CLAUDE_DIR, "plugins", "installed_plugins.json")).get("plugins") or {}
    for pid, entries in installed.items() if isinstance(installed, dict) else []:
        if not PLUGIN_ID_RE.fullmatch(str(pid)):
            continue
        entries = [e for e in (entries if isinstance(entries, list) else [entries]) if isinstance(e, dict)]
        user_scope = any(e.get("scope") == "user" for e in entries)
        root = next((e.get("installPath") for e in entries if e.get("installPath")), None)
        root = root if root and os.path.isdir(root) else None
        m = _manifest(root) if root else {}
        add({"kind": "mod" if root and _is_mod(root) else "plugin", "id": pid, "name": pid.split("@")[0],
             "source": pid.split("@")[1], "description": _short(m.get("description")),
             "skills": _skill_count(root) if root else 0, "scope": "user" if user_scope else "project",
             "default": bool(enabled[pid]) if pid in enabled else user_scope})

    env_dirs = (user.get("env") or {}).get("CLAUDE_CODE_PLUGIN_DIRS") if isinstance(user.get("env"), dict) else None
    env_dirs = env_dirs or os.environ.get("CLAUDE_CODE_PLUGIN_DIRS") or ""
    for d in [x.strip() for x in str(env_dirs).split(os.pathsep) if x.strip()]:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        m = _manifest(d)
        name = str(m.get("name") or os.path.basename(d.rstrip("\\/")))
        pid = f"{name}@inline"
        if not PLUGIN_ID_RE.fullmatch(pid):
            continue
        add({"kind": "mod" if _is_mod(d) else "plugin", "id": pid, "name": name, "source": "inline",
             "description": _short(m.get("description")), "skills": _skill_count(d), "scope": "user",
             "default": bool(enabled[pid]) if pid in enabled else m.get("defaultEnabled", True) is not False})

    skills_dir = os.path.join(CLAUDE_DIR, "skills")
    try:
        entries = sorted(os.scandir(skills_dir), key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for e in entries:
        if not e.is_dir():
            continue
        if os.path.exists(os.path.join(e.path, ".claude-plugin", "plugin.json")):
            m = _manifest(e.path)
            name = str(m.get("name") or e.name)
            pid = f"{name}@skills-dir"
            if PLUGIN_ID_RE.fullmatch(pid):
                add({"kind": "mod" if _is_mod(e.path) else "plugin", "id": pid, "name": name, "source": "skills-dir",
                     "description": _short(m.get("description")), "skills": _skill_count(e.path), "scope": "user",
                     "default": bool(enabled[pid]) if pid in enabled else m.get("defaultEnabled", True) is not False})
            continue
        md = os.path.join(e.path, "SKILL.md")
        if not os.path.exists(md):
            continue
        fm = _frontmatter(md)
        name = str(fm.get("name") or e.name)
        if not SKILL_NAME_RE.fullmatch(name):
            continue
        add({"kind": "skill", "id": "skill:" + name, "name": name, "source": "personal",
             "description": _short(fm.get("description")), "skills": 0, "scope": "user",
             "default": overrides.get(name, "on") != "off"})

    order = {"mod": 0, "plugin": 1, "skill": 2}
    items.sort(key=lambda i: (order[i["kind"]], i["name"].lower()))
    return items


def project_extension_state(path, items):
    """Effective on/off for each item in a project: local > shared project > user default."""
    shared = read_json(os.path.join(path, ".claude", "settings.json"))
    local = read_json(os.path.join(path, ".claude", "settings.local.json"))
    states = {}
    for it in items:
        val, inherited = it["default"], it["default"]
        if it["kind"] == "skill":
            name = it["name"]
            for src in (shared, local):
                ov = src.get("skillOverrides") if isinstance(src.get("skillOverrides"), dict) else {}
                if name in ov:
                    val = ov[name] != "off"
                if src is shared:
                    inherited = val
        else:
            for src in (shared, local):
                ep = src.get("enabledPlugins") if isinstance(src.get("enabledPlugins"), dict) else {}
                if it["id"] in ep:
                    val = bool(ep[it["id"]])
                if src is shared:
                    inherited = val
        states[it["id"]] = (val, inherited)
    return states


def write_project_extensions(path, states, items):
    """Write only the switches that differ from what the project would inherit."""
    by_id = {i["id"]: i for i in items}
    current = project_extension_state(path, items)
    local_path = os.path.join(path, ".claude", "settings.local.json")
    local = read_json(local_path)
    ep = dict(local.get("enabledPlugins") or {}) if isinstance(local.get("enabledPlugins"), dict) else {}
    so = dict(local.get("skillOverrides") or {}) if isinstance(local.get("skillOverrides"), dict) else {}
    for iid, want in states.items():
        it = by_id.get(iid)
        if it is None or not isinstance(want, bool):
            continue  # unknown ids from the UI are ignored, never written
        inherited = current[iid][1]
        if it["kind"] == "skill":
            if want == inherited:
                so.pop(it["name"], None)
            else:
                so[it["name"]] = "on" if want else "off"
        else:
            if want == inherited:
                ep.pop(iid, None)
            else:
                ep[iid] = want
    for k, v in (("enabledPlugins", ep), ("skillOverrides", so)):
        if v:
            local[k] = v
        else:
            local.pop(k, None)
    if not local and not os.path.exists(local_path):
        return 0
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    tmp = local_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(local, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, local_path)
    return len(ep) + len(so)


def ensure_gitignored(path, entry=".claude/settings.local.json"):
    gi = os.path.join(path, ".gitignore")
    try:
        existing = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
    except OSError:
        return
    if entry not in existing.splitlines():
        with open(gi, "a", encoding="utf-8") as f:
            f.write(("" if not existing or existing.endswith("\n") else "\n") + entry + "\n")


# ------------------------------------------------------------------ token usage

class Usage:
    def __init__(self):
        self.data = {"status": "loading"}
        self.lock = threading.Lock()
        self.busy = False

    def refresh_async(self):
        if self.busy:
            return
        self.busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def loop(self):
        while True:
            self.refresh_async()
            time.sleep(USAGE_REFRESH_S)

    def _run(self):
        try:
            self.data = self._fetch()
        except Exception as e:  # noqa: BLE001 - surfaced in the UI
            self.data = {"status": "error", "error": str(e)[:300]}
            log(f"usage error: {e!r}")
        finally:
            self.busy = False

    def _fetch(self):
        npx = which("npx")
        if not npx:
            return {"status": "error", "error": "npx not found: install Node.js"}
        r = subprocess.run([npx, "-y", CCUSAGE_PKG, "blocks", "--json", "--token-limit", "max"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=240, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return {"status": "error", "error": (r.stderr or "ccusage error").strip()[-300:]}
        blocks = [b for b in json.loads(r.stdout).get("blocks", []) if not b.get("isGap")]
        active = next((b for b in blocks if b.get("isActive")), None)
        days = {}
        for b in blocks:
            try:
                d = datetime.fromisoformat(b["startTime"].replace("Z", "+00:00")).astimezone().date().isoformat()
            except (KeyError, ValueError):
                continue
            agg = days.setdefault(d, {"tokens": 0, "cost": 0.0})
            agg["tokens"] += b.get("totalTokens", 0)
            agg["cost"] += b.get("costUSD", 0.0)
        last7 = []
        today = datetime.now().date()
        for i in range(6, -1, -1):
            d = datetime.fromordinal(today.toordinal() - i).date().isoformat()
            last7.append({"date": d, **days.get(d, {"tokens": 0, "cost": 0.0})})
        res = {"status": "ok", "updated": time.time(), "today": last7[-1], "last7": last7, "active": None}
        if active:
            res["active"] = {
                "tokens": active.get("totalTokens", 0), "cost": active.get("costUSD", 0.0),
                "start": active.get("startTime"), "end": active.get("endTime"),
                "burn": (active.get("burnRate") or {}).get("tokensPerMinute"),
                "costPerHour": (active.get("burnRate") or {}).get("costPerHour"),
                "projection": (active.get("projection") or {}).get("totalTokens"),
                "remainingMin": (active.get("projection") or {}).get("remainingMinutes"),
                "percent": (active.get("tokenLimitStatus") or {}).get("percentUsed"),
                "limit": (active.get("tokenLimitStatus") or {}).get("limit"),
            }
        return res


# ------------------------------------------------------------------ API exposed to the UI

class Api:
    """Every public method here is callable from the UI (pywebview js_api).
    Keep internal state in underscore attributes: pywebview does not expose those."""

    def __init__(self):
        self._store = Store()
        self._usage = Usage()
        self._window = None

    # --- read
    def get_state(self):
        sessions = scan_sessions()
        latest = {}
        counts = {}
        for s in sessions:
            k = key(s["cwd"])
            counts[k] = counts.get(k, 0) + 1
            if k not in latest:
                latest[k] = s
        projects = {}
        for k, p in self._store.data["projects"].items():
            if p.get("hidden"):
                continue
            projects[k] = dict(p, source="launcher")
        for k, s in latest.items():
            if k in projects or key(HOME) == k:
                continue
            if self._store.data["projects"].get(k, {}).get("hidden"):
                continue
            mode = "desktop" if "desktop" in (s.get("entrypoint") or "").lower() else "terminal"
            projects[k] = {"name": os.path.basename(s["cwd"].rstrip("\\/")) or s["cwd"], "path": s["cwd"],
                           "mode": mode, "pinned": False, "created": 0, "last_opened": 0, "opens": 0,
                           "source": "claude"}
        out = []
        for k, p in projects.items():
            if not os.path.isdir(p["path"]):
                continue
            s = latest.get(k)
            activity = max(p.get("last_opened", 0), s["mtime"] if s else 0, p.get("created", 0))
            out.append(dict(p, last_activity=activity, sessions=counts.get(k, 0),
                            branch=git_branch(p["path"]), last_session=s and {
                                "id": s["id"], "title": s["summary"] or s["title"], "mtime": s["mtime"]}))
        out.sort(key=lambda p: p["last_activity"], reverse=True)
        last = next((p for p in out if p.get("last_opened")), out[0] if out else None)
        for s in sessions:
            p = projects.get(key(s["cwd"]))
            s["project"] = p["name"] if p else os.path.basename(s["cwd"])
            s["display"] = s["summary"] or s["title"]
        return {"projects": out, "sessions": sessions[:40], "last": last,
                "default_root": default_root(), "settings": self._store.data["settings"],
                "home": HOME, "has_wt": bool(which("wt")), "hotkey": installed_hotkey()}

    def ui_log(self, msg):
        log("UI: " + str(msg)[:2000])
        return True

    def get_usage(self):
        return dict(self._usage.data, busy=self._usage.busy)

    def refresh_usage(self):
        self._usage.refresh_async()
        return True

    # --- actions
    def open_project(self, path, mode=None, resume=None):
        if not os.path.isdir(path):
            return {"ok": False, "error": f"Folder no longer exists: {path}"}
        p = self._store.get(path) or {}
        mode = mode if mode in ("terminal", "desktop") else (p.get("mode") or "terminal")
        try:
            if mode == "desktop":
                copy_to_clipboard(path)
                open_claude_desktop()
                msg = "Claude Desktop opened, folder path copied to clipboard"
            else:
                args = []
                if resume == "continue":
                    args = ["--continue"]
                elif resume:
                    if not UUID_RE.fullmatch(resume):
                        return {"ok": False, "error": "Invalid session ID"}
                    args = ["--resume", resume]
                open_terminal(path, args)
                msg = "Session resumed in terminal" if resume else "Terminal opened with Claude Code"
        except (OSError, RuntimeError) as e:
            log(f"open error {path} {mode}: {e!r}")
            return {"ok": False, "error": str(e)}
        log(f"open {mode} resume={resume} {path}")
        self._store.upsert(path, mode=mode, last_opened=time.time(), opens=p.get("opens", 0) + 1, hidden=False)
        if self._store.data["settings"].get("minimize_after_open") and self._window:
            try:
                self._window.minimize()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "message": msg}

    def new_project(self, name, parent, mode="terminal", git_init=True, claude_md=True, extensions=None):
        name = re.sub(r'[<>:"/\\|?*]', "", (name or "").strip()).strip(". ")
        if not name or name.split(".")[0].upper() in WINDOWS_RESERVED:
            return {"ok": False, "error": "Enter a valid name"}
        parent = os.path.abspath(os.path.expanduser(parent or default_root()))
        path = os.path.join(parent, name)
        try:
            os.makedirs(path, exist_ok=True)
            git = which("git")
            if git_init and git and not os.path.exists(os.path.join(path, ".git")):
                subprocess.run([git, "init", "-q"], cwd=path, creationflags=CREATE_NO_WINDOW)
            md = os.path.join(path, "CLAUDE.md")
            if claude_md and not os.path.exists(md):
                with open(md, "w", encoding="utf-8") as f:
                    f.write(f"# {name}\n\n## Goal\n\n## Stack and conventions\n\n## Notes for Claude\n")
        except OSError as e:
            return {"ok": False, "error": str(e)}
        self._store.upsert(path, name=name, mode=mode, created=time.time(), hidden=False)
        if isinstance(extensions, dict) and extensions:
            try:
                if write_project_extensions(path, extensions, extension_inventory()) and git_init:
                    ensure_gitignored(path)
            except OSError as e:
                log(f"extensions not written for {path}: {e!r}")
        res = self.open_project(path, mode)
        res["path"] = path
        return res

    def open_desktop(self):
        try:
            open_claude_desktop()
        except (OSError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "message": "Claude Desktop opened"}

    # --- extensions
    def _known_project(self, path):
        if not path or not os.path.isdir(path):
            return False
        return self._store.get(path) is not None or any(key(s["cwd"]) == key(path) for s in scan_sessions())

    def get_extensions(self, path=None):
        items = extension_inventory()
        defaults = self._store.data["settings"].get("extension_defaults") or {}
        if path:
            if not self._known_project(path):
                return {"ok": False, "error": "Unknown project"}
            states = project_extension_state(path, items)
            for it in items:
                it["state"], it["inherited"] = states[it["id"]]
        else:
            for it in items:
                it["inherited"] = it["default"]
                it["state"] = bool(defaults[it["id"]]) if isinstance(defaults.get(it["id"]), bool) else it["default"]
        return {"ok": True, "items": items, "has_defaults": bool(defaults)}

    def set_extensions(self, path, states):
        if not self._known_project(path) or not isinstance(states, dict):
            return {"ok": False, "error": "Unknown project"}
        try:
            n = write_project_extensions(path, states, extension_inventory())
        except OSError as e:
            return {"ok": False, "error": str(e)}
        log(f"extensions saved for {path}: {n} override(s)")
        return {"ok": True, "overrides": n,
                "message": f"Saved {n} project override(s). Restart Claude Code in that folder (or run /reload-plugins) to apply."}

    def save_extension_defaults(self, states):
        if not isinstance(states, dict):
            return False
        valid = {i["id"] for i in extension_inventory()}
        clean = {k: v for k, v in states.items() if k in valid and isinstance(v, bool)}
        with self._store.lock:
            if clean:
                self._store.data["settings"]["extension_defaults"] = clean
            else:
                self._store.data["settings"].pop("extension_defaults", None)
            self._store.save()
        return True

    def add_existing(self, path, mode="terminal"):
        if not path or not os.path.isdir(path):
            return {"ok": False, "error": "Invalid folder"}
        self._store.upsert(path, mode=mode, hidden=False, last_opened=time.time())
        return {"ok": True}

    def browse_folder(self, start=None):
        import webview
        ftype = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        start = start if start and os.path.isdir(start) else default_root()
        res = self._window.create_file_dialog(ftype, directory=start)
        return res[0] if res else None

    def open_explorer(self, path):
        if os.path.isdir(path):
            if SYSTEM == "Windows":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if SYSTEM == "Darwin" else "xdg-open", path])
        return True

    def toggle_pin(self, path):
        p = self._store.get(path) or {}
        return self._store.upsert(path, pinned=not p.get("pinned", False))["pinned"]

    def set_mode(self, path, mode):
        if mode not in ("terminal", "desktop"):
            return False
        self._store.upsert(path, mode=mode)
        return True

    def rename(self, path, name):
        name = (name or "").strip()[:120]
        if name:
            self._store.upsert(path, name=name)
        return True

    def hide(self, path):
        self._store.upsert(path, hidden=True, pinned=False)
        return True

    def set_setting(self, name, value):
        if name not in ("minimize_after_open",):
            return False
        with self._store.lock:
            self._store.data["settings"][name] = value
            self._store.save()
        return True


# ------------------------------------------------------------------ single instance

def already_running():
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=0.5) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def listen_for_show(window):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", SINGLE_PORT))
    except OSError:
        return
    srv.listen(2)
    while True:
        conn, _ = srv.accept()
        with conn:
            if conn.recv(16) == b"show":
                try:
                    window.restore()
                    window.show()
                    window.on_top = True
                    window.on_top = False
                    window.evaluate_js("window.onShow && window.onShow()")
                except Exception:  # noqa: BLE001
                    pass


def selftest():
    t = time.time()
    st = Api().get_state()
    log(f"selftest get_state {time.time()-t:.2f}s: {len(st['projects'])} projects, {len(st['sessions'])} sessions, "
        f"last={st['last'] and st['last']['name']}")
    for p in st["projects"][:8]:
        log(f"  P {p['name']} | {p['mode']} | {p['sessions']} sess | {p['path']}")
    for s in st["sessions"][:5]:
        log(f"  S {s['project']} | {s['display'][:70]}")
    t = time.time()
    inv = extension_inventory()
    kinds = {k: sum(1 for i in inv if i["kind"] == k) for k in ("mod", "plugin", "skill")}
    log(f"selftest extensions {time.time()-t:.2f}s: {kinds}, off by default: "
        f"{[i['id'] for i in inv if not i['default']]}")
    t = time.time()
    u = Usage()._fetch()
    log(f"selftest usage {time.time()-t:.1f}s: status={u.get('status')} active={bool(u.get('active'))} err={u.get('error')}")


def setup_crash_logging():
    import faulthandler
    os.makedirs(DATA_DIR, exist_ok=True)
    out = open(os.path.join(DATA_DIR, "stderr.log"), "a", encoding="utf-8", buffering=1)
    out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()} argv={sys.argv[1:]}\n")
    if sys.stderr is None or "pythonw" in sys.executable.lower():
        sys.stderr = out
        sys.stdout = out
    faulthandler.enable(file=out, all_threads=True)

    def hook(exc_type, exc, tb):
        import traceback
        log("EXCEPTION: " + "".join(traceback.format_exception(exc_type, exc, tb))[-1500:])
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def set_windows_identity():
    if SYSTEM != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001
        pass


def apply_window_icon(window):
    if SYSTEM != "Windows" or not os.path.exists(ICON_FILE):
        return
    try:
        import clr  # provided by pythonnet, which pywebview uses on Windows
        clr.AddReference("System.Drawing")
        from System import Action
        from System.Drawing import Icon
        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(ICON_FILE))))
    except Exception as e:  # noqa: BLE001
        log(f"icon not applied: {e!r}")


def main():
    os.chdir(APP_DIR)  # executable lookups must never search a project folder
    setup_crash_logging()
    set_windows_identity()
    if "--selftest" in sys.argv:
        selftest()
        return
    if already_running():
        log("already running: asked existing window to show")
        return
    log(f"start pid={os.getpid()} python={sys.version.split()[0]}")
    try:
        import webview
    except ImportError:
        import tkinter as tk
        from tkinter import messagebox
        tk.Tk().withdraw()
        messagebox.showerror("Launchpad", "pywebview is missing.\nRun install.bat in the Launchpad folder.")
        return
    api = Api()
    window = webview.create_window("Launchpad for Claude Code", os.path.join(APP_DIR, "ui.html"), js_api=api,
                                   width=1180, height=780, min_size=(940, 620), background_color="#0c0c0f")
    api._window = window
    window.events.closed += lambda: log("window closed")
    window.events.shown += lambda: apply_window_icon(window)
    threading.Thread(target=api._usage.loop, daemon=True).start()
    threading.Thread(target=listen_for_show, args=(window,), daemon=True).start()
    try:
        webview.start(debug="--debug" in sys.argv)
    except Exception as e:  # noqa: BLE001
        log(f"webview.start failed: {e!r}")
        raise
    log("exit")


if __name__ == "__main__":
    main()
