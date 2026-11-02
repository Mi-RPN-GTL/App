import base64
import glob
import hashlib
import json
import os
import re
import shutil
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
BT = os.environ.get("BT", "36.0.0")
SDK = os.environ.get("ANDROID_HOME", "")
WS = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
FACTS = os.path.join(WS, "build-facts.txt")
LOG = os.path.join(WS, "build.log")


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


def find(name):
    for a in release()["assets"]:
        if a["name"] == name:
            return a
    return None


def drop(name):
    a = find(name)
    if a:
        jcall("DELETE", "/repos/" + REPO + "/releases/assets/" + str(a["id"]))


def upload(name, path, rid=None):
    rid = rid or REL
    if rid == REL:
        drop(name)
    with open(path, "rb") as f:
        data = f.read()
    code, out = call("POST", "https://uploads.github.com/repos/" + REPO + "/releases/" + str(rid) + "/assets?name=" + urllib.parse.quote(name), data, "application/octet-stream")
    if code >= 300:
        raise SystemExit("upload " + name + " " + str(code) + " " + out.decode(errors="replace")[:300])
    return json.loads(out)


def fetch_asset(name, dest):
    a = find(name)
    if not a:
        raise SystemExit("asset " + name + " is missing")
    with open(dest, "wb") as o:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", "/repos/" + REPO + "/releases/assets/" + str(a["id"])], stdout=o, check=True)


def state():
    last = None
    for i in range(3):
        try:
            return json.loads(release().get("body") or "{}")
        except Exception as e:
            last = e
            time.sleep(2)
    raise SystemExit("could not read the build record: " + str(last))


def set_state(**kw):
    s = state()
    s.update(kw)
    jcall("PATCH", "/repos/" + REPO + "/releases/" + REL, {"body": json.dumps(s)})
    return s


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def info(kind, name, path, asset):
    return {"kind": kind, "name": name, "size": os.path.getsize(path), "sha256": sha(path), "assetId": asset["id"]}


