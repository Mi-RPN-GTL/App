import json
import os
import re
import sys

PASS = "211"
AGP = "8.13.0"
KOTLIN = "2.0.21"
COMPOSE_BOM = "2024.11.00"
SERIALIZATION_JSON = "1.7.3"
COMPILE_SDK = 36
NEEDLE = "kotlinx.serialization"
SCAN_MAX = 1024 * 1024

IMPLICIT = [
    ("androidx.activity.compose", "androidx.activity:activity-compose", "androidx.activity:activity-compose:1.9.3"),
    ("androidx.core.", "androidx.core:core-ktx", "androidx.core:core-ktx:1.13.1"),
    ("androidx.lifecycle.viewmodel.compose", "androidx.lifecycle:lifecycle-viewmodel-compose", "androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7"),
    ("androidx.lifecycle.compose", "androidx.lifecycle:lifecycle-runtime-compose", "androidx.lifecycle:lifecycle-runtime-compose:2.8.7"),
    ("androidx.navigation.compose", "androidx.navigation:navigation-compose", "androidx.navigation:navigation-compose:2.8.4"),
    ("kotlinx.serialization", "org.jetbrains.kotlinx:kotlinx-serialization-json", "org.jetbrains.kotlinx:kotlinx-serialization-json:%s" % SERIALIZATION_JSON),
    ("kotlinx.coroutines.", "org.jetbrains.kotlinx:kotlinx-coroutines-android", "org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1"),
]

BASE_RULES = """-dontwarn **
-keepattributes *Annotation*,Signature,InnerClasses,EnclosingMethod,Exceptions,SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
-keep class * extends android.app.Application { <init>(); }
-keep class kotlin.Metadata { *; }
"""

IMPORT = re.compile(r"^import\s+([A-Za-z0-9_.]+)", re.M)


