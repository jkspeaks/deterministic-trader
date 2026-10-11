#!/usr/bin/env python3
"""Guided first-run setup for deterministic-trader.

Run it from the repo root:

    uv run python scripts/install.py

It checks the interpreter, confirms `uv` is on PATH, collects the values a fork
needs, writes a filled-in `.env` from `.env.example`, and prints every tracked
file a fork must still edit by hand.

By default it changes **no tracked file**: the printed checklist names each
placeholder so the edit stays yours and `git status` stays clean until you make
it. `--apply` will substitute the collected values into those files; that is
explicitly opt-in because it dirties the working tree.

Flags:
    --yes       non-interactive; do not prompt, do not create `.env`
    --no-sync   skip `uv sync --group dev`
    --test      run `uv run pytest` after setup
    --apply     also substitute the collected values into the tracked files in
                the checklist below. Review with `git diff` before committing.

It is stdlib-only and makes no network calls. Nothing here is required to run
the core locally - `uv sync --group dev` and `uv run pytest` are enough for that.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Tracked files that carry a placeholder a fork must replace. Kept deliberately
# out of the default path: editing source from an installer leaves the tree
# dirty and can conflict with upstream, so it only happens under `--apply`.
APPLY_FILES = [
    ".github/workflows/universe.yml",
    ".github/workflows/leaders.yml",
    ".github/workflows/events.yml",
    "config/inception.json",
    "ci/common.py",
    "SECURITY.md",
    "README.md",
    "docs/routine/runner-prompt.md",
    "docs/routine/cycle-prompt.md",
    "docs/routine/dip-monitor-prompt.md",
    "docs/routine/universe-pick-prompt.md",
    "docs/routine/manual-check-prompt.md",
    "docs/routines.md",
]


def _say(msg: str = "") -> None:
    print(msg)


def _section(title: str) -> None:
    _say()
    _say(f"== {title} " + "=" * max(0, 60 - len(title)))


def _ask(
    label: str,
    *,
    required: bool = False,
    default: str = "",
    validate=None,
    hint: str = "",
    warn=None,
) -> str:
    """One prompt. `validate` re-prompts on failure; `warn` accepts but notes."""
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            answer = input(f"{label}{suffix}: ").strip()
        except EOFError:
            answer = ""
        if not answer:
            answer = default
        if not answer and required:
            _say("  required - please enter a value.")
            continue
        if not answer:
            return ""
        if validate is not None and not validate(answer):
            _say(f"  {hint or 'that does not look right.'}")
            continue
        if warn is not None and not warn(answer):
            _say(f"  note: {hint or 'unusual shape'} - keeping it.")
        return answer


def check_python() -> bool:
    ok = sys.version_info >= (3, 11)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    _say(f"Python {version}: {'ok' if ok else 'TOO OLD (need >= 3.11)'}")
    return ok


def check_uv() -> bool:
    exe = shutil.which("uv")
    if exe:
        _say(f"uv: found at {exe}")
        return True
    _say("uv: NOT FOUND. Install it from https://docs.astral.sh/uv/ and re-run.")
    return False


def _valid_url(value: str) -> bool:
    return value.startswith("https://") and ".supabase.co" in value


def _valid_key(value: str) -> bool:
    return value.startswith("eyJ") or len(value) >= 32


def collect_values() -> dict:
    """Prompt for every value a fork needs. Partial dict on Ctrl-C."""
    _section("Values")
    _say("Collected once. The ten ops keys go into `.env`; the rest are recorded")
    _say("for Vercel / Supabase Vault and listed in the checklist at the end.")
    values: dict = {}
    try:
        values["owner"] = _ask(
            "GitHub owner/username", required=True,
            validate=lambda s: bool(OWNER_RE.match(s)),
            hint="letters, digits and hyphens only (e.g. octocat).",
        )
        values["email"] = _ask(
            "GitHub email", required=True,
            validate=lambda s: bool(EMAIL_RE.match(s)),
            hint="a valid address (e.g. you@example.com).",
        )
        values["url"] = _ask(
            "Supabase project URL", required=True,
            validate=_valid_url, hint="must start with https:// and contain .supabase.co",
        )
        values["key"] = _ask(
            "Supabase service_role key", required=True,
            warn=_valid_key, hint="service_role keys are JWTs starting with 'eyJ'",
        )
        values["cash"] = _ask(
            "Robinhood cash account number", required=True,
            validate=lambda s: bool(ACCOUNT_RE.match(s)),
            hint="letters, digits, hyphen or underscore only.",
        )
        values["margin"] = _ask("Robinhood margin account number (optional)")

        _say()
        _say("Optional - press Enter to skip each.")
        values["core_token"] = _ask("CORE_TOKEN (hosted-core shared secret)")
        values["supabase_url"] = _ask("SUPABASE_URL (hosted core; blank = same project)")
        values["service_role"] = _ask("SUPABASE_SERVICE_ROLE_KEY (hosted core)")
        values["twelve"] = _ask("TWELVE_DATA_API_KEY (pre-market job)")
        values["alpha"] = _ask("ALPHAVANTAGE_API_KEY (evidence back-fill)")
        values["tg_token"] = _ask("Telegram bot token (Supabase Vault)")
        values["tg_chat"] = _ask("Telegram chat id (Supabase Vault)")
    except KeyboardInterrupt:
        _say("\nStopped early; using what was entered so far.")
    return values


def write_env(values: dict, non_interactive: bool) -> bool:
    if ENV_FILE.exists():
        _say(f".env already exists at {ENV_FILE}; leaving it untouched.")
        return True
    if not ENV_EXAMPLE.exists():
        _say(f".env.example is missing at {ENV_EXAMPLE}; cannot create .env.")
        return False
    if non_interactive or not values:
        _say("No values collected; not creating .env.")
        return True

    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    url = values.get("url", "")
    key = values.get("key", "")
    if url:
        text = text.replace(
            "EDGE_SUPABASE_URL=https://your-project.supabase.co", f"EDGE_SUPABASE_URL={url}"
        )
    if key:
        text = text.replace("EDGE_SUPABASE_KEY=your-service-role-key", f"EDGE_SUPABASE_KEY={key}")

    optional = [
        ("core_token", "CORE_TOKEN", "your-core-shared-secret"),
        ("supabase_url", "SUPABASE_URL", "https://your-project.supabase.co"),
        ("service_role", "SUPABASE_SERVICE_ROLE_KEY", "your-supabase-service-role-key"),
        ("twelve", "TWELVE_DATA_API_KEY", "your-twelvedata-basic-key"),
        ("alpha", "ALPHAVANTAGE_API_KEY", "your-alphavantage-free-key"),
    ]
    for field, env_key, placeholder in optional:
        value = (values.get(field) or "").strip()
        if value:
            text = text.replace(f"# {env_key}={placeholder}", f"{env_key}={value}")

    ENV_FILE.write_text(text, encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    _say(f"Wrote {ENV_FILE} (keep it out of git; it is gitignored).")
    return True


def manual_edits(values: dict) -> None:
    _section("Still to edit by hand")
    _say("The installer does not touch these tracked files. Replace each placeholder:")
    _say()
    _say("  workflows  .github/workflows/{universe,leaders,events}.yml")
    _say('             git config user.name "<owner>", user.email "you@example.com"')
    _say("  config     config/inception.json")
    _say('             "account_number" (<your-cash-account-number>) and the margin')
    _say("             number in its \"_account_number\" note (<your-margin-account-number>)")
    _say("  ci         ci/common.py")
    _say('             EDGAR_CONTACT default "you@example.com"')
    _say('  docs       SECURITY.md, README.md, docs/routines.md, docs/routine/*.md')
    _say('             "<owner>" in clone/report URLs; the account number in the routine')
    _say('             instruction and prompts')
    _say()
    if values:
        _say("Recommended values entered:")
        for key, label in (
            ("owner", "GitHub owner"), ("email", "GitHub email"),
            ("cash", "cash account"), ("margin", "margin account"),
        ):
            if values.get(key):
                _say(f"  {label}: {values[key]}")
        _say()
    _say("Re-run with --apply to substitute these into the files above automatically.")
    _say("That dirties the working tree; review with `git diff` before committing.")


def apply_edits(values: dict) -> None:
    _section("Applying substitutions")
    missing = [k for k in ("owner", "email", "cash") if not values.get(k)]
    if missing:
        _say(f"--apply needs {', '.join(missing)}; re-run without --yes to enter them.")
        return
    replacements = {
        "<owner>": values["owner"],
        "you@example.com": values["email"],
        "<your-cash-account-number>": values["cash"],
    }
    if values.get("margin"):
        replacements["<your-margin-account-number>"] = values["margin"]

    changed = 0
    for rel in APPLY_FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        before = path.read_text(encoding="utf-8")
        after = before
        for placeholder, value in replacements.items():
            after = after.replace(placeholder, value)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed += 1
            _say(f"  updated {rel}")
    _say(f"{changed} file(s) changed. Review with `git diff`; the tree is now dirty.")


def sync_dev() -> None:
    _say("Running `uv sync --group dev` ...")
    try:
        subprocess.run(["uv", "sync", "--group", "dev"], cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        _say(f"`uv sync` failed ({e}). Run it by hand from the repo root.")


def run_tests() -> bool:
    _section("Tests")
    _say("Running `uv run pytest` ...")
    try:
        subprocess.run(["uv", "run", "pytest", "-q"], cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        _say(f"`uv run pytest` failed ({e}).")
        return False
    _say("Tests passed.")
    return True


def next_steps() -> None:
    _section("Next steps")
    _say("Local core only (no accounts, no keys, no network):")
    _say("  uv sync --group dev")
    _say("  uv run pytest")
    _say()
    _say("To deploy, follow README.md -> 'Install and run':")
    _say("  1. Supabase: create a project, run db/schema.sql once in the SQL Editor,")
    _say("     and add the Vault secrets (see SECRETS.md).")
    _say("  2. Test the alert relay with one row into edge_alerts.")
    _say("  3. Vercel: import the repo (repo root as Root Directory) and set its env vars.")
    _say("  4. GitHub: add the EDGE_SUPABASE_URL / EDGE_SUPABASE_KEY Actions secrets.")
    _say("  5. Replace config/inception.json and review config/config.json.")
    _say("  6. Create the scheduled Claude routines (docs/routines.md).")
    _say("  7. Go live: run the Manual Control Action with `status`, then `master_on`.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="non-interactive; skip prompts")
    parser.add_argument("--no-sync", action="store_true", help="skip `uv sync --group dev`")
    parser.add_argument("--test", action="store_true", help="run `uv run pytest` after setup")
    parser.add_argument("--apply", action="store_true", help="substitute values into tracked files")
    args = parser.parse_args()

    _section("deterministic-trader setup")
    _say(f"Repo root: {ROOT}")

    _section("Environment")
    have_python = check_python()
    have_uv = check_uv()

    _section("Secrets")
    values = {} if args.yes else collect_values()
    write_env(values, non_interactive=args.yes)

    _section("Manual edits")
    manual_edits(values)

    if args.apply:
        apply_edits(values)

    tests_ok = True
    if have_uv and not args.no_sync:
        _section("Dependencies")
        sync_dev()
        if args.test:
            tests_ok = run_tests()

    next_steps()

    if not (have_python and have_uv):
        _say()
        _say("Fix the items marked above, then re-run this script.")
        return 1
    if not tests_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
