#!/usr/bin/env python3
"""Talk to a running Tabletop Simulator over its External Editor API.

TTS listens on 127.0.0.1:39999 for JSON commands and connects back to
127.0.0.1:39998 to deliver print() output, script errors, return values,
and script dumps. Only one program may own 39998 at a time (the VS Code
TTS extension also wants it), so run exactly one `listen`.

Usage (stdlib only, Python 3.8+; on this machine `python` is the Store stub,
so use `uv run --no-project python tools/tts_dev.py ...`):

  tts_dev.py listen                 # keep running; logs everything
  tts_dev.py exec 'print("hi")'     # run Lua in Global
  tts_dev.py exec -f snippet.lua --guid abc123
  tts_dev.py exec 'return JSON.encode({n = #getAllObjects()})' --wait
  tts_dev.py tail [-n 40]           # last messages from the log
  tts_dev.py get                    # dump live scripts to build/live-scripts/
  tts_dev.py diff [--show]          # compare live scripts to the repo
  tts_dev.py push --yes             # Save & Play: reload game with repo scripts
  tts_dev.py repack [--install]     # tts repack (+ copy into TTS Saves folder)

The listener appends one JSON object per line to build/tts-messages.jsonl;
`exec --wait`, `get` and `diff` read that file, so `listen` must be running
for them to see replies.

Gotchas learned the hard way:
  * A Lua `return` of a table produces no reply at all. Return a string,
    number or bool; for structured data `return JSON.encode(t)` and `exec`
    unwraps it.
  * `push` only hot-swaps Global and top-level objects (matched by GUID).
    Scripts on objects inside bags/decks (objects/*/contained/...) need
    `repack --install` and loading the save in TTS.
  * `push` triggers Save & Play, which saves over and reloads whatever
    game is open in TTS. Load a dev save first.
"""
import argparse
import json
import random
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build"
LOG = BUILD / "tts-messages.jsonl"
LIVE = BUILD / "live-scripts"
TTS_IN = ("127.0.0.1", 39999)   # we send here
TTS_OUT = ("127.0.0.1", 39998)  # TTS sends here

# messageID -> name, for messages TTS sends to us
INCOMING = {
    0: "push_object",      # scriptStates for an object opened via "Scripting Editor"
    1: "load_game",        # all scriptStates (after load, or reply to `get`)
    2: "print",            # print()/log() output -> message
    3: "error",            # error, guid, errorMessagePrefix
    4: "custom",           # sendExternalMessage() -> customMessage
    5: "return",           # returnID, returnValue
    6: "game_saved",
    7: "object_created",   # guid
}


# ---------------------------------------------------------------- transport
def send(msg):
    """Send one JSON command to TTS. Raises ConnectionRefusedError if TTS is not up."""
    data = json.dumps(msg).encode("utf-8")
    with socket.create_connection(TTS_IN, timeout=5) as s:
        s.sendall(data)


def ping():
    try:
        with socket.create_connection(TTS_IN, timeout=2):
            return True
    except OSError:
        return False


def read_log(since_pos=0):
    """Yield (pos, record) for log lines written after byte offset since_pos."""
    if not LOG.exists():
        return
    with LOG.open("rb") as f:
        f.seek(since_pos)
        while True:
            line = f.readline()
            if not line:
                return
            try:
                yield f.tell(), json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue


def log_size():
    return LOG.stat().st_size if LOG.exists() else 0


def wait_for(pred, start_pos, timeout):
    """Poll the log until pred(record) is true or timeout (seconds) elapses."""
    deadline = time.time() + timeout
    pos = start_pos
    while time.time() < deadline:
        for pos, rec in read_log(pos):
            if pred(rec):
                return rec
        time.sleep(0.1)
    return None


def summarize(rec):
    """One human-readable line for a message from TTS."""
    mid = rec.get("messageID")
    name = INCOMING.get(mid, "msg%s" % mid)
    ts = time.strftime("%H:%M:%S", time.localtime(rec.get("_ts", time.time())))
    if mid == 2:
        body = rec.get("message", "")
    elif mid == 3:
        body = "%s %s  [guid %s]" % (rec.get("errorMessagePrefix", ""), rec.get("error", ""), rec.get("guid"))
    elif mid == 4:
        body = json.dumps(rec.get("customMessage"))
    elif mid == 5:
        body = "#%s -> %s" % (rec.get("returnID"), json.dumps(rec.get("returnValue")))
    elif mid in (0, 1):
        states = rec.get("scriptStates", [])
        body = "%d script state(s)%s" % (len(states), (" from " + rec["savePath"]) if rec.get("savePath") else "")
    elif mid == 7:
        body = "guid %s" % rec.get("guid")
    else:
        body = json.dumps({k: v for k, v in rec.items() if k not in ("messageID", "_ts")})[:200]
    return "%s %-14s %s" % (ts, name, body)


