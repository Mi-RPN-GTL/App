import json
import os
import re
import sys

AGP = "8.13.0"
KOTLIN = "1.9.24"
COMPOSE_COMPILER = "1.5.14"
COMPOSE_BOM = "2024.12.01"
COMPILE_SDK = 36

IMPLICIT = [
    ("androidx.activity.compose", "androidx.activity:activity-compose", "androidx.activity:activity-compose:1.9.3"),
    ("androidx.core.", "androidx.core:core-ktx", "androidx.core:core-ktx:1.13.1"),
    ("androidx.lifecycle.viewmodel.compose", "androidx.lifecycle:lifecycle-viewmodel-compose", "androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7"),
    ("androidx.lifecycle.compose", "androidx.lifecycle:lifecycle-runtime-compose", "androidx.lifecycle:lifecycle-runtime-compose:2.8.7"),
    ("androidx.navigation.compose", "androidx.navigation:navigation-compose", "androidx.navigation:navigation-compose:2.8.4"),
    ("kotlinx.coroutines.", "org.jetbrains.kotlinx:kotlinx-coroutines-android", "org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1"),
]

BASE_RULES = """-dontwarn **
-keepattributes *Annotation*,Signature,InnerClasses,EnclosingMethod,Exceptions,SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
-keep class * extends android.app.Application { <init>(); }
-keep class kotlin.Metadata { *; }
"""

IMPORT = re.compile(r"^import\s+([A-Za-z0-9_.]+)", re.M)


def imports(root):
    used = set()
    seen = 0
    for sub in ("src/main/kotlin", "src/main/java"):
        base = os.path.join(root, sub)
        for dp, _, names in os.walk(base):
            for n in names:
                if not (n.endswith(".kt") or n.endswith(".java")):
                    continue
                seen += 1
                if seen > 800:
                    return used
                with open(os.path.join(dp, n), errors="replace") as f:
                    head = f.read(16384)
                used.update(IMPORT.findall(head))
    return used


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def generate(root):
    with open(os.path.join(root, "project.json")) as f:
        spec = json.load(f)
    used = imports(root)
    compose = any(u.startswith("androidx.compose") for u in used)
    libs = [l for l in spec.get("libs", []) if isinstance(l, str)]
    have = {":".join(l.split(":")[:2]) for l in libs}
    for prefix, key, coord in IMPLICIT:
        if any(u.startswith(prefix) for u in used) and key not in have:
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
    plugins = '''plugins {
    id("com.android.application") version "%s"
    id("org.jetbrains.kotlin.android") version "%s"
}
''' % (AGP, KOTLIN)
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
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
%s    }
%s    packaging {
        resources {
            excludes += setOf("/META-INF/AL2.0", "/META-INF/LGPL2.1", "/META-INF/DEPENDENCIES", "/META-INF/LICENSE*", "/META-INF/NOTICE*", "/META-INF/INDEX.LIST")
        }
    }
    lint {
        abortOnError = false
        checkReleaseBuilds = false
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
        '    composeOptions {\n        kotlinCompilerExtensionVersion = "%s"\n    }\n' % COMPOSE_COMPILER if compose else "",
        "\n".join(deps),
    )
    with open(os.path.join(root, "build.gradle.kts"), "w") as f:
        f.write(plugins + body)
    with open(os.path.join(root, "settings.gradle.kts"), "w") as f:
        f.write('pluginManagement {\n    repositories {\n        google()\n        mavenCentral()\n        gradlePluginPortal()\n    }\n}\ndependencyResolutionManagement {\n    repositories {\n        google()\n        mavenCentral()\n    }\n}\nrootProject.name = "app"\n')
    with open(os.path.join(root, "gradle.properties"), "w") as f:
        f.write("org.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8\nkotlin.daemon.jvmargs=-Xmx3g\nandroid.useAndroidX=true\nandroid.nonTransitiveRClass=false\nandroid.suppressUnsupportedCompileSdk=36\n")
    with open(os.path.join(root, "aot-rules.pro"), "w") as f:
        f.write(BASE_RULES)
    own = os.path.join(root, "proguard-rules.pro")
    if not os.path.exists(own):
        open(own, "w").close()


if __name__ == "__main__":
    generate(sys.argv[1])