def scan(root):
    used = set()
    mentions = False
    for dp, _, names in os.walk(os.path.join(root, "src")):
        for n in names:
            if not (n.endswith(".kt") or n.endswith(".java")):
                continue
            with open(os.path.join(dp, n), errors="replace") as f:
                text = f.read(SCAN_MAX)
            used.update(IMPORT.findall(text))
            if NEEDLE in text:
                mentions = True
    return used, mentions


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def generate(root, facts_path=None):
    with open(os.path.join(root, "project.json")) as f:
        spec = json.load(f)
    used, mentions = scan(root)
    compose = any(u.startswith("androidx.compose") for u in used)
    libs = [l for l in spec.get("libs", []) if isinstance(l, str)]
    forced = os.environ.get("AOT_FORCE_SERIAL") == "1"
    reasons = []
    if mentions or any(u.startswith(NEEDLE) for u in used):
        reasons.append("source mentions " + NEEDLE)
    if any("kotlinx-serialization" in l for l in libs):
        reasons.append("project.json libs has kotlinx-serialization")
    if forced:
        reasons.append("forced on after an unresolved serializer error")
    serial = bool(reasons)
    have = {":".join(l.split(":")[:2]) for l in libs}
    for prefix, key, coord in IMPLICIT:
        if (any(u.startswith(prefix) for u in used) or (serial and key.endswith("kotlinx-serialization-json"))) and key not in have:
            libs.append(coord)
            have.add(key)
    app_id = spec["applicationId"]
    namespace = spec.get("namespace") or app_id
    min_sdk = int(spec.get("minSdk", 24))
    target = min(int(spec.get("targetSdk", COMPILE_SDK)), COMPILE_SDK)
    suffix = bool(spec.get("debugSuffix", True))
    deps = []
    if compose:
        deps.append('    implementation(platform("androidx.compose:compose-bom:%s"))' % COMPOSE_BOM)
        for c in ("androidx.compose.ui:ui", "androidx.compose.ui:ui-graphics", "androidx.compose.foundation:foundation", "androidx.compose.material3:material3", "androidx.compose.material:material-icons-core"):
            deps.append('    implementation("%s")' % c)
    for l in libs:
        if l.startswith("androidx.compose.") and compose and l.count(":") < 2:
            continue
        deps.append('    implementation("%s")' % esc(l))
    for a in spec.get("aars", []):
        if isinstance(a, str) and re.fullmatch(r"libs/[0-9a-f]{64}\.aar", a):
            deps.append('    implementation(files("%s"))' % a)
    extra_repos = ""
    for r in spec.get("repos", []):
        if not isinstance(r, dict):
            continue
        u = r.get("url")
        gs = [g for g in r.get("groups", []) if isinstance(g, str) and re.fullmatch(r"[A-Za-z0-9_.\-]+", g)]
        if isinstance(u, str) and re.fullmatch(r"https://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+", u) and gs:
            extra_repos += '        maven {\n            url = uri("%s")\n            content {\n%s            }\n        }\n' % (u, "".join('                includeGroup("%s")\n' % g for g in gs))
    plugin_lines = '    id("com.android.application") version "%s"\n    id("org.jetbrains.kotlin.android") version "%s"\n' % (AGP, KOTLIN)
    if compose:
        plugin_lines += '    id("org.jetbrains.kotlin.plugin.compose") version "%s"\n' % KOTLIN
    if serial:
        plugin_lines += '    id("org.jetbrains.kotlin.plugin.serialization") version "%s"\n' % KOTLIN
    plugins = "plugins {\n" + plugin_lines + "}\n"
    body = '''
android {
    namespace = "%s"
    compileSdk = %d
    defaultConfig {
        applicationId = "%s"
        minSdk = %d
        targetSdk = %d
        versionCode = %d
        versionName = "%s"
    }
    buildTypes {
        debug {
%s        }
        release {
            isMinifyEnabled = true
            isShrinkResources = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "aot-rules.pro", "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
        buildConfig = true
%s    }
    packaging {
        resources {
            excludes += setOf("/META-INF/AL2.0", "/META-INF/LGPL2.1", "/META-INF/DEPENDENCIES", "/META-INF/LICENSE*", "/META-INF/NOTICE*", "/META-INF/INDEX.LIST")
        }
    }
    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
%s
}
''' % (
        esc(namespace),
        COMPILE_SDK,
        esc(app_id),
        min_sdk,
        target,
        int(spec.get("versionCode", 1)),
        esc(str(spec.get("versionName", "1.0"))),
        '            applicationIdSuffix = ".debug"\n' if suffix else "",
        "        compose = true\n" if compose else "",
        "\n".join(deps),
    )
    with open(os.path.join(root, "build.gradle.kts"), "w") as f:
        f.write(plugins + body)
    with open(os.path.join(root, "settings.gradle.kts"), "w") as f:
        f.write('pluginManagement {\n    repositories {\n        google()\n        mavenCentral()\n        gradlePluginPortal()\n    }\n}\ndependencyResolutionManagement {\n    repositories {\n        google()\n        mavenCentral()\n' + extra_repos + '    }\n}\nrootProject.name = "app"\n')
    with open(os.path.join(root, "gradle.properties"), "w") as f:
        f.write("org.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8\nkotlin.daemon.jvmargs=-Xmx3g\nandroid.useAndroidX=true\nandroid.nonTransitiveRClass=false\nandroid.suppressUnsupportedCompileSdk=36\n")
    with open(os.path.join(root, "aot-rules.pro"), "w") as f:
        f.write(BASE_RULES)
    own = os.path.join(root, "proguard-rules.pro")
    if not os.path.exists(own):
        open(own, "w").close()
    if facts_path:
        facts = [
            "stack: Kotlin %s, AGP %s, Compose %s, serialization plugin %s, Gradle 8.14.3, built in the cloud" % (
                KOTLIN, AGP, ("plugin " + KOTLIN + ", BOM " + COMPOSE_BOM) if compose else "off", ("on (json " + SERIALIZATION_JSON + ")") if serial else "off"),
            "aot: cloud files v%s, generator pass %s" % (os.environ.get("AOT_CLOUD_VERSION", "?"), PASS),
            "serialization plugin " + ("on: " + "; ".join(reasons) if serial else "off: no kotlinx.serialization in sources or libs"),
            "sdk: min %d, target %d, compile %d" % (min_sdk, target, COMPILE_SDK),
            "libs (%d): %s" % (len(libs), ", ".join(libs[:40])),
        ]
        with open(facts_path, "a") as f:
            f.write("\n".join(facts) + "\n")


if __name__ == "__main__":
    generate(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