# ----------------------------------------------------------------- commands
def cmd_listen(args):
    BUILD.mkdir(exist_ok=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(TTS_OUT)
    except OSError as e:
        sys.exit("cannot bind %s:%d (%s). Another listener (VS Code TTS extension?) owns it." % (*TTS_OUT, e))
    srv.listen(8)
    print("listening on %s:%d, logging to %s" % (*TTS_OUT, LOG), flush=True)
    while True:
        conn, _ = srv.accept()
        chunks = []
        with conn:
            conn.settimeout(10)
            try:
                while True:
                    b = conn.recv(65536)
                    if not b:
                        break
                    chunks.append(b)
            except socket.timeout:
                pass
        raw = b"".join(chunks).decode("utf-8", "replace")
        # TTS normally sends one JSON object per connection; tolerate several.
        dec = json.JSONDecoder()
        i = 0
        while i < len(raw):
            while i < len(raw) and raw[i].isspace():
                i += 1
            if i >= len(raw):
                break
            try:
                rec, j = dec.raw_decode(raw, i)
            except json.JSONDecodeError:
                print("unparseable: %r" % raw[i:i + 200], flush=True)
                break
            i = j
            rec["_ts"] = time.time()
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            print(summarize(rec), flush=True)


def cmd_tail(args):
    recs = [r for _, r in read_log()]
    for r in recs[-args.n:]:
        print(summarize(r))


def cmd_exec(args):
    code = Path(args.file).read_text(encoding="utf-8") if args.file else args.code
    if code is None:
        sys.exit("give Lua code as an argument or with -f FILE")
    rid = random.randint(1, 2**31 - 1)
    start = log_size()
    send({"messageID": 3, "guid": args.guid, "script": code, "returnID": rid})
    if not args.wait:
        print("sent (returnID %d)" % rid)
        return

    def done(r):
        return (r.get("messageID") == 5 and r.get("returnID") == rid) or r.get("messageID") == 3

    rec = wait_for(done, start, args.timeout)
    if rec is None:
        sys.exit("no reply within %ss (is `listen` running?)" % args.timeout)
    if rec.get("messageID") == 3:
        sys.exit("lua error: %s %s" % (rec.get("errorMessagePrefix", ""), rec.get("error", "")))
    val = rec.get("returnValue")
    if isinstance(val, str):
        # Lua tables never come back; `return JSON.encode(t)` does, so unwrap it.
        try:
            val = json.loads(val)
        except ValueError:
            pass
    print(json.dumps(val, indent=2, ensure_ascii=False))


def _state_path(guid):
    return LIVE / ("global" if guid == "-1" else guid)


def cmd_get(args):
    start = log_size()
    send({"messageID": 0})
    rec = wait_for(lambda r: r.get("messageID") == 1, start, args.timeout)
    if rec is None:
        sys.exit("no script dump within %ss (is `listen` running?)" % args.timeout)
    if LIVE.exists():
        shutil.rmtree(LIVE)
    LIVE.mkdir(parents=True)
    for st in rec["scriptStates"]:
        d = _state_path(st["guid"])
        d.mkdir(exist_ok=True)
        (d / "name.txt").write_text(st.get("name", ""), encoding="utf-8")
        if st.get("script"):
            (d / "script.lua").write_text(st["script"], encoding="utf-8")
        if st.get("ui"):
            (d / "ui.xml").write_text(st["ui"], encoding="utf-8")
    print("wrote %d object(s) to %s (save: %s)" % (len(rec["scriptStates"]), LIVE, rec.get("savePath")))
    return rec


def repo_states():
    """scriptStates for Global and every top-level object with a script or UI in the repo.

    Only top-level objects can be hot-swapped; objects inside bags/decks
    (objects/*/contained/...) are only reachable by repack + load.
    """
    states = [{"guid": "-1", "script": (REPO / "script.lua").read_text(encoding="utf-8"),
               "ui": (REPO / "ui.xml").read_text(encoding="utf-8") if (REPO / "ui.xml").exists() else ""}]
    for d in sorted((REPO / "objects").iterdir()):
        if not d.is_dir():
            continue
        lua, xml = d / "script.lua", d / "ui.xml"
        if not (lua.exists() or xml.exists()):
            continue
        st = {"guid": d.name}
        if lua.exists():
            st["script"] = lua.read_text(encoding="utf-8")
        if xml.exists():
            st["ui"] = xml.read_text(encoding="utf-8")
        states.append(st)
    return states


def cmd_diff(args):
    rec = cmd_get(args)
    live = {st["guid"]: st for st in rec["scriptStates"]}
    repo = {st["guid"]: st for st in repo_states()}
    changed = []
    for guid, st in repo.items():
        l = live.get(guid)
        if l is None:
            changed.append((guid, "not in live game"))
            continue
        if (st.get("script") or "").strip() != (l.get("script") or "").strip():
            changed.append((guid, "script differs"))
        if (st.get("ui") or "").strip() != (l.get("ui") or "").strip():
            changed.append((guid, "ui differs"))
    for guid in live:
        if guid not in repo and (live[guid].get("script") or live[guid].get("ui")):
            changed.append((guid, "live object has script but repo has none (spawned/contained?)"))
    if not changed:
        print("live game matches repo for all top-level scripted objects")
    for guid, why in changed:
        name = live.get(guid, {}).get("name", "")
        print("%-7s %-30s %s" % (guid, name[:30], why))
    if args.show:
        import difflib
        for guid, why in changed:
            if guid not in live or guid not in repo:
                continue
            key = "ui" if why.startswith("ui") else "script"
            ext = "ui.xml" if key == "ui" else "script.lua"
            a = (live[guid].get(key) or "").splitlines()
            b = (repo[guid].get(key) or "").splitlines()
            label = "global" if guid == "-1" else guid
            sys.stdout.writelines(l + "\n" for l in difflib.unified_diff(
                a, b, "live/%s/%s" % (label, ext), "repo/%s/%s" % (label, ext), lineterm="", n=2))


def cmd_push(args):
    if not args.yes:
        sys.exit("push does Save & Play: it saves and RELOADS the game currently open in TTS. Re-run with --yes.")
    states = repo_states()
    send({"messageID": 1, "scriptStates": states})
    print("sent Save & Play with %d script state(s); TTS is reloading" % len(states))


def saves_dir():
    for base in (Path.home() / "Documents", Path.home() / "OneDrive" / "Documents"):
        p = base / "My Games" / "Tabletop Simulator" / "Saves"
        if p.is_dir():
            return p
    return None


def cmd_repack(args):
    # tabletop-tools depends on bson 0.5.10, whose setup.py uses pkgutil.find_loader
    # (removed in Python 3.14), so build it on 3.13 regardless of the default python.
    cmd = ["uv", "run", "--python", "3.13", "--with", "tabletop-tools~=0.7.1", "tts", "repack"]
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=REPO, check=True)
    out = BUILD / "packed-savegame.json"
    print("built", out, "(%.1f MB)" % (out.stat().st_size / 1e6))
    if args.install:
        d = saves_dir()
        if d is None:
            sys.exit("could not find the TTS Saves folder")
        dest = d / args.install
        shutil.copyfile(out, dest)
        print("copied to", dest, "- load it in TTS via Games > Save & Load")


