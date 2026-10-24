#!/usr/bin/env python3
# Report what the upstream forks have that k2-pro-openhost does not
#
# Read-only on the branches: it fetches, compares and tries the merge in
# memory (git merge-tree). It never merges, commits or pushes. The process
# around it is described in docs/UPSTREAM_SYNC_EN.md.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import argparse
import datetime
import json
import os
import subprocess
import sys

CONFIG = ".github/upstream-sync.json"

# Host code where a change can move the timing of steps, homing or the
# MCU link, plus the shared MCU sources (also built for the host MCU on the
# CM5). The K2 MCUs run Creality firmware, not this tree's src/, so a host
# change that relies on new MCU behaviour can pass CI and still fail on the
# printer: these always need a check on the hardware.
RED_PATHS = (
    "klippy/chelper/",
    "klippy/clocksync.py",
    "klippy/kinematics/",
    "klippy/mcu.py",
    "klippy/msgproto.py",
    "klippy/reactor.py",
    "klippy/serialhdl.py",
    "klippy/stepper.py",
    "klippy/toolhead.py",
    "klippy/extras/homing.py",
    "klippy/extras/motion_queuing.py",
    "src/",
)
# Docs, CI, example configs and MCU boards that no K2-OpenHost machine
# runs: they cannot change the printer
OTHER_PATHS = (
    "docs/",
    ".github/",
    "README",
    "LICENSE",
    "COPYING",
    "lib/",
    "config/example",
    "config/generic-",
    "config/kit-",
    "config/printer-",
    "config/sample-",
    "src/ar100/",
    "src/atsam/",
    "src/atsamd/",
    "src/avr/",
    "src/hc32f460/",
    "src/indx/",
    "src/lpc176x/",
    "src/pru/",
    "src/rp2040/",
    "src/simulator/",
    "src/stm32/",
)
# The K2 modules (Jacob10383 "Add K2 extras") and the K2 profile
K2_PATHS = (
    "config/k2/",
    "klippy/extras/belt_mdl.py",
    "klippy/extras/box",
    "klippy/extras/chamber_heater_circulation_fan.py",
    "klippy/extras/extended_zone_transform.py",
    "klippy/extras/external_rfid_reader.py",
    "klippy/extras/fan_feedback.py",
    "klippy/extras/force_stop_homing.py",
    "klippy/extras/led_idle_manager.py",
    "klippy/extras/motion_limits.py",
    "klippy/extras/motor_control.py",
    "klippy/extras/motor_map.json",
    "klippy/extras/power_loss_recovery.py",
    "klippy/extras/prtouch.py",
    "klippy/extras/ptc_power_limiter.py",
    "klippy/extras/serial_485.py",
    "klippy/extras/temperature_fan_manual_floor.py",
    "klippy/extras/z_align.py",
)
CATEGORIES = ("red", "yellow", "green", "other")
LABELS = {
    "red": "🔴 motion / timing / MCU",
    "yellow": "🟡 touches files we changed",
    "green": "🟢 files we never changed",
    "other": "⚪ docs, CI, examples, MCU boards we don't run",
}
DECISIONS = ("taken", "ported", "skipped")
# GitHub refuses issue bodies over 65536 characters
BODY_LIMIT = 60000


class GitError(Exception):
    pass


