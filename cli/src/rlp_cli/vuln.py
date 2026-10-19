"""The vulnerability remediation loop (#49): `rlp loop vuln`.

It fixes what the golden pipeline's `trivy` gate found by upgrading: a
vulnerable dependency to a release that fixes it, or a container image to a
newer base image. It never adds a VEX exception; `.rlp/vex.yaml` is not among
the paths the agent may change, so an attempt that edits it is thrown away.

Discovery reads the Trivy JSON reports the trivy stage wrote (listed in
`.rlp/pipeline/results.json`, else every `.rlp/pipeline/reports/trivy-*.json`),
so run the gates first (the loop workflow does):
`rlp pipeline run -s setup -s test -s package -s trivy`. Each finding carries
its package, installed version, fixed version, the scan target and the
manifest to change: the lockfile or requirements file Trivy found it in, or
the component's Dockerfile for a package that came with the base image.
Findings an unexpired `.rlp/vex.yaml` entry covers are left out, as the gate
leaves them out.

The verify stages are `setup`, `test`, `package` and `trivy`. The golden
pipeline has no separate `build` stage; the package verb builds the artifact.

A major-version bump is a human's call. Before the first agent call, a
finding whose every fixed version is a new major version of a direct
dependency escalates the run. After the verify stages pass, a change that
moved a direct dependency or a base image tag to a new major version
escalates the run instead of ending it `fixed`, so no PR opens for it.

The agent may change only dependency manifests and lockfiles (requirements
files, pyproject.toml, uv.lock, package.json and so on) and Dockerfiles: the
tracked ones at each component's root, its declared Dockerfile, and any
manifest the filesystem scan named, outside hidden and protected directories.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from rlp_cli import loop, pipeline, trivy, vex
from rlp_cli.contract import component_dir

NAME = "vuln"
VERIFY_STAGES = ("setup", "test", "package", "trivy")
PROTECTED_DIRS = frozenset({"policy", "infra", "reports"})
# Dependency manifests and lockfiles the agent may edit, by file name.
MANIFESTS = frozenset({
    "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "pdm.lock",
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock",
    "pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile", "composer.json", "composer.lock",
})
IMAGE_PREFIX = "trivy-image-"
# Exceptions and Trivy settings: an attempt that touches one is put back and thrown away, even where git ignores it.
GUARDED = (".rlp/vex.yaml", ".trivyignore", ".trivyignore.yaml", "trivy.yaml", "trivy.yml")


# --- file kinds ---


def is_requirements(name: str) -> bool:
    return name.endswith(".txt") and (name.startswith("requirements") or name.startswith("constraints"))


def is_dockerfile(name: str) -> bool:
    return name in ("Dockerfile", "Containerfile") or name.startswith("Dockerfile.") or name.endswith(".Dockerfile")


def is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in MANIFESTS or is_requirements(name) or is_dockerfile(name)


def _hidden_or_protected(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(p.startswith(".") for p in parts[:-1]) or (len(parts) > 1 and parts[0] in PROTECTED_DIRS)


def _tracked(root: Path) -> list[str]:
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise loop.LoopError(f"{root} is not in a git work tree; loops work on a branch")
    return [p for p in proc.stdout.split("\0") if p]


def _rel(root: Path, component: dict) -> str:
    return component_dir(root, component).resolve().relative_to(root.resolve()).as_posix()


def _join(base: str, name: str) -> str:
    return name if base in ("", ".") else f"{base}/{name}"


def dockerfile(root: Path, component: dict) -> str:
    """The component's Dockerfile, relative to the repo root."""
    return _join(_rel(root, component), (component.get("artifact") or {}).get("dockerfile", "Dockerfile"))


# --- allowed paths ---


