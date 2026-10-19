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

The agent can't run commands, so after a kept attempt that changed a
dependency manifest at a component's root, the loop regenerates that
component's lockfile itself (#80) before committing: the component's `lock`
command from rlp.yaml, or `uv lock` when a uv.lock is tracked beside it. The
regenerated lockfile goes into the iteration's commit, so the major-bump
review sees it. A lock command that fails, or that changes a file outside the
allowed paths, throws the attempt away and its output goes into the next
prompt.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from rlp_cli import agent, loop, pipeline, trivy, vex
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
# The lock command a tracked lockfile implies when the component declares none.
DEFAULT_LOCKS = {"uv.lock": "uv lock"}
LOCK_TIMEOUT_SECONDS = 600
LOCK_OUTPUT_CHARS = 3000


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


# --- regenerating lockfiles ---


def lock_commands(root: Path, doc: dict) -> list[dict]:
    """{component, path, command} for each component with a lock command: declared, or implied by its lockfile."""
    tracked = set(_tracked(root))
    out = []
    for c in doc.get("components") or []:
        rel = _rel(root, c)
        command = c.get("lock") or next((cmd for name, cmd in DEFAULT_LOCKS.items() if _join(rel, name) in tracked), "")
        if command:
            out.append({"component": c["name"], "path": rel, "command": command})
    return out


def _at_root(rel: str, path: str) -> bool:
    prefix = "" if rel == "." else rel + "/"
    return path.startswith(prefix) and "/" not in path[len(prefix):]


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= LOCK_OUTPUT_CHARS else "..." + text[-LOCK_OUTPUT_CHARS:]


def run_lock(root: Path, lock: dict) -> tuple[int, str]:
    """Run one lock command in its component's directory: (exit code, combined output)."""
    argv = shlex.split(lock["command"], posix=True)
    exe = shutil.which(argv[0]) if argv else None
    if exe is None:
        return 127, f"command not found: {argv[0] if argv else lock['command']}"
    try:
        proc = subprocess.run([exe, *argv[1:]], cwd=root / lock["path"], env=dict(os.environ), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=LOCK_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {LOCK_TIMEOUT_SECONDS} seconds"
    except OSError as err:
        return 126, str(err)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def regenerate_locks(root: Path, doc: dict, changed: list[str], allowed: list[str]) -> loop.AfterAttempt:
    """After a kept attempt: run the lock command of each component whose root manifests the agent changed."""
    out = loop.AfterAttempt()
    for lock in lock_commands(root, doc):
        if not any(_at_root(lock["path"], f) and is_manifest(f) and not is_dockerfile(PurePosixPath(f).name) for f in changed):
            continue
        before = agent.snapshot(root)
        code, output = run_lock(root, lock)
        files = agent.changed(before, agent.snapshot(root))
        out.files += files
        out.steps.append({"component": lock["component"], "command": lock["command"], "exitCode": code, "files": files})
        where = f"the lock command `{lock['command']}` for component {lock['component']}"
        if code != 0:
            out.rejected = f"{where} failed (exit {code}):\n{_tail(output)}"
            return out
        stray = agent.outside(files, allowed)
        if stray:
            out.rejected = f"{where} changed files outside the allowed paths: {', '.join(stray)}"
            return out
    return out


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
    locks = lock_commands(root, doc)
    for f in findings:
        lock = next((lk for lk in locks if f.get("source") == "dependency" and _at_root(lk["path"], f.get("path", ""))), None)
        if lock:
            f["lock"] = lock["command"]
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


UPGRADE_RULE = " ".join([
    "- Upgrade each vulnerable package to a release that fixes it, within its current major version.",
    "For a package that came with a container's base image, move the Dockerfile's FROM line to a newer tag of the same image and major version.",
])


def build_prompt(p: loop.PromptInput) -> str:
    lines = [
        f"You are fixing vulnerabilities that rlp's trivy gate found in this repository's dependencies and container images (attempt {p.iteration} of {p.max_iterations}).",
        "",
        "Rules:",
        f"- Change only these files: {', '.join(p.allowed_paths)}. Changes anywhere else are thrown away.",
        UPGRADE_RULE,
        "- Never add an exception: .rlp/vex.yaml, .trivyignore and Trivy settings are off limits. Never weaken a gate.",
        "- Change only the pins the findings need. When a lockfile pins the same package, update its entry to match.",
        "- You can't run commands. When you finish, the loop reruns setup, test, package and trivy itself.",
        "- When you are done, say in one short paragraph what you changed.",
    ]
    by_manifest: dict[str, list[dict]] = {}
    for f in p.findings:
        by_manifest.setdefault(f.get("path", "") or "(no manifest)", []).append(f)
    locks = sorted({(f["path"], f["lock"]) for f in p.findings if f.get("lock")})
    if locks:
        lines += ["", "Lockfiles:", ""]
        for manifest, command in locks:
            folder = PurePosixPath(manifest).parent.as_posix()
            where = "the repo root" if folder == "." else folder
            lines.append(f"- After your change the loop runs `{command}` in {where}, which regenerates the lockfile there.")
        lines += [
            "- So fix a package there in its manifest (pyproject.toml, package.json and so on), not by editing the lockfile:",
            "  raise the declared pin to the fixed release, within its current major version (for example ==42.0.4, not >=42.0.4).",
            "  For a package the manifest doesn't declare, add a constraint the package manager reads (for uv, [tool.uv] constraint-dependencies).",
        ]
    for manifest, group in by_manifest.items():
        base = any(f.get("source") == "base-image" for f in group)
        lines += ["", f"## {manifest}" + (" (base image)" if base else ""), ""]
        for f in group:
            if f.get("package"):
                lines.append(f"- {f['package']} {f['installed']}: {f['rule']} ({f['severity']}), fixed in {f['fixed']} [{f['target']}]")
            else:
                lines.append(f"- {f.get('message', '')}")
    if p.rejected:
        first, _, rest = p.rejected.partition("\n")
        lines += ["", f"The previous attempt was thrown away: {first}"]
        if rest.strip():
            lines += ["", _fence(rest)]
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
    after_attempt=regenerate_locks,
))
