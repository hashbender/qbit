#!/usr/bin/env python3
"""Mirror Qbit-Org/qbit PR history into hashbender/qbit so the same GitHub
Actions fire on the fork's tenki runners.

Model: each upstream base branch is reconstructed on the fork as an *evolving*
branch that starts just before the first mirrored PR and advances as each PR
merges -- i.e. we replay upstream's PR history forward. Every base/head workflow
file has its runner labels rewritten to the tenki pools, so all CI lands on the
fork's runners. PRs are opened and then merged / closed / left-open to match the
upstream PR's state.

Authoritative per-PR data:
  * file set     -> GitHub API  pulls/N/files  (status + previous_filename)
  * file content -> frozen       refs/pull/N/head  snapshot

Idempotent: a "Mirror-of: Qbit-Org/qbit#N" marker in each mirrored PR body lets
re-runs skip already-mirrored PRs and sync state transitions. Re-running on a
schedule IS the ongoing watcher.
"""
import argparse, glob, json, os, re, subprocess, sys, time

UPSTREAM = "Qbit-Org/qbit"
FORK     = "hashbender/qbit"
ORIGIN   = "origin"          # git remote -> UPSTREAM
FORKREM  = "fork"            # git remote -> FORK
MARKER_T = "Mirror-of: {}#{{}}".format(UPSTREAM)

TENKI_LARGE  = "tenki-standard-large-plus-16c-32g"   # heavy / self-hosted pool
TENKI_MEDIUM = "tenki-standard-medium-4c-8g"         # everything github-hosted

# bases whose ci.yml/core-checks/required-merge-gate actually trigger on
# pull_request/push (their `branches:` filter). PRs to other bases fire ~no CI,
# matching upstream, so we don't wait for runs on them.
PR_CI_BASES = {"main", "0.1.x", "develop"}

REPO_DIR = None

# --- shell helpers ----------------------------------------------------------

def git(*a, check=True):
    r = subprocess.run(["git", "-C", REPO_DIR, *a], capture_output=True, text=True)
    if check and r.returncode:
        sys.exit("git {}\n{}".format(" ".join(a), r.stderr or r.stdout))
    return (r.stdout or "").strip()

def gh(*a, check=True):
    r = subprocess.run(["gh", *a], capture_output=True, text=True)
    if check and r.returncode:
        sys.exit("gh {}\n{}".format(" ".join(a), r.stderr or r.stdout))
    return (r.stdout or "").strip()

# --- runner-label rewrite ---------------------------------------------------

_EXPR   = re.compile(r"\$\{\{[^}]*github\.repository[^}]*self-hosted[^}]*\}\}")
_ARR    = re.compile(r"(runs-on:\s*)\[\s*self-hosted[^\]]*\]")
_HOSTED = re.compile(r"(runs-on:\s*)(?:ubuntu|macos|windows|blacksmith)-[\w.\-]+")

def tenkify(text):
    text = _EXPR.sub(TENKI_LARGE, text)
    text = _ARR.sub(r"\1" + TENKI_LARGE, text)
    text = _HOSTED.sub(r"\1" + TENKI_MEDIUM, text)
    return text

def tenkify_workflows():
    wf = os.path.join(REPO_DIR, ".github", "workflows")
    changed = []
    for p in glob.glob(wf + "/*.yml") + glob.glob(wf + "/*.yaml"):
        old = open(p).read()
        new = tenkify(old)
        if new != old:
            open(p, "w").write(new)
            changed.append(os.path.relpath(p, REPO_DIR))
    return changed

def assert_tenki(ctx):
    bad = []
    for line in git("grep", "-h", "-E", "runs-on:", "--", ".github/workflows/").splitlines():
        v = line.split("runs-on:", 1)[1].strip()
        v = re.sub(r"^&\w+\s*", "", v)
        if v == "" or v.startswith("*") or v.startswith("tenki-"):
            continue
        if "self-hosted" in v or "github.repository" in v or re.search(r"(ubuntu|macos|windows|blacksmith)-", v):
            bad.append(line)
    if bad:
        sys.exit("{}: non-tenki runs-on remains:\n  {}".format(ctx, "\n  ".join(bad)))

# --- upstream data ----------------------------------------------------------

def pr_files(n):
    """list of (status, path, prev) from the authoritative GitHub API."""
    out = gh("api", "repos/{}/pulls/{}/files".format(UPSTREAM, n), "--paginate",
             "-q", '.[] | [.status, .filename, (.previous_filename // "")] | @tsv')
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return rows

def first_commit_parent(n):
    """parent of the PR's oldest commit -> the branch point (the start)."""
    oid = gh("pr", "view", str(n), "--repo", UPSTREAM, "--json", "commits",
             "-q", ".commits[0].oid")
    return oid + "^"

def _ref_has(ref, path):
    return subprocess.run(["git", "-C", REPO_DIR, "cat-file", "-e", "{}:{}".format(ref, path)],
                          capture_output=True).returncode == 0

CI_YML = ".github/workflows/ci.yml"

