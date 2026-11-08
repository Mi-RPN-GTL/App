import hashlib
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
REL = os.environ["REL"]
API = "https://api.github.com"
JADX_VERSION = os.environ.get("JADX_VERSION", "1.5.2")
JADX_SHA256 = os.environ.get("JADX_SHA256", "").strip().lower()
MAX_OUT = 90 * 1024 * 1024
BAD = ("Method dump skipped", "JADX WARN: Code restructure failed", "Can't load method", "Code decompiled incorrectly", "JADX WARN: Failed to decompile")
RENAMED = re.compile(r"renamed from: ([\w.$]+)")
OBF = re.compile(r"^(C\d+[a-z]*|zz[a-z]+|[a-z]{1,3})$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,59}$")
H_SERIAL = re.compile(r'SerialDescriptor\(\s*"([\w.$]{3,120})"')
H_TOSTR = re.compile(r'"([A-Z][A-Za-z0-9_]{2,40})[({][a-z][A-Za-z0-9_]*="')
H_SOURCE = re.compile(r"compiled from: ([A-Za-z_][A-Za-z0-9_$]*)\.(?:kt|java)")
H_TAG = re.compile(r'\bTAG\s*=\s*"([A-Za-z_][A-Za-z0-9_]{2,40})"')


def call(method, url, data=None, ctype="application/json"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jcall(method, path, body=None):
    code, out = call(method, API + path, None if body is None else json.dumps(body).encode())
    if code >= 300 and not (method == "DELETE" and code == 404):
        raise SystemExit(method + " " + path + " " + str(code) + " " + out.decode(errors="replace")[:300])
    return json.loads(out) if out else {}


def release():
    return jcall("GET", "/repos/" + REPO + "/releases/" + REL)


def assets():
    return release().get("assets", [])


def fetch_asset(a, dest):
    with open(dest, "wb") as o:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", "/repos/" + REPO + "/releases/assets/" + str(a["id"])], stdout=o, check=True)


def upload(name, path):
    for a in assets():
        if a["name"] == name:
            jcall("DELETE", "/repos/" + REPO + "/releases/assets/" + str(a["id"]))
    with open(path, "rb") as f:
        data = f.read()
    code, out = call("POST", "https://uploads.github.com/repos/" + REPO + "/releases/" + str(REL) + "/assets?name=" + urllib.parse.quote(name), data, "application/octet-stream")
    if code >= 300:
        raise SystemExit("upload " + name + " " + str(code) + " " + out.decode(errors="replace")[:300])


def state():
    try:
        return json.loads(release().get("body") or "{}")
    except Exception:
        return {}


def set_state(**kw):
    s = state()
    s.update(kw)
    jcall("PATCH", "/repos/" + REPO + "/releases/" + REL, {"body": json.dumps(s)})


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cmd_tool():
    url = "https://github.com/skylot/jadx/releases/download/v" + JADX_VERSION + "/jadx-" + JADX_VERSION + ".zip"
    urllib.request.urlretrieve(url, "jadx.zip")
    got = sha("jadx.zip")
    print("jadx " + JADX_VERSION + " sha256 " + got)
    if JADX_SHA256 and got != JADX_SHA256:
        raise SystemExit("jadx download does not match the pinned hash")
    shutil.rmtree("jadx", ignore_errors=True)
    with zipfile.ZipFile("jadx.zip") as z:
        z.extractall("jadx")
    for n in ("jadx", "jadx-gui"):
        p = os.path.join("jadx", "bin", n)
        if os.path.exists(p):
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def cmd_fetch():
    os.makedirs("in", exist_ok=True)
    got = 0
    for a in assets():
        n = a["name"]
        if n == "targets.txt":
            fetch_asset(a, "targets.txt")
        elif re.fullmatch(r"classes\d*\.dex\.xz", n):
            tmp = os.path.join("in", n)
            fetch_asset(a, tmp)
            with lzma.open(tmp, "rb") as src, open(os.path.join("in", n[:-3]), "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(tmp)
            got += 1
    if got == 0:
        raise SystemExit("no dex files were uploaded")
    if not os.path.exists("targets.txt"):
        raise SystemExit("targets.txt was not uploaded")
    set_state(state="running", runId=os.environ.get("GITHUB_RUN_ID", ""))


def targets():
    out = set()
    with open("targets.txt", encoding="utf-8") as f:
        for line in f:
            n = line.strip()
            if n:
                out.add(n)
    return out


def top(name):
    return name.split("$")[0]


def looks_obf(simple):
    return len(simple) <= 2 or bool(OBF.match(simple))


def name_hints(text, simple):
    if not looks_obf(simple):
        return None
    found = []
    m = H_SERIAL.search(text)
    if m:
        found.append((m.group(1).replace("$", ".").split(".")[-1], "serializer"))
    m = H_TOSTR.search(text)
    if m:
        found.append((m.group(1), "toString"))
    m = H_SOURCE.search(text)
    if m:
        found.append((m.group(1).split("$")[0], "SourceFile"))
    m = H_TAG.search(text)
    if m:
        found.append((m.group(1), "TAG"))
    for name, src in found:
        if IDENT.match(name) and name != simple and name != "SourceFile":
            return name, src
    return None


def cmd_decompile():
    dex = sorted(os.path.join("in", n) for n in os.listdir("in") if n.endswith(".dex"))
    shutil.rmtree("out", ignore_errors=True)
    env = dict(os.environ)
    env["JAVA_OPTS"] = "-Xmx6g"
    cmd = [os.path.join("jadx", "bin", "jadx"), "--deobf", "--show-bad-code", "--no-res", "-j", "2", "-d", "out"] + dex
    try:
        helptext = subprocess.run([cmd[0], "--help"], capture_output=True, text=True, timeout=120).stdout
        for line in helptext.splitlines():
            if "sourcename" in line.lower():
                print("jadx option: " + line.strip())
    except Exception:
        pass
    with open("jadx.log", "wb") as log:
        code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env).returncode
    print("jadx exit " + str(code))
    if not os.path.isdir(os.path.join("out", "sources")):
        raise SystemExit("jadx produced no sources (exit " + str(code) + ")")


def cmd_pack():
    want = {top(n) for n in targets()}
    root = os.path.join("out", "sources")
    found = set()
    bad = set()
    spent = 0
    truncated = 0
    kept = 0
    hints = {}
    with zipfile.ZipFile("result.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for d, _, files in os.walk(root):
            for f in files:
                if not f.endswith(".java"):
                    continue
                p = os.path.join(d, f)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                name = rel[:-5].replace("/", ".")
                with open(p, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                origs = {top(o) for o in RENAMED.findall(text)}
                hit = ({top(name)} | origs) & want
                if not hit:
                    continue
                orig = sorted(origs)[0] if origs else top(name)
                if orig not in hints:
                    h = name_hints(text, orig.split(".")[-1])
                    if h:
                        hints[orig] = h
                if spent + len(text) > MAX_OUT:
                    truncated += 1
                    continue
                z.writestr("java/" + rel, text)
                spent += len(text)
                kept += 1
                found |= hit
                if any(m in text for m in BAD):
                    bad |= hit
        rows = ["original\tsuggested\tsource"] + [k + "\t" + v[0] + "\t" + v[1] for k, v in sorted(hints.items())]
        z.writestr("names.tsv", "\n".join(rows) + "\n")
        report = {
            "tool": "jadx " + JADX_VERSION,
            "files": kept,
            "bytes": spent,
            "truncated": truncated,
            "names": len(hints),
            "missing": sorted(want - found),
            "bad": sorted(bad),
        }
        z.writestr("report.json", json.dumps(report, indent=1))
    upload("result.zip", "result.zip")
    set_state(state="done", finishedAt=int(time.time()), files=kept, missing=len(report["missing"]), bad=len(report["bad"]))


def cmd_fail():
    err = ""
    if os.path.exists("jadx.log"):
        err = open("jadx.log", errors="replace").read().strip()[-600:]
    set_state(state="failed", failedAt=int(time.time()), step=os.environ.get("AOT_STEP", ""), error=err)


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "tool":
        cmd_tool()
    elif c == "fetch":
        cmd_fetch()
    elif c == "decompile":
        cmd_decompile()
    elif c == "pack":
        cmd_pack()
    elif c == "fail":
        cmd_fail()
    else:
        raise SystemExit("unknown command " + c)