def cmd_ping(args):
    print("TTS editor port %s:%d is %s" % (*TTS_IN, "open" if ping() else "closed (TTS not running?)"))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping").set_defaults(fn=cmd_ping)
    sub.add_parser("listen").set_defaults(fn=cmd_listen)
    t = sub.add_parser("tail")
    t.add_argument("-n", type=int, default=40)
    t.set_defaults(fn=cmd_tail)
    e = sub.add_parser("exec")
    e.add_argument("code", nargs="?")
    e.add_argument("-f", "--file")
    e.add_argument("--guid", default="-1", help="object GUID to run in (default: Global)")
    e.add_argument("--wait", action="store_true", help="wait for the return value via the listener log")
    e.add_argument("--timeout", type=float, default=10)
    e.set_defaults(fn=cmd_exec)
    g = sub.add_parser("get")
    g.add_argument("--timeout", type=float, default=15)
    g.set_defaults(fn=cmd_get)
    d = sub.add_parser("diff")
    d.add_argument("--timeout", type=float, default=15)
    d.add_argument("--show", action="store_true", help="print unified diffs, not just a summary")
    d.set_defaults(fn=cmd_diff)
    u = sub.add_parser("push")
    u.add_argument("--yes", action="store_true")
    u.set_defaults(fn=cmd_push)
    r = sub.add_parser("repack")
    r.add_argument("--install", nargs="?", const="SpiritIslandDev.json", metavar="FILENAME",
                   help="also copy the build into the TTS Saves folder")
    r.set_defaults(fn=cmd_repack)
    args = p.parse_args()
    try:
        args.fn(args)
    except ConnectionRefusedError:
        sys.exit("TTS is not listening on %s:%d (is it running with a game loaded?)" % TTS_IN)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