def _pick_start(base, first_pr, all_prs):
    """Choose the base's start commit so the result is CODE-COHERENT with its
    workflows (else ci.yml fails at the first 'classify' job and nothing builds):
      * historical pre-PR point if ci.yml already exists there, or if some
        mirrored PR on this base introduces the CI infrastructure (e.g. the
        release PR on main);
      * otherwise the current upstream tip, which has coherent code + workflows
        (merged PRs already in it become empty and are skipped)."""
    hist = first_commit_parent(first_pr["number"])
    if _ref_has(hist, CI_YML):
        return hist, "historical"
    for p in all_prs:
        if p["baseRefName"] == base and any(f[1] == CI_YML for f in pr_files(p["number"])):
            return hist, "historical(infra via #{})".format(p["number"])
    tip = "{}/{}".format(ORIGIN, base)
    if _ref_has(tip, CI_YML):
        return tip, "current-tip"
    return hist, "historical(no-CI)"

# --- build steps ------------------------------------------------------------

def build_overlay(src_ref, head_ref, files, msg):
    """Check out src_ref detached, overlay the PR's files from head_ref, rewrite
    workflow labels, commit. Returns (commit_sha, n_changed, n_wf) or
    (None, 0, 0) if the result is an empty diff."""
    git("checkout", "--quiet", "--detach", src_ref)
    for status, path, prev in files:
        if status == "removed":
            git("rm", "-q", "-f", "--ignore-unmatch", "--", path, check=False)
        elif status == "renamed":
            if prev:
                git("rm", "-q", "-f", "--ignore-unmatch", "--", prev, check=False)
            git("checkout", head_ref, "--", path)
        else:  # added / modified / changed
            git("checkout", head_ref, "--", path)
    wf = tenkify_workflows()
    assert_tenki(msg)
    git("add", "-A")
    if not git("diff", "--cached", "--name-only"):
        return None, 0, 0
    n = len(git("diff", "--cached", "--name-only").splitlines())
    git("commit", "--quiet", "-m", msg)
    return git("rev-parse", "HEAD"), n, len(wf)

# --- fork PR state ----------------------------------------------------------

def existing_mirrors():
    out = gh("pr", "list", "--repo", FORK, "--state", "all", "--limit", "300",
             "--json", "number,body,state")
    pat = re.compile(re.escape(MARKER_T).replace(r"\{\}", r"(\d+)"))
    mp = {}
    for p in json.loads(out or "[]"):
        m = pat.search(p.get("body") or "")
        if not m:
            continue
        k, cur = int(m.group(1)), mp.get(int(m.group(1)))
        # with trial-run duplicates, prefer an OPEN mirror (the live one to sync),
        # otherwise the highest-numbered (newest) mirror.
        if (cur is None
                or (p["state"] == "OPEN") > (cur["state"] == "OPEN")
                or ((p["state"] == "OPEN") == (cur["state"] == "OPEN")
                    and p["number"] > cur["number"])):
            mp[k] = p
    return mp

# --- main -------------------------------------------------------------------

