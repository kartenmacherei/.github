#!/usr/bin/env python3
"""Scan GitHub for stale work belonging to the authenticated gh user."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

ORG_FILTER = __import__("os").environ.get("GITHUB_ORG", "").strip()


def with_org_scope(query: str) -> str:
    if ORG_FILTER:
        return f"org:{ORG_FILTER} {query}"
    return query

NO_REVIEW_DAYS = 7
PENDING_REVIEW_DAYS = 3
INACTIVE_ISSUE_DAYS = 14
OLD_DRAFT_DAYS = 14
TIER_RED_DAYS = 21
TIER_YELLOW_DAYS = 7
PAGE_SIZE = 50
MAX_PAGES = 4


@dataclass
class StaleItem:
    category: str
    repo: str
    title: str
    url: str
    stale_days: int
    involved: list[str] = field(default_factory=list)
    extra: str = ""

    @property
    def tier(self) -> str:
        if self.stale_days >= TIER_RED_DAYS:
            return "red"
        if self.stale_days >= TIER_YELLOW_DAYS:
            return "yellow"
        return "green"

    def format_line(self) -> str:
        people = ", ".join(self.involved) if self.involved else "—"
        suffix = f" — {self.extra}" if self.extra else ""
        return (
            f"- **{self.repo}** — [{self.title}]({self.url})  \n"
            f"  Stale **{self.stale_days}d** · {self.category}{suffix}  \n"
            f"  Involved: {people}"
        )


def run_gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh failed: {' '.join(args)}")
    return result.stdout


def gh_graphql(query: str, **variables: Any) -> Any:
    cmd = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        cmd.extend([flag, f"{key}={value}"])
    payload = json.loads(run_gh(cmd))
    if "errors" in payload:
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def days_since(value: str, now: datetime) -> int:
    return max(0, (now - parse_dt(value)).days)


def viewer_login() -> str:
    return gh_graphql("query { viewer { login } }")["viewer"]["login"]


def search_issues_graphql(query: str, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
    gql = """
    query($q: String!, $first: Int!, $after: String) {
      search(query: $q, type: ISSUE, first: $first, after: $after) {
        issueCount
        pageInfo { hasNextPage endCursor }
        nodes {
          ... on PullRequest {
            __typename
            title url createdAt updatedAt isDraft
            repository { nameWithOwner }
            author { login }
            reviews(first: 20) {
              nodes { author { login } state submittedAt }
            }
            reviewRequests(first: 10) {
              nodes {
                requestedReviewer {
                  ... on User { login }
                  ... on Team { name slug }
                }
              }
            }
            assignees(first: 10) { nodes { login } }
          }
          ... on Issue {
            __typename
            title url createdAt updatedAt
            repository { nameWithOwner }
            author { login }
            assignees(first: 10) { nodes { login } }
            labels(first: 10) { nodes { name } }
          }
        }
      }
    }
    """
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(max_pages):
        kwargs: dict[str, Any] = {"q": query, "first": PAGE_SIZE}
        if after:
            kwargs["after"] = after
        data = gh_graphql(gql, **kwargs)
        search = data["search"]
        nodes.extend(search["nodes"])
        if not search["pageInfo"]["hasNextPage"]:
            break
        after = search["pageInfo"]["endCursor"]
    return nodes


def reviewer_logins(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for n in node.get("reviewRequests", {}).get("nodes", []):
        rr = n.get("requestedReviewer") or {}
        out.append(rr.get("login") or rr.get("name") or rr.get("slug"))
    return [x for x in out if x]


def external_reviews(node: dict[str, Any]) -> list[dict[str, Any]]:
    author = (node.get("author") or {}).get("login")
    return [
        r
        for r in node.get("reviews", {}).get("nodes", [])
        if r.get("author")
        and r["author"]["login"] != author
        and r.get("state") in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}
    ]


def my_review(node: dict[str, Any], user: str, display: str) -> bool:
    aliases = {user, display, display.removesuffix("[bot]"), f"{display.removesuffix('[bot]')}[bot]"}
    return any(
        r.get("author") and r["author"]["login"] in aliases
        for r in node.get("reviews", {}).get("nodes", [])
    )


def collect_open_prs_no_review(user: str, now: datetime) -> list[StaleItem]:
    created_before = (now - timedelta(days=NO_REVIEW_DAYS)).date().isoformat()
    nodes = search_issues_graphql(
        with_org_scope(f"author:{user} is:pr is:open created:<={created_before}")
    )
    items: list[StaleItem] = []
    for node in nodes:
        if node.get("__typename") != "PullRequest":
            continue
        if external_reviews(node):
            continue
        reviewers = reviewer_logins(node)
        author = node["author"]["login"]
        items.append(
            StaleItem(
                category="Opened PR — no review yet",
                repo=node["repository"]["nameWithOwner"],
                title=node["title"],
                url=node["url"],
                stale_days=days_since(node["createdAt"], now),
                involved=reviewers or ["no reviewers requested"],
                extra="draft" if node["isDraft"] else "ready for review",
            )
        )
    return items


def collect_pending_reviews(user: str, display: str, now: datetime) -> list[StaleItem]:
    created_before = (now - timedelta(days=PENDING_REVIEW_DAYS)).date().isoformat()
    nodes = search_issues_graphql(
        with_org_scope(
            f"review-requested:{display} is:pr is:open created:<={created_before}"
        )
    )
    items: list[StaleItem] = []
    for node in nodes:
        if node.get("__typename") != "PullRequest":
            continue
        if my_review(node, user, display):
            continue
        author = node["author"]["login"]
        others = [r for r in reviewer_logins(node) if r not in {user, display}]
        items.append(
            StaleItem(
                category="Review requested — not reviewed by you",
                repo=node["repository"]["nameWithOwner"],
                title=node["title"],
                url=node["url"],
                stale_days=days_since(node["createdAt"], now),
                involved=list(dict.fromkeys([author, *others])),
            )
        )
    return items


def collect_inactive_issues(user: str, display: str, now: datetime) -> list[StaleItem]:
    updated_before = (now - timedelta(days=INACTIVE_ISSUE_DAYS)).date().isoformat()
    nodes = search_issues_graphql(
        with_org_scope(
            f"assignee:{display} is:issue is:open updated:<={updated_before}"
        )
    )
    items: list[StaleItem] = []
    for node in nodes:
        if node.get("__typename") != "Issue":
            continue
        author = node["author"]["login"]
        assignees = [a["login"] for a in node.get("assignees", {}).get("nodes", [])]
        labels = [l["name"] for l in node.get("labels", {}).get("nodes", [])]
        items.append(
            StaleItem(
                category="Assigned issue — no recent activity",
                repo=node["repository"]["nameWithOwner"],
                title=node["title"],
                url=node["url"],
                stale_days=days_since(node["updatedAt"], now),
                involved=list(dict.fromkeys([author, *assignees])),
                extra=", ".join(labels[:3]) if labels else "unlabeled",
            )
        )
    return items


def collect_old_drafts(user: str, now: datetime) -> list[StaleItem]:
    created_before = (now - timedelta(days=OLD_DRAFT_DAYS)).date().isoformat()
    nodes = search_issues_graphql(
        with_org_scope(
            f"author:{user} is:pr is:open is:draft created:<={created_before}"
        )
    )
    items: list[StaleItem] = []
    for node in nodes:
        if node.get("__typename") != "PullRequest" or not node.get("isDraft"):
            continue
        items.append(
            StaleItem(
                category="Draft PR — stuck in draft",
                repo=node["repository"]["nameWithOwner"],
                title=node["title"],
                url=node["url"],
                stale_days=days_since(node["createdAt"], now),
                involved=[node["author"]["login"]],
            )
        )
    return items


def render_report(user: str, items: list[StaleItem]) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tiers = {
        "red": "🔴 Embarrassingly stale (3+ weeks)",
        "yellow": "🟡 Getting dusty (1–3 weeks)",
        "green": "🟢 Just needs a nudge (under 1 week)",
    }
    by_tier: dict[str, list[StaleItem]] = {"red": [], "yellow": [], "green": []}
    for item in sorted(items, key=lambda i: (-i.stale_days, i.repo, i.title)):
        by_tier[item.tier].append(item)

    lines = [
        "# GitHub Stale Work Report",
        "",
        f"**Account:** `{user}` · **Generated:** {now_str}",
        "",
    ]
    if not items:
        lines.append("✅ Nothing stale found — you're caught up!")
        return "\n".join(lines)

    lines.append(f"**{len(items)} item(s)** falling through the cracks:\n")
    for tier_key in ("red", "yellow", "green"):
        bucket = by_tier[tier_key]
        lines.append(f"## {tiers[tier_key]}")
        if not bucket:
            lines.append("_None_\n")
            continue
        for item in bucket:
            lines.append(item.format_line())
            lines.append("")
    return "\n".join(lines)


def resolve_user(arg: str | None) -> tuple[str, str]:
    """Return (search_qualifier, display_name)."""
    if arg:
        cleaned = arg.lstrip("@")
        return cleaned, cleaned
    login = viewer_login()
    if login.endswith("[bot]"):
        slug = login.removesuffix("[bot]")
        return f"app/{slug}", login
    return login, login


def main() -> int:
    now = datetime.now(timezone.utc)
    arg_user = sys.argv[1] if len(sys.argv) > 1 else __import__("os").environ.get("GITHUB_USER")
    user, display = resolve_user(arg_user)
    items: list[StaleItem] = []

    collectors = [
        lambda: collect_open_prs_no_review(user, now),
        lambda: collect_pending_reviews(user, display, now),
        lambda: collect_inactive_issues(user, display, now),
        lambda: collect_old_drafts(user, now),
    ]

    for collector in collectors:
        try:
            items.extend(collector())
        except RuntimeError as exc:
            print(f"Warning: {collector.__name__}: {exc}", file=sys.stderr)

    deduped: dict[str, StaleItem] = {}
    for item in items:
        existing = deduped.get(item.url)
        if not existing or item.stale_days > existing.stale_days:
            deduped[item.url] = item

    print(render_report(display, list(deduped.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
