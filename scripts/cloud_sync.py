#!/usr/bin/env python3
"""wbai cloud-side fork sync.

Modes (env MODE):
  sync       default: tag last-known-good, health-check the diff, then merge-upstream.
  init-tags  one-off: establish last-known-good baselines on all forks without syncing.
  restore    restore one fork's branch from its last-known-good tag (env TARGET_REPO).

Auth: env SYNC_PAT. Only the Python standard library is required at runtime.
All operations are GitHub REST API calls; no clone and no push of git objects.
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.request

OWNER = "martinDLi"
TOKEN = os.environ.get("SYNC_PAT", "")
API = "https://api.github.com"
LKG = "last-known-good"

REPOS = [
    {"name": "bigpowers", "branch": "main", "upstream": "danielvm-git/bigpowers"},
    {"name": "book-to-skill", "branch": "master", "upstream": "virgiliojr94/book-to-skill"},
    {"name": "claude-token-optimizer", "branch": "main", "upstream": "nadimtuhin/claude-token-optimizer"},
    {"name": "aoci-code", "branch": "main", "upstream": "aoci-spec/aoci-code"},
    {"name": "agent-rules-books", "branch": "main", "upstream": "ciembor/agent-rules-books"},
    {"name": "andrej-karpathy-skills", "branch": "main", "upstream": "multica-ai/andrej-karpathy-skills"},
    {"name": "rea", "branch": "main", "upstream": "morluto/rea"},
    {"name": "security-audit-skill", "branch": "main", "upstream": "cloudflare/security-audit-skill"},
    {"name": "open-code-review", "branch": "main", "upstream": "alibaba/open-code-review"},
    {"name": "ZCode", "branch": "main", "upstream": "zai-org/ZCode"},
]


def api(method, path, body=None):
    if not TOKEN:
        raise RuntimeError("SYNC_PAT 未设置")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "wbai-cloud-sync",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"message": raw[:200]}


def ref_sha(full_repo, ref):
    code, data = api("GET", "/repos/%s/git/ref/%s" % (full_repo, ref))
    if code == 200:
        return data.get("object", {}).get("sha")
    return None


def ensure_lkg(full_repo, sha, stamp):
    archive = "lkg/%s-%s" % (stamp, sha[:7])
    code, _ = api("POST", "/repos/%s/git/refs" % full_repo,
                  {"ref": "refs/tags/" + archive, "sha": sha})
    if code not in (201, 422):
        return archive, "归档 tag 失败 HTTP %s" % code
    if ref_sha(full_repo, "tags/" + LKG):
        code, _ = api("PATCH", "/repos/%s/git/refs/tags/%s" % (full_repo, LKG),
                      {"sha": sha, "force": True})
    else:
        code, _ = api("POST", "/repos/%s/git/refs" % full_repo,
                      {"ref": "refs/tags/" + LKG, "sha": sha})
    if code not in (200, 201):
        return archive, "滚动 tag 失败 HTTP %s" % code
    return archive, ""


def health_check(upstream_full, fork_sha, up_sha):
    code, cmp_data = api("GET", "/repos/%s/compare/%s...%s" % (upstream_full, fork_sha, up_sha))
    if code != 200:
        return False, "compare 不可读 HTTP %s（不阻断）" % code
    files = cmp_data.get("files") or []
    removed = sum(1 for f in files if f.get("status") == "removed")
    additions = sum(f.get("additions", 0) for f in files)
    deletions = sum(f.get("deletions", 0) for f in files)
    if len(files) >= 10 and removed / len(files) > 0.5:
        return True, "可疑：删除 %d/%d 个文件" % (removed, len(files))
    if additions == 0 and deletions > 1000:
        return True, "可疑：零新增、删除 %d 行" % deletions
    return False, "体检通过（%d 文件，+%d/-%d）" % (len(files), additions, deletions)


def main():
    mode = os.environ.get("MODE", "sync")
    target = os.environ.get("TARGET_REPO", "")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    lines, errors = [], 0

    for repo in REPOS:
        if target and repo["name"] != target:
            continue
        fork = "%s/%s" % (OWNER, repo["name"])
        branch = repo["branch"]
        item = ["- **%s**" % repo["name"]]

        fork_sha = ref_sha(fork, "heads/" + branch)
        up_sha = ref_sha(repo["upstream"], "heads/" + branch)
        if not fork_sha or not up_sha:
            item.append("读取 tip 失败（fork=%s upstream=%s）" % (bool(fork_sha), bool(up_sha)))
            errors += 1
            lines.append(" ".join(item))
            continue

        if mode == "restore":
            lkg = ref_sha(fork, "tags/" + LKG)
            if not lkg:
                item.append("无 last-known-good tag，无法恢复")
                errors += 1
            else:
                code, _ = api("PATCH", "/repos/%s/git/refs/heads/%s" % (fork, branch),
                              {"sha": lkg, "force": True})
                item.append("恢复 %s -> %s：HTTP %s" % (fork_sha[:7], lkg[:7], code))
                if code != 200:
                    errors += 1
            lines.append(" ".join(item))
            continue

        if mode == "sync" and fork_sha == up_sha:
            item.append("已一致，跳过")
            lines.append(" ".join(item))
            continue

        archive, err = ensure_lkg(fork, fork_sha, stamp)
        if err:
            item.append(err)
            errors += 1
            lines.append(" ".join(item))
            continue
        item.append("LKG=%s [%s]" % (fork_sha[:7], archive))

        if mode == "init-tags":
            item.append("基线已建立，未同步")
            lines.append(" ".join(item))
            continue

        suspicious, note = health_check(repo["upstream"], fork_sha, up_sha)
        item.append(note)
        if suspicious:
            item.append("**已跳过同步，fork 保持在最后有效版本**")
            errors += 1
            lines.append(" ".join(item))
            continue

        code, body = api("POST", "/repos/%s/merge-upstream" % fork, {"branch": branch})
        detail = body.get("merge_type") or body.get("message", "")
        item.append("merge-upstream HTTP %s / %s" % (code, detail))
        if code not in (200, 422):
            errors += 1
        lines.append(" ".join(item))

    output = "\n".join(lines)
    print(output)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write("## cloud fork sync (%s)\n\n" % mode + output + "\n")
    if errors:
        print("存在 %d 项需关注，run 标红" % errors, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