def main():
    global REPO_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--prs-file", help="JSON from gh pr list; else fetched live")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--reset-bases", action="store_true",
                    help="force-reset+push base branches to their pre-PR start "
                         "(ONLY the initial backfill; never the watcher)")
    ap.add_argument("--only", type=int, nargs="*", help="replay only these PR numbers")
    ap.add_argument("--ignore-existing", action="store_true",
                    help="re-mirror PRs even if a marker already exists on the fork")
    ap.add_argument("--delay", type=float, default=0)
    args = ap.parse_args()
    REPO_DIR = args.repo_dir
    ex = args.execute
    def log(m): print(m, flush=True)

    log("== fetch ==")
    git("fetch", "--quiet", ORIGIN, "+refs/heads/*:refs/remotes/origin/*",
        "+refs/pull/*/head:refs/mirror-src/pr/*")
    git("fetch", "--quiet", FORKREM, "+refs/heads/*:refs/remotes/fork/*")

    if args.prs_file:
        prs = json.load(open(args.prs_file))
    else:
        prs = json.loads(gh("pr", "list", "--repo", UPSTREAM, "--state", "all",
              "--limit", "100", "--json",
              "number,title,body,state,baseRefName,headRefName,mergedAt,createdAt,url"))
    prs.sort(key=lambda p: p["createdAt"])
    all_prs = list(prs)                          # full set (for base start selection)
    # bases are derived from the FULL set so staging a subset still resets every base
    bases = {}
    for p in prs:
        bases.setdefault(p["baseRefName"], p)   # earliest PR per base (sorted)
    if args.only:
        prs = [p for p in prs if p["number"] in args.only]

    mirrors = existing_mirrors() if ex else {}

    # 1) evolving base branches: either reset to pre-PR start (initial backfill)
    #    or continue from the current fork tip (watcher / staged continuation).
    log("\n== bases ({}) ==  [{}{}]".format(
        len(bases), "EXECUTE" if ex else "dry-run", ", RESET" if args.reset_bases else ""))
    evolving = {}   # base -> local commit sha that the next PR on it builds upon
    for b, first in bases.items():
        if args.reset_bases:
            start, mode = _pick_start(b, first, all_prs)
            git("checkout", "--quiet", "--detach", start)
            wf = tenkify_workflows()
            assert_tenki("base " + b)
            if wf:
                git("add", "-A")
                git("commit", "--quiet", "-m",
                    "ci: tenki runner labels for {} (mirror base)".format(b))
            sha = git("rev-parse", "HEAD")
            evolving[b] = sha
            log("  {:<26} start {} (+{} wf)  [{}]".format(b, sha[:10], len(wf), mode))
            if ex:
                git("push", "--force", FORKREM, "{}:refs/heads/{}".format(sha, b))
        else:
            ref = "fork/" + b
            sha = subprocess.run(["git", "-C", REPO_DIR, "rev-parse", "-q", "--verify", ref],
                                 capture_output=True, text=True).stdout.strip()
            if not sha:
                sys.exit("base {} missing on fork; run the initial backfill with "
                         "--reset-bases first".format(b))
            evolving[b] = sha
            log("  {:<26} continue {}".format(b, sha[:10]))

    # 2) replay PRs chronologically.
    log("\n== PRs ({}) ==".format(len(prs)))
    for pr in prs:
        n, b, st = pr["number"], pr["baseRefName"], pr["state"]
        head = "refs/mirror-src/pr/{}".format(n)

        if ex and n in mirrors and not args.ignore_existing:
            fp = mirrors[n]
            if fp["state"] == "OPEN" and st in ("MERGED", "CLOSED"):
                log("  #{:<3} already #{} OPEN -> sync {}".format(n, fp["number"], st))
                _drive_state(st, str(fp["number"]), b, evolving, ex, log)
            else:
                log("  #{:<3} already #{} ({}) -- skip".format(n, fp["number"], fp["state"]))
            continue

        files = pr_files(n)
        sha, nfiles, nwf = build_overlay(evolving[b], head, files, pr["title"])
        if sha is None:
            log("  #{:<3} {:<26} EMPTY after tenki rewrite -- skipped".format(n, b))
            continue
        branch = "mirror/pr-{}".format(n)
        git("branch", "-f", branch, sha)
        log("  #{:<3} {:<26} <- {:<30} {:>3} files{}  [{}]".format(
            n, b, pr["headRefName"], nfiles,
            " (+{} wf)".format(nwf) if nwf else "", st))

        if not ex:
            if st == "MERGED":          # simulate the merge advancing the base
                evolving[b] = sha
            continue

        git("push", "--force", FORKREM, "{}:refs/heads/{}".format(sha, branch))
        body = "{}\n\n---\n{}\n{}".format((pr.get("body") or "").strip(),
                                          MARKER_T.format(n), pr["url"])
        url = gh("pr", "create", "--repo", FORK, "--base", b, "--head", branch,
                 "--title", pr["title"], "--body", body)
        log("       opened {}".format(url))
        # let the pull_request CI run spawn before we merge, else merging a
        # seconds-old PR cancels/skips its run (and we lose that load).
        if st == "MERGED" and b in PR_CI_BASES:
            _wait_for_pr_run(branch, sha, log)
        _drive_state(st, url, b, evolving, ex, log)
        if args.delay:
            time.sleep(args.delay)

    log("\nDone ({}).".format("executed" if ex else "dry-run, nothing pushed"))


def _wait_for_pr_run(branch, sha, log, timeout=90):
    """Poll until a pull_request run exists for THIS commit (match by headSha so
    stale runs from a reused branch name don't give a false positive)."""
    waited = 0
    while waited < timeout:
        out = gh("run", "list", "--repo", FORK, "--branch", branch, "--event",
                 "pull_request", "--limit", "10", "--json", "headSha", check=False)
        if any(r.get("headSha", "").startswith(sha[:12]) for r in json.loads(out or "[]")):
            log("       pull_request CI spawned ({}s)".format(waited))
            return
        time.sleep(6)
        waited += 6
    log("       (no pull_request run after {}s; merging anyway)".format(timeout))


def _drive_state(state, ref, base, evolving, ex, log):
    # branches are kept (no --delete-branch) so each PR's own CI run can finish
    # rather than being cancelled when its head branch disappears.
    if state == "MERGED":
        last = ""
        for attempt in range(6):                  # mergeability can lag PR creation
            r = subprocess.run(["gh", "pr", "merge", ref, "--repo", FORK, "--squash"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                break
            last = r.stderr or r.stdout
            time.sleep(5)
        else:
            sys.exit("merge {} failed after retries:\n{}".format(ref, last))
        git("fetch", "--quiet", FORKREM, "+refs/heads/{0}:refs/remotes/fork/{0}".format(base))
        evolving[base] = git("rev-parse", "fork/" + base)   # advance to merged tip
        log("       merged (squash); base advanced")
    elif state == "CLOSED":
        gh("pr", "close", ref, "--repo", FORK)
        log("       closed")
    else:
        log("       left open")


if __name__ == "__main__":
    main()
