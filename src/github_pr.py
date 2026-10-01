"""
Minimal GitHub REST API wrapper for the eval agent's auto-tuning flow
(see eval_agent.py and main.py's handle_commands()).

Scope is deliberately narrow: this only ever patches a single numeric
constant in a single known file (see TUNABLE_PARAMS below), never
arbitrary LLM-generated code. A GitHub PR is opened for the change; it
only gets merged if you reply YES to the Telegram message that names it
-- a bare "yes" is a strong enough review for "this one number changes
within a pre-approved safe range," but would NOT be a safe way to
approve an arbitrary code diff you never actually saw, which is exactly
why this stays scoped to numeric constants instead of open-ended code
changes.

Needs GITHUB_TOKEN (a fine-grained PAT scoped to just this repo, with
Contents + Pull requests read/write -- nothing else) and GITHUB_REPO
("owner/repo"). Without a token configured, every function here returns
None/False immediately -- this whole flow is optional, same pattern as
Telegram/Anthropic being unconfigured elsewhere.
"""
import base64
import re

import requests

from . import config

API_BASE = "https://api.github.com"

# Whitelisted tunable constants: only these can ever be auto-PR'd. Each
# entry names the file (relative to the repo root) and file), the
# constant's name as it appears in source ("NAME = <number>"), and a safe
# min/max range the eval agent's suggestion is clamped to regardless of
# what it asks for.
TUNABLE_PARAMS = {
    "VOLUME_CONFIRM_MULTIPLE": {"file": "src/strategy.py", "min": 1.0, "max": 2.5},
    "STOP_R_MULTIPLE": {"file": "src/strategy.py", "min": 0.5, "max": 2.0},
    "TARGET_R_MULTIPLE": {"file": "src/strategy.py", "min": 1.0, "max": 4.0},
}


def _headers():
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _configured() -> bool:
    return bool(config.GITHUB_TOKEN and config.GITHUB_REPO)


def patch_constant(file_content: str, const_name: str, new_value: float) -> str | None:
    """Replaces NAME = <number> with the new value, preserving everything
    else on that line (comments included). Returns None (refuses to
    guess) unless the constant appears EXACTLY once -- a deliberately
    conservative safety check before ever touching a file automatically."""
    pattern = re.compile(rf"^({re.escape(const_name)}\s*=\s*)[\d.]+", re.MULTILINE)
    new_content, count = pattern.subn(rf"\g<1>{new_value}", file_content)
    if count != 1:
        print(f"[github_pr] expected exactly 1 occurrence of '{const_name}', found {count} -- refusing to patch")
        return None
    return new_content


def open_tuning_pr(param: str, new_value: float, reason: str) -> dict | None:
    """Creates a branch, patches the one whitelisted constant, and opens a
    PR against GITHUB_BASE_BRANCH. Returns {"number":, "url":, "branch":}
    on success, or None on any failure (never raises) -- a failure here
    just means no PR was opened, handled gracefully by the caller."""
    if not _configured():
        print("[github_pr] GITHUB_TOKEN/GITHUB_REPO not configured, skipping PR")
        return None

    spec = TUNABLE_PARAMS.get(param)
    if not spec:
        print(f"[github_pr] '{param}' is not a whitelisted tunable parameter, refusing")
        return None

    new_value = max(spec["min"], min(spec["max"], new_value))
    repo = config.GITHUB_REPO
    base = config.GITHUB_BASE_BRANCH
    path = spec["file"]

    try:
        # 1. Base branch's latest commit SHA
        resp = requests.get(f"{API_BASE}/repos/{repo}/git/ref/heads/{base}", headers=_headers(), timeout=15)
        resp.raise_for_status()
        base_sha = resp.json()["object"]["sha"]

        # 2. Create a new branch from it
        from . import timeutil

        branch = f"eval-tune-{param.lower()}-{timeutil.now_ist().strftime('%Y%m%d-%H%M%S')}"
        resp = requests.post(
            f"{API_BASE}/repos/{repo}/git/refs",
            headers=_headers(),
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            timeout=15,
        )
        resp.raise_for_status()

        # 3. Read the current file content + its blob SHA (needed to update it)
        resp = requests.get(
            f"{API_BASE}/repos/{repo}/contents/{path}", headers=_headers(), params={"ref": branch}, timeout=15
        )
        resp.raise_for_status()
        file_data = resp.json()
        current_content = base64.b64decode(file_data["content"]).decode("utf-8")
        file_sha = file_data["sha"]

        # 4. Patch just that one constant
        new_content = patch_constant(current_content, param, new_value)
        if new_content is None:
            return None

        # 5. Commit the change to the new branch
        resp = requests.put(
            f"{API_BASE}/repos/{repo}/contents/{path}",
            headers=_headers(),
            json={
                "message": f"Auto-tune {param} to {new_value} (eval agent suggestion)",
                "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                "sha": file_sha,
                "branch": branch,
            },
            timeout=15,
        )
        resp.raise_for_status()

        # 6. Open the PR
        resp = requests.post(
            f"{API_BASE}/repos/{repo}/pulls",
            headers=_headers(),
            json={
                "title": f"Eval agent: tune {param} to {new_value}",
                "body": f"Proposed by the daily eval agent.\n\n**Reason:** {reason}\n\n"
                f"Clamped to the whitelisted safe range [{spec['min']}, {spec['max']}]. "
                f"Only merges if approved via Telegram.",
                "head": branch,
                "base": base,
            },
            timeout=15,
        )
        resp.raise_for_status()
        pr = resp.json()
        return {"number": pr["number"], "url": pr["html_url"], "branch": branch}
    except requests.RequestException as e:
        print(f"[github_pr] failed to open tuning PR: {e.__class__.__name__}: {e}")
        return None


def merge_pr(pr_number: int) -> bool:
    if not _configured():
        return False
    try:
        resp = requests.put(
            f"{API_BASE}/repos/{config.GITHUB_REPO}/pulls/{pr_number}/merge",
            headers=_headers(),
            json={"merge_method": "squash"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[github_pr] failed to merge PR #{pr_number}: {e.__class__.__name__}: {e}")
        return False


def close_pr(pr_number: int) -> bool:
    if not _configured():
        return False
    try:
        resp = requests.patch(
            f"{API_BASE}/repos/{config.GITHUB_REPO}/pulls/{pr_number}",
            headers=_headers(),
            json={"state": "closed"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[github_pr] failed to close PR #{pr_number}: {e.__class__.__name__}: {e}")
        return False