def seal(enc_name):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    priv = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    with open("seal-pub.pem", "wb") as f:
        f.write(pem)
    upload("seal-pub.pem", "seal-pub.pem")
    end = time.time() + 600
    while time.time() < end:
        if find(enc_name):
            break
        time.sleep(4)
    else:
        raise SystemExit("the phone did not answer in time")
    fetch_asset(enc_name, "seal.enc")
    box = json.load(open("seal.enc"))
    key = priv.decrypt(base64.b64decode(box["k"]), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    plain = AESGCM(key).decrypt(base64.b64decode(box["n"]), base64.b64decode(box["c"]), None)
    drop("seal-pub.pem")
    drop(enc_name)
    os.remove("seal.enc")
    return json.loads(plain)


def safe_rel(p):
    n = os.path.normpath(p)
    return not (n.startswith("..") or os.path.isabs(n))


def cmd_fetch():
    fetch_asset("source.zip", "src.zip")
    shutil.rmtree("proj", ignore_errors=True)
    with zipfile.ZipFile("src.zip") as z:
        z.extractall("proj")
    set_state(state="building", runId=os.environ.get("GITHUB_RUN_ID", ""))


def cmd_seal(enc_name, then):
    data = seal(enc_name)
    if then == "apply":
        for rel, b64 in data.get("files", {}).items():
            if not safe_rel(rel):
                continue
            dest = os.path.join("proj", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(base64.b64decode(b64))
        k = data.get("keys")
        if k:
            with open("keys.json", "w") as f:
                json.dump(k, f)
            os.chmod("keys.json", 0o600)


def cmd_finish_build():
    out = os.path.join("proj", "build", "outputs")
    pairs = [
        ("DebugApk", "debug-unsigned.apk", glob.glob(out + "/apk/debug/*.apk")),
        ("ReleaseApk", "release-unsigned.apk", glob.glob(out + "/apk/release/*.apk")),
        ("Aab", "release-unsigned.aab", glob.glob(out + "/bundle/release/*.aab")),
        ("Mapping", "mapping.txt", glob.glob(out + "/mapping/release/mapping.txt")),
        ("Log", "build.log", ["build.log"] if os.path.exists("build.log") else []),
    ]
    files = []
    for kind, name, found in pairs:
        if not found:
            continue
        files.append(info(kind, name, found[0], upload(name, found[0])))
    if os.path.exists(FACTS):
        upload("build-facts.txt", FACTS)
    set_state(state="built", files=files, finishedAt=int(time.time()))


class RunFail(Exception):
    pass


def run(cmd, env=None):
    p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode(errors="replace")
    head = os.path.basename(cmd[0]) + (" " + cmd[1] if len(cmd) > 1 else "")
    with open("sign.log", "a") as f:
        f.write("$ " + head + " exit " + str(p.returncode) + "\n" + out + "\n")
    print(out)
    if p.returncode != 0:
        raise RunFail(head + " failed (exit " + str(p.returncode) + ")\n" + out.strip()[-600:])


def sign_apk(bt, kstype, alias, src, dst, env):
    base = [os.path.join(bt, "apksigner"), "sign", "--ks", "ks.p12", "--ks-type", kstype, "--ks-pass", "env:KSPASS"]
    tries = [base + ["--ks-key-alias", alias, "--key-pass", "env:KEYPASS"], base + ["--ks-key-alias", alias], base]
    last = None
    for t in tries:
        try:
            run(t + ["--out", dst, src], env)
            return
        except RunFail as e:
            last = e
    raise last


def cmd_sign():
    s = state()
    unsigned = {f["name"]: f for f in s.get("files", [])}
    for name in ("debug-unsigned.apk", "release-unsigned.apk", "release-unsigned.aab"):
        if name in unsigned:
            fetch_asset(name, name)
    set_state(state="signing")
    keys = json.load(open("keys.json")) if os.path.exists("keys.json") else seal("seal-keys.enc")
    with open("ks.p12", "wb") as f:
        f.write(base64.b64decode(keys["keystore"]))
    env = dict(os.environ, KSPASS=keys["storePass"], KEYPASS=keys["keyPass"])
    kstype = keys.get("type", "PKCS12")
    bt = os.path.join(SDK, "build-tools", BT)
    done = []
    for src, name, kind in (("debug-unsigned.apk", "debug.apk", "DebugApk"), ("release-unsigned.apk", "release.apk", "ReleaseApk")):
        if not os.path.exists(src):
            continue
        run([os.path.join(bt, "zipalign"), "-p", "-f", "4", src, "aligned-" + name])
        sign_apk(bt, kstype, keys["alias"], "aligned-" + name, name, env)
        run([os.path.join(bt, "apksigner"), "verify", name])
        done.append((kind, name))
    if os.path.exists("release-unsigned.aab"):
        shutil.copy("release-unsigned.aab", "release.aab")
        run(["jarsigner", "-keystore", "ks.p12", "-storetype", kstype, "-storepass:env", "KSPASS", "-keypass:env", "KEYPASS", "release.aab", keys["alias"]], env)
        done.append(("Aab", "release.aab"))
    files = [f for f in s.get("files", []) if f["name"] not in ("debug-unsigned.apk", "release-unsigned.apk", "release-unsigned.aab")]
    for kind, name in done:
        files.append(info(kind, name, name, upload(name, name)))
    for old in unsigned:
        if old.endswith("-unsigned.apk") or old.endswith("-unsigned.aab"):
            drop(old)
    pub = {}
    tag = os.environ.get("PUB_TAG", "")
    if tag:
        for kind, name, ptag, pre in (("ReleaseApk", "release.apk", tag + "-release", False), ("DebugApk", "debug.apk", tag + "-debug", True)):
            if not os.path.exists(name):
                continue
            for r in jcall("GET", "/repos/" + REPO + "/releases?per_page=100"):
                if r["draft"] and r["tag_name"] == ptag:
                    jcall("DELETE", "/repos/" + REPO + "/releases/" + str(r["id"]))
            made = jcall("POST", "/repos/" + REPO + "/releases", {"tag_name": ptag, "name": ptag, "body": "", "draft": True, "prerelease": pre})
            asset = upload(ptag + ".apk", name, made["id"])
            pub[kind] = {"releaseId": made["id"], "assetId": asset["id"], "tag": ptag, "name": asset["name"]}
    for tmp in ("ks.p12", "keys.json"):
        if os.path.exists(tmp):
            os.remove(tmp)
    set_state(state="ready", files=files, pub=pub, signedAt=int(time.time()), error="")


def cmd_sign_inline():
    if os.path.exists("keys.json"):
        cmd_sign()
    else:
        print("no signing key was sent; the files stay unsigned")


def fact(text):
    with open(FACTS, "a") as f:
        f.write(text + "\n")


def capture(cmd, cwd=None, limit=240):
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=limit)
        return p.stdout.decode(errors="replace")
    except Exception as e:
        return "(" + " ".join(cmd[:2]) + " did not run: " + str(e) + ")"


def cmd_env_facts():
    g = capture(["gradle", "--version", "--console=plain"])
    for key in ("Gradle ", "Kotlin:", "Launcher JVM:", "Daemon JVM:"):
        for line in g.splitlines():
            if line.startswith(key):
                fact("runtime: " + line.strip())
                break
    fact("runtime: runner " + os.environ.get("RUNNER_OS", "?") + " " + os.environ.get("ImageVersion", ""))


HEAL = re.compile(r"unresolved reference:? '?serializer|not enough information to infer type variable|cannot infer a type|is ambiguous|could not (?:resolve|download|get)|daemon (?:disappeared|has terminated|crashed)|connection to the daemon|outofmemory|metaspace|incompatible version|incompatible binary|could not load module|unable to load class|already registered|corrupt", re.I)
SERIAL = re.compile(r"unresolved reference:? '?serializer", re.I)
TASKS = ["compileDebugKotlin", "compileDebugJavaWithJavac", "compileReleaseKotlin", "compileReleaseJavaWithJavac"]


def gradle_run(args, tag):
    with open(LOG, "a") as f:
        f.write("[aot] " + tag + "\n")
        f.flush()
        p = subprocess.run(["gradle"] + args + ["--console=plain"], stdout=f, stderr=subprocess.STDOUT)
    return p.returncode


def log_from(offset):
    with open(LOG, "rb") as f:
        f.seek(offset)
        return f.read().decode(errors="replace")


def serial_plugin_on():
    try:
        return "plugin.serialization" in open("build.gradle.kts").read()
    except Exception:
        return False


def cmd_compile():
    start = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    rc = gradle_run(TASKS, "compile attempt 1")
    fact("compile attempt 1: exit " + str(rc))
    if rc == 0:
        return 0
    seg = log_from(start)
    if not HEAL.search(seg):
        fact("compile: the failure looks like a source error, so no retry")
        return rc
    plan = []
    if SERIAL.search(seg) and not serial_plugin_on():
        plan.append("force-serialization")
    plan.append("clean-rerun")
    n = 1
    for step in plan:
        n += 1
        if step == "force-serialization":
            env = dict(os.environ, AOT_FORCE_SERIAL="1")
            subprocess.run([sys.executable, os.path.join(WS, ".github", "aot", "aot_gradle_gen.py"), ".", FACTS], env=env, check=False)
            args = list(TASKS)
        else:
            subprocess.run(["gradle", "--stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for d in (".gradle", "build"):
                shutil.rmtree(d, ignore_errors=True)
            args = ["--rerun-tasks", "--no-build-cache", "--no-daemon", "-Pkotlin.compiler.execution.strategy=in-process", "-Pkotlin.incremental=false"] + TASKS
        rc = gradle_run(args, "compile attempt " + str(n) + " (" + step + ")")
        fact("compile attempt " + str(n) + " (" + step + "): exit " + str(rc))
        if rc == 0:
            fact("compile healed by: " + step)
            return 0
    return rc


KEEP = re.compile(r"kotlin-gradle-plugin|kotlin-stdlib|serialization|compose-compiler|compose.*plugin|com\.android\.tools\.build:gradle|kotlin-compiler", re.I)


def diag():
    seen = []
    for label, args in (("classpath", ["buildEnvironment"]), ("deps", ["dependencies", "--configuration", "debugCompileClasspath"])):
        out = capture(["gradle", "-q", "--console=plain"] + args, cwd="proj" if os.path.isdir("proj") else ".", limit=300)
        for line in out.splitlines():
            t = line.strip(" |+\\-")
            if KEEP.search(t) and (label + ": " + t) not in seen:
                seen.append(label + ": " + t)
    for t in seen[:60]:
        fact("diag " + t)
    if not seen:
        fact("diag: no kotlin or serialization entries could be read from gradle")


def cmd_fail():
    step = os.environ.get("AOT_STEP", "")
    if os.path.isdir("proj"):
        try:
            diag()
        except Exception as e:
            fact("diag stopped: " + str(e))
    if os.path.exists(FACTS):
        upload("build-facts.txt", FACTS)
    if os.path.exists("build.log"):
        upload("build.log", "build.log")
    err = ""
    if os.path.exists("sign.log"):
        upload("sign.log", "sign.log")
        err = open("sign.log", errors="replace").read().strip()[-700:]
    set_state(state="failed", failedAt=int(time.time()), step=step, error=err)


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "fetch":
        cmd_fetch()
    elif c == "seal":
        cmd_seal(sys.argv[2], sys.argv[3])
    elif c == "finish-build":
        cmd_finish_build()
    elif c == "sign":
        cmd_sign()
    elif c == "sign-inline":
        cmd_sign_inline()
    elif c == "env-facts":
        cmd_env_facts()
    elif c == "compile":
        sys.exit(cmd_compile())
    elif c == "fail":
        cmd_fail()