def allowed_paths(root: Path, doc: dict) -> list[str]:
    """Manifests and Dockerfiles: at each component's root, declared, or named by the last filesystem scan."""
    tracked = _tracked(root)
    known = set(tracked)
    allowed: list[str] = []
    for c in doc.get("components") or []:
        rel = _rel(root, c)
        prefix = "" if rel == "." else rel + "/"
        allowed += [p for p in tracked if p.startswith(prefix) and "/" not in p[len(prefix):] and is_manifest(p)]
        if (c.get("artifact") or {}).get("kind") == "container-image" and dockerfile(root, c) in known:
            allowed.append(dockerfile(root, c))
    for target in _fs_targets(root):
        if target in known and is_manifest(target) and not _hidden_or_protected(target):
            allowed.append(target)
    allowed = [p for p in dict.fromkeys(allowed) if not _hidden_or_protected(p)]
    if not allowed:
        raise loop.LoopError("no tracked dependency manifests or Dockerfiles the vuln loop may change (for example requirements.txt, uv.lock or Dockerfile)")
    return sorted(allowed)


# --- reading manifests ---


def normalise(name: str) -> str:
    """PEP 503 normalisation, which also suits npm and Go names well enough."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:(==|===|~=|>=|<=|!=|>|<)\s*([^\s;,#]+))?")


def _requirement(text: str) -> tuple[str, str] | None:
    """(name, pinned version or "") for one requirement string, or None."""
    m = REQ_LINE.match(text)
    if not m:
        return None
    return normalise(m.group(1)), (m.group(3) or "") if m.group(2) in ("==", "===", "~=", ">=") else ""


def pins(path: str, text: str) -> dict[str, str]:
    """name -> declared version in one manifest; versions are "" when not pinned. Unknown formats give {}."""
    name = PurePosixPath(path).name
    out: dict[str, str] = {}
    if is_requirements(name):
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            req = _requirement(line)
            if req:
                out[req[0]] = req[1]
    elif name == "pyproject.toml":
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}
        project = doc.get("project") or {}
        groups = [project.get("dependencies") or [], *(project.get("optional-dependencies") or {}).values(), *(doc.get("dependency-groups") or {}).values()]
        for group in groups:
            for item in group:
                req = _requirement(item) if isinstance(item, str) else None
                if req:
                    out[req[0]] = req[1]
    elif name == "uv.lock":
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}
        for pkg in doc.get("package") or []:
            out[normalise(str(pkg.get("name", "")))] = str(pkg.get("version", ""))
    elif name == "package.json":
        try:
            doc = json.loads(text)
        except ValueError:
            return {}
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            for dep, spec in (doc.get(key) or {}).items():
                out[normalise(dep)] = str(spec).lstrip("^~=v ")
    elif is_dockerfile(name):
        out.update(base_images(text))
    return out


FROM_LINE = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.IGNORECASE | re.MULTILINE)


def base_images(text: str) -> dict[str, str]:
    """image -> tag for each FROM line with a tag (digests dropped)."""
    out = {}
    for ref in FROM_LINE.findall(text):
        ref = ref.split("@", 1)[0]
        image, sep, tag = ref.rpartition(":")
        if sep and "/" not in tag and image:
            out[image] = tag
    return out


def direct_dependencies(root: Path, files: Iterable[str]) -> dict[str, str]:
    """Normalised name -> the manifest that declares it, over the given manifests (lockfiles don't declare)."""
    out: dict[str, str] = {}
    for rel in files:
        name = PurePosixPath(rel).name
        if not (is_requirements(name) or name in ("pyproject.toml", "package.json")):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        for dep in pins(rel, text):
            out.setdefault(dep, rel)
    return out


# --- versions ---


def major(version: str) -> int | None:
    """The leading number of a version or image tag (v1.2.3, 2:1.0-1, 3.13-slim), or None."""
    v = version.strip().lstrip("vV")
    v = v.split(":", 1)[1] if re.match(r"^\d+:", v) else v
    m = re.match(r"^(\d+)", v)
    return int(m.group(1)) if m else None


def needs_major(installed: str, fixed: str) -> bool:
    """Whether every fixed version Trivy lists is on a different major version than the installed one."""
    have = major(installed)
    wanted = [major(v) for v in fixed.split(",") if v.strip()]
    if have is None or not wanted or any(w is None for w in wanted):
        return False
    return have not in wanted


# --- discovery ---


def _reports(root: Path) -> list[tuple[str, Path]]:
    """(scan name, path) for each Trivy JSON report the last trivy stage wrote."""
    r = pipeline.load_results(root).get("trivy") or {}
    scans = (r.get("data") or {}).get("scans") or []
    paths = [root / s["json"] for s in scans if s.get("json")] or sorted((root / pipeline.REPORTS).glob("trivy-*.json"))
    return [(p.stem, p) for p in paths if p.is_file()]


def _fs_targets(root: Path) -> list[str]:
    for name, path in _reports(root):
        if name == "trivy-fs":
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
            return [r.get("Target", "") for r in report.get("Results") or [] if r.get("Target")]
    return []


def findings_from_report(root: Path, doc: dict, name: str, report: dict, exceptions: list[vex.Entry], direct: dict[str, str]) -> list[dict]:
    components = {c["name"]: c for c in doc.get("components") or []}
    image = name.startswith(IMAGE_PREFIX)
    component = components.get(name[len(IMAGE_PREFIX):]) if image else None
    out = []
    for result in report.get("Results") or []:
        klass = result.get("Class", "")
        for v in result.get("Vulnerabilities") or []:
            severity, fixed = v.get("Severity", ""), v.get("FixedVersion", "")
            vid, package, installed = v.get("VulnerabilityID", ""), v.get("PkgName", ""), v.get("InstalledVersion", "")
            if severity not in trivy.SEVERITIES or not fixed:
                continue
            if any(e.covers(vid, package) for e in exceptions):
                continue
            declared = direct.get(normalise(package), "")
            if not image:
                manifest, source = result.get("Target", ""), "dependency"
            elif klass == "lang-pkgs" and declared:
                manifest, source = declared, "dependency"
            else:
                manifest = dockerfile(root, component) if component else result.get("Target", "")
                source = "base-image"
            what = "a package in the base image" if source == "base-image" else ("a direct dependency" if declared else "a dependency")
            out.append({
                "id": f"vuln-{vid}-{package}-{manifest}",
                "stage": "trivy",
                "path": manifest,
                "rule": vid,
                "severity": severity,
                "package": package,
                "installed": installed,
                "fixed": fixed,
                "target": f"image {component['name']}" if image and component else ("image" if image else "fs"),
                "source": source,
                "direct": bool(declared) and source == "dependency",
                "major": needs_major(installed, fixed),
                "message": f"{package} {installed} has {vid} ({severity}), fixed in {fixed}; {what}" + (f": {v['Title']}" if v.get("Title") else ""),
            })
    return out


def discover(root: Path, doc: dict) -> list[dict]:
    """Fixable HIGH and CRITICAL findings from the last trivy run, without the ones .rlp/vex.yaml covers."""
    r = pipeline.load_results(root).get("trivy")
    if not r or r.get("status") != "failed":
        return []
    exceptions = vex.active(vex.load(root))
    tracked = _tracked(root)
    direct = direct_dependencies(root, [p for p in tracked if is_manifest(p) and not _hidden_or_protected(p)])
    findings: list[dict] = []
    seen = set()
    for name, path in _reports(root):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for f in findings_from_report(root, doc, name, report, exceptions, direct):
            if f["id"] not in seen:
                seen.add(f["id"])
                findings.append(f)
    if not findings:
        findings.append({"id": "trivy", "stage": "trivy", "message": f"the trivy stage failed: {r.get('detail', '')}"})
    return findings


# --- major bumps ---


def preflight(root: Path, doc: dict, findings: list[dict]) -> str:
    """Escalate before any agent call when a direct dependency can only be fixed by a new major version."""
    majors = [f for f in findings if f.get("direct") and f.get("major")]
    if not majors:
        return ""
    shown = "; ".join(f"{f['package']} {f['installed']} -> {f['fixed']} ({f['rule']}, {f['path']})" for f in majors)
    return f"{len(majors)} finding(s) can only be fixed by a major-version bump of a direct dependency, which a human decides: {shown}"


def _show(root: Path, ref: str, path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{path}"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return proc.stdout if proc.returncode == 0 else None


def major_bumps(root: Path, base: str, head: str = "HEAD") -> list[str]:
    """Direct dependencies and base image tags whose major version changed between base and head."""
    changed = loop.git(root, "diff", "--name-only", f"{base}...{head}").splitlines()
    head_files = [p for p in changed if is_manifest(p)]
    direct = set(direct_dependencies(root, [p for p in _tracked(root) if is_manifest(p)]))
    bumps = []
    for path in head_files:
        old_text, new_text = _show(root, base, path), _show(root, head, path)
        if old_text is None or new_text is None:
            continue
        old, new = pins(path, old_text), pins(path, new_text)
        lockfile = PurePosixPath(path).name in MANIFESTS - {"pyproject.toml", "package.json"}
        for name, before in old.items():
            after = new.get(name)
            if not after or not before or after == before:
                continue
            if lockfile and name not in direct:
                continue  # a transitive package: only a direct dependency's major bump is a human's call
            was, now_ = major(before), major(after)
            if was is not None and now_ is not None and was != now_:
                what = "base image" if is_dockerfile(PurePosixPath(path).name) else "dependency"
                bumps.append(f"{what} {name} {before} -> {after} in {path}")
    return bumps


def review(root: Path, base: str) -> str:
    """Escalate a green run whose change includes a major-version bump."""
    bumps = major_bumps(root, base)
    if not bumps:
        return ""
    return f"the verify stages passed, but the fix is a major-version bump, which a human decides: {'; '.join(bumps)}"


# --- the prompt ---


def _fence(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text.rstrip()}\n{fence}"


def build_prompt(p: loop.PromptInput) -> str:
    lines = [
        f"You are fixing vulnerabilities that rlp's trivy gate found in this repository's dependencies and container images (attempt {p.iteration} of {p.max_iterations}).",
        "",
        "Rules:",
        f"- Change only these files: {', '.join(p.allowed_paths)}. Changes anywhere else are thrown away.",
        "- Upgrade each vulnerable package to a release that fixes it, within its current major version. "
        "For a package that came with a container's base image, move the Dockerfile's FROM line to a newer tag of the same image and major version.",
        "- Never add an exception: .rlp/vex.yaml, .trivyignore and Trivy settings are off limits. Never weaken a gate.",
        "- Change only the pins the findings need. When a lockfile pins the same package, update its entry to match.",
        "- You can't run commands. When you finish, the loop reruns setup, test, package and trivy itself.",
        "- When you are done, say in one short paragraph what you changed.",
    ]
    by_manifest: dict[str, list[dict]] = {}
    for f in p.findings:
        by_manifest.setdefault(f.get("path", "") or "(no manifest)", []).append(f)
    for manifest, group in by_manifest.items():
        base = any(f.get("source") == "base-image" for f in group)
        lines += ["", f"## {manifest}" + (" (base image)" if base else ""), ""]
        for f in group:
            if f.get("package"):
                lines.append(f"- {f['package']} {f['installed']}: {f['rule']} ({f['severity']}), fixed in {f['fixed']} [{f['target']}]")
            else:
                lines.append(f"- {f.get('message', '')}")
    if p.rejected:
        lines += ["", f"The previous attempt was thrown away: {p.rejected}"]
    failed = [g for g in p.gates if g.get("status") == "failed"]
    if failed:
        lines += ["", "The gates still fail after the previous attempt:"]
        for g in failed:
            lines += [f"- {g['stage']}:", "", *("  " + x for x in _fence(g.get("detail", "")).splitlines())]
    return "\n".join(lines) + "\n"


KIND = loop.register(loop.LoopKind(
    name=NAME,
    description="Fix Trivy findings by upgrading dependencies and base images, and verify with setup, test, package and trivy.",
    discover=discover,
    verify_stages=VERIFY_STAGES,
    allowed_paths=allowed_paths,
    build_prompt=build_prompt,
    preflight=preflight,
    review=review,
    guarded=GUARDED,
))