def git(repo, *args, ok=(0,)):
    proc = subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode not in ok:
        raise GitError("git %s: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.returncode, proc.stdout


def rev_parse(repo, ref):
    return git(repo, "rev-parse", "--verify", ref + "^{commit}")[1].strip()


def load_config(repo, path=CONFIG):
    with open(os.path.join(repo, path), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("decisions", {})
    return cfg


def save_config(repo, cfg, path=CONFIG):
    cfg["decisions"] = dict(sorted(cfg["decisions"].items()))
    with open(os.path.join(repo, path), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def upstream_refs(cfg, overrides):
    refs = {}
    for up in cfg["upstreams"]:
        name = up["name"]
        refs[name] = overrides.get(name, "%s/%s" % (name, up["branch"]))
    return refs


def fetch(repo, cfg):
    for up in cfg["upstreams"]:
        name, branch = up["name"], up["branch"]
        url = "https://github.com/%s.git" % (up["repo"],)
        rc, _ = git(repo, "remote", "get-url", name, ok=(0, 2))
        if rc:
            git(repo, "remote", "add", name, url)
        git(
            repo,
            "fetch",
            "--quiet",
            "--no-tags",
            name,
            "+refs/heads/%s:refs/remotes/%s/%s" % (branch, name, branch),
        )


def resolve_target(repo, target):
    for ref in ("origin/" + target, target):
        try:
            rev_parse(repo, ref)
            return ref
        except GitError:
            pass
    raise GitError("target branch %s not found" % (target,))


def list_commits(repo, target, ref):
    fmt = "%x00%H%x1f%cs%x1f%an%x1f%s"
    _, out = git(
        repo,
        "log",
        "--no-merges",
        "--name-only",
        "--format=" + fmt,
        "%s..%s" % (target, ref),
    )
    commits = []
    for chunk in out.split("\0")[1:]:
        lines = chunk.strip("\n").split("\n")
        sha, date, author, subject = lines[0].split("\x1f", 3)
        files = [line for line in lines[1:] if line]
        commits.append(
            {
                "sha": sha,
                "date": date,
                "author": author,
                "subject": subject,
                "files": files,
            }
        )
    return commits


def equivalent(repo, target, ref):
    # Commits whose patch is already on the target under another hash
    _, out = git(repo, "cherry", target, ref)
    return {line[2:] for line in out.splitlines() if line.startswith("- ")}


def our_files(repo, target, ref):
    base = git(repo, "merge-base", target, ref)[1].strip()
    _, out = git(repo, "diff", "--name-only", base, target)
    return set(out.split())


def ancestors(repo, ref):
    return set(git(repo, "rev-list", ref)[1].split())


def trial_merge(repo, target, ref):
    rc, out = git(
        repo,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        target,
        ref,
        ok=(0, 1),
    )
    conflicts = sorted(set(out.splitlines()[1:])) if rc else []
    return {"clean": rc == 0, "conflicts": conflicts}


def _other(path):
    return path.startswith(OTHER_PATHS) or path.endswith(".md")


def classify(files, ours):
    relevant = [f for f in files if not _other(f)]
    if not relevant:
        category = "other"
    elif any(f.startswith(RED_PATHS) for f in relevant):
        category = "red"
    elif any(f in ours for f in relevant):
        category = "yellow"
    else:
        category = "green"
    flags = []
    if any(f.startswith(K2_PATHS) for f in files):
        flags.append("K2")
    if any(f.startswith("src/") for f in relevant):
        flags.append("MCU")
    if category == "red" and any(f in ours for f in relevant):
        flags.append("ours")
    return category, flags


def build_report(repo, cfg, target, refs, merge=True):
    decisions = cfg["decisions"]
    report = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        "target": target,
        "target_sha": rev_parse(repo, target),
        "upstreams": [],
    }
    for up in cfg["upstreams"]:
        name = up["name"]
        ref = refs[name]
        ours = our_files(repo, target, ref)
        present = equivalent(repo, target, ref)
        via = up.get("via")
        via_commits = ancestors(repo, refs[via]) if via else set()
        counts = dict.fromkeys(CATEGORIES + ("present", "via", "decided"), 0)
        pending = []
        for commit in list_commits(repo, target, ref):
            sha = commit["sha"]
            if sha in present:
                counts["present"] += 1
            elif sha in via_commits:
                counts["via"] += 1
            elif sha in decisions:
                counts["decided"] += 1
            else:
                category, flags = classify(commit["files"], ours)
                counts[category] += 1
                commit.update(category=category, flags=flags)
                pending.append(commit)
        _, behind = git(repo, "rev-list", "--count", "%s..%s" % (target, ref))
        entry = {
            "name": name,
            "repo": up["repo"],
            "branch": up["branch"],
            "ref": ref,
            "sha": rev_parse(repo, ref),
            "via": via,
            "behind": int(behind),
            "counts": counts,
            "pending": pending,
        }
        if merge:
            entry["merge"] = trial_merge(repo, target, ref)
        report["upstreams"].append(entry)
    return report


def _cell(text):
    return text.replace("|", "\\|").replace("\n", " ")


def _commit_rows(up, commits):
    rows = [
        "| Commit | Date | Subject | Files | Flags |",
        "|---|---|---|---|---|",
    ]
    for c in commits:
        rows.append(
            "| [`%s`](https://github.com/%s/commit/%s) | %s | %s | %d | %s |"
            % (
                c["sha"][:8],
                up["repo"],
                c["sha"],
                c["date"],
                _cell(c["subject"]),
                len(c["files"]),
                " ".join(c["flags"]),
            )
        )
    return rows


def render_markdown(report, limit=BODY_LIMIT):
    out = [
        "# Upstream sync report",
        "",
        "Generated %s for `%s` at `%s` by `scripts/upstream_sync.py`. "
        "It only reports: nothing is merged. Process and decisions: "
        "`docs/UPSTREAM_SYNC_EN.md`."
        % (report["generated"], report["target"], report["target_sha"][:8]),
        "",
        "| Upstream | Behind | Pending | 🔴 | 🟡 | 🟢 | ⚪ | Already here "
        "| Via other upstream | Decided | Trial merge |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for up in report["upstreams"]:
        c = up["counts"]
        merge = up.get("merge")
        if merge is None:
            status = "not run"
        elif merge["clean"]:
            status = "clean"
        else:
            status = "%d conflicts" % (len(merge["conflicts"]),)
        out.append(
            "| %s (`%s`) | %d | %d | %d | %d | %d | %d | %d | %d | %d | %s |"
            % (
                up["repo"],
                up["branch"],
                up["behind"],
                len(up["pending"]),
                c["red"],
                c["yellow"],
                c["green"],
                c["other"],
                c["present"],
                c["via"],
                c["decided"],
                status,
            )
        )
    out += [
        "",
        "Behind counts merges too. Flags: **K2** = K2 modules or profile, "
        "**MCU** = shared MCU sources (the K2 MCUs run Creality firmware; "
        "the CM5 host MCU builds them), "
        "**ours** = a red commit on files we changed.",
    ]
    tail = []
    for up in report["upstreams"]:
        sect = [
            "",
            "## %s `%s` at `%s`" % (up["repo"], up["branch"], up["sha"][:8]),
            "",
        ]
        if up["via"]:
            sect.append(
                "Commits already in `%s` are left out: they arrive with it."
                % (up["via"],)
            )
            sect.append("")
        merge = up.get("merge")
        if merge and not merge["clean"]:
            sect.append(
                "<details><summary>Trial merge: %d files in conflict"
                "</summary>\n" % (len(merge["conflicts"]),)
            )
            sect += ["- `%s`" % (f,) for f in merge["conflicts"]]
            sect += ["", "</details>", ""]
        for cat in CATEGORIES:
            commits = [p for p in up["pending"] if p["category"] == cat]
            if not commits:
                continue
            opened = cat == "red"
            sect.append(
                "<details%s><summary>%s: %d</summary>\n"
                % (" open" if opened else "", LABELS[cat], len(commits))
            )
            sect += _commit_rows(up, commits)
            sect += ["", "</details>", ""]
        tail.append(sect)
    body = "\n".join(out + [line for sect in tail for line in sect])
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + (
            "\n\n_Truncated: the full report is in the workflow artifact._"
        )
    return body + "\n"


def github_output(report):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for up in report["upstreams"]:
            merge = up.get("merge") or {}
            f.write("%s_sha=%s\n" % (up["name"], up["sha"]))
            f.write("%s_pending=%d\n" % (up["name"], len(up["pending"])))
            clean = "true" if merge.get("clean") else "false"
            f.write("%s_clean=%s\n" % (up["name"], clean))


def cmd_report(args):
    cfg = load_config(args.repo)
    overrides = dict(item.split("=", 1) for item in args.ref)
    if args.fetch:
        fetch(args.repo, cfg)
    target = args.target or resolve_target(args.repo, cfg["target"])
    refs = upstream_refs(cfg, overrides)
    report = build_report(args.repo, cfg, target, refs, merge=not args.no_merge)
    body = render_markdown(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(body)
    else:
        # the report has emoji: do not depend on the console code page
        sys.stdout.buffer.write(body.encode("utf-8"))
    if args.github_output:
        github_output(report)


def cmd_fetch(args):
    fetch(args.repo, load_config(args.repo))


def cmd_decide(args):
    cfg = load_config(args.repo)
    today = datetime.date.today().isoformat()
    for sha in args.commits:
        full = rev_parse(args.repo, sha)
        entry = {"decision": args.decision, "note": args.note, "date": today}
        if args.pr:
            entry["pr"] = args.pr
        cfg["decisions"][full] = entry
        print("%s %s: %s" % (full[:8], args.decision, args.note))
    save_config(args.repo, cfg)


def main():
    parser = argparse.ArgumentParser(
        description="Report upstream commits missing from k2-pro-openhost"
    )
    parser.add_argument("--repo", default=".", help="repository path")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="report pending upstream commits")
    rep.add_argument("--fetch", action="store_true", help="fetch upstreams")
    rep.add_argument("--target", help="branch to compare (default: config)")
    rep.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="NAME=REF",
        help="use REF for upstream NAME instead of NAME/branch",
    )
    rep.add_argument("--no-merge", action="store_true", help="skip merge")
    rep.add_argument("--markdown", help="write the report to this file")
    rep.add_argument("--json", help="write the raw report as JSON")
    rep.add_argument(
        "--github-output",
        action="store_true",
        help="write per-upstream results to $GITHUB_OUTPUT",
    )
    rep.set_defaults(func=cmd_report)
    fet = sub.add_parser("fetch", help="add and fetch the upstream remotes")
    fet.set_defaults(func=cmd_fetch)
    dec = sub.add_parser("decide", help="record a decision on commits")
    dec.add_argument("commits", nargs="+", help="upstream commits")
    dec.add_argument("--decision", required=True, choices=DECISIONS)
    dec.add_argument("--note", required=True, help="why")
    dec.add_argument("--pr", type=int, help="pull request that did it")
    dec.set_defaults(func=cmd_decide)
    args = parser.parse_args()
    try:
        args.func(args)
    except GitError as e:
        sys.exit("upstream_sync: %s" % (e,))


if __name__ == "__main__":
    main()
