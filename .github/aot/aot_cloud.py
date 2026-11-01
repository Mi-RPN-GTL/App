import base64
import glob
import hashlib
import json
import os
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
    try:
        return json.loads(release().get("body") or "{}")
    except Exception:
        return {}


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
    set_state(state="built", files=files, finishedAt=int(time.time()))


def run(cmd, env=None):
    subprocess.run(cmd, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def cmd_sign():
    s = state()
    unsigned = {f["name"]: f for f in s.get("files", [])}
    for name in ("debug-unsigned.apk", "release-unsigned.apk", "release-unsigned.aab"):
        if name in unsigned:
            fetch_asset(name, name)
    set_state(state="signing")
    keys = seal("seal-keys.enc")
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
        run([os.path.join(bt, "apksigner"), "sign", "--ks", "ks.p12", "--ks-type", kstype, "--ks-key-alias", keys["alias"], "--ks-pass", "env:KSPASS", "--key-pass", "env:KEYPASS", "--out", name, "aligned-" + name], env)
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
    set_state(state="ready", files=files, pub=pub, signedAt=int(time.time()))


def cmd_fail():
    step = os.environ.get("AOT_STEP", "")
    if os.path.exists("build.log"):
        upload("build.log", "build.log")
    set_state(state="failed", failedAt=int(time.time()), step=step)


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
    elif c == "fail":
        cmd_fail()
