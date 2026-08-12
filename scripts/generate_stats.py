#!/usr/bin/env python3
"""
Self-hosted GitHub stats generator.

Runs inside GitHub Actions on a schedule (see .github/workflows/update-stats.yml).
Pulls real numbers straight from the GitHub API and renders a single
self-contained SVG card. No third-party Vercel/Heroku service is involved
in producing this image, so it can't 404 or sit in "deployment paused"
limbo the way the earlier README embeds did.

Auth model (deliberately soft-required):
  - GH_USER_TOKEN: optional. A fine-grained personal access token with only
    the "read:user" scope, stored as a repo secret named STATS_PAT — never
    hard-coded, never logged, never printed anywhere in this file or in CI
    output. It is only used to authenticate outbound requests; nothing here
    ever writes/echoes the token value itself.

  Two GitHub API quirks make this token matter more than you'd expect for
  a "just read some public numbers" script:
    1. The Search API (used for PR counts) rejects author-scoped queries
       from fully unauthenticated requests, and the Actions-issued
       GITHUB_TOKEN doesn't have the right kind of identity for it either
       — you need a token that actually belongs to a user account.
    2. This specific account's public REST profile (`/users/{username}`)
       under-reports `followers` when queried unauthenticated (0 instead
       of the real count) — seemingly some anti-scraping throttling GitHub
       applies to certain accounts for anonymous callers. Authenticated
       calls return the correct number.

  Rather than "hardening" around that right now (asking the user to wire
  up the secret before anything ships), this script just treats every
  auth-dependent figure as OPTIONAL: if it can't be fetched reliably, the
  corresponding card is left out of the SVG entirely instead of rendering
  a misleading "0". Add the STATS_PAT secret whenever convenient and the
  missing cards will simply appear on the next scheduled run — no code
  changes required.
"""
import json
import os
import sys
import urllib.request
import urllib.error

USERNAME = os.environ.get("GH_USERNAME", "Codenuclei")
USER_TOKEN = os.environ.get("GH_USER_TOKEN", "").strip()

# Shared headers for plain REST calls. Authorization is only attached when
# a token is actually present — everything downstream treats "authed" vs
# "anonymous" as a first-class fact rather than assuming success.
REST_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": USERNAME}
if USER_TOKEN:
    REST_HEADERS["Authorization"] = f"Bearer {USER_TOKEN}"


def rest_get(path, params=""):
    """GET a GitHub REST endpoint. Returns None (not an exception) on any
    HTTP error, so callers can treat "couldn't fetch" as a normal case
    rather than a crash — that's the whole point of the graceful-skip
    design described above."""
    url = f"https://api.github.com{path}{params}"
    req = urllib.request.Request(url, headers=REST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"WARN: REST {path} -> {e.code}", file=sys.stderr)
        return None


def graphql(query, variables):
    """POST a GraphQL query. Deliberately short-circuits to None when no
    user token is configured, since GitHub's GraphQL API has no useful
    unauthenticated mode at all (unlike some REST endpoints)."""
    if not USER_TOKEN:
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USERNAME,
        "Authorization": f"Bearer {USER_TOKEN}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"WARN: GraphQL -> {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return None


def fetch_user():
    """Public profile fields (public_repos, followers, ...). Works
    unauthenticated, but see the followers caveat in the module docstring —
    followers is only trusted downstream when USER_TOKEN is set."""
    return rest_get(f"/users/{USERNAME}") or {}


def fetch_pr_counts():
    """Lifetime PR counts via the Search API. Returns (None, None) instead
    of (0, 0) when unauthenticated/unavailable, so the caller can tell
    "no data" apart from "genuinely zero PRs"."""
    if not USER_TOKEN:
        return None, None
    total = rest_get("/search/issues", f"?q=author:{USERNAME}+type:pr")
    merged = rest_get("/search/issues", f"?q=author:{USERNAME}+type:pr+is:merged")
    if total is None or merged is None:
        return None, None
    return total.get("total_count"), merged.get("total_count")


def fetch_contributions():
    """Rolling 12-month contribution calendar + related counts, via
    GraphQL. Returns None outright if no token is configured or the query
    fails — this is the piece we're explicitly not hardening right now.

    Also pulls the day-by-day calendar (weeks -> contributionDays) so we
    can compute streaks ourselves in `compute_streaks()` below, instead of
    embedding streak-stats.demolab.com in the README — that's yet another
    third-party Vercel-style service in the same category that has already
    gone down on us once (pixel-profile) and lost its endpoint on us once
    (github-readme-stats). One less external dependency, one less thing
    that can silently break.
    """
    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
          totalCommitContributions
          totalIssueContributions
          totalPullRequestContributions
          totalPullRequestReviewContributions
        }
        repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, PULL_REQUEST]) {
          totalCount
        }
      }
    }
    """
    data = graphql(query, {"login": USERNAME})
    if not data or "data" not in data or not data["data"].get("user"):
        return None
    u = data["data"]["user"]
    cc = u["contributionsCollection"]
    days = [
        day
        for week in cc["contributionCalendar"]["weeks"]
        for day in week["contributionDays"]
    ]
    return {
        "total_contributions": cc["contributionCalendar"]["totalContributions"],
        "commits": cc["totalCommitContributions"],
        "issues": cc["totalIssueContributions"],
        "prs": cc["totalPullRequestContributions"],
        "reviews": cc["totalPullRequestReviewContributions"],
        "orgs_contributed": u["repositoriesContributedTo"]["totalCount"],
        "days": days,
    }


def compute_streaks(days):
    """Current + longest daily contribution streak from the calendar's
    daily counts, computed locally instead of trusting a third-party
    streak-card service. `days` is already in chronological order (oldest
    first) since that's how GitHub returns the weeks array.

    "Current streak" walks backward from today/yesterday: a streak that's
    still active includes today only if today already has a contribution;
    otherwise it counts back from yesterday so a streak isn't shown as
    broken just because it's still early in the current day.
    """
    if not days:
        return 0, 0

    longest = 0
    running = 0
    for d in days:
        if d["contributionCount"] > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0

    current = 0
    idx = len(days) - 1
    # If today has no contributions yet, start counting from yesterday.
    if days[idx]["contributionCount"] == 0:
        idx -= 1
    while idx >= 0 and days[idx]["contributionCount"] > 0:
        current += 1
        idx -= 1

    return current, longest


def fetch_repo_data():
    """One shared fetch of owned, non-fork repos — feeds both the
    language-mix bar and the star/fork totals below, so we don't hit the
    same endpoint twice. Unauthenticated is fine here: this account only
    has 48 repos, well under one page (per_page=100), so pagination isn't
    a concern, and repo lists aren't subject to the same anti-scraping
    under-reporting that `followers` runs into."""
    return [r for r in (rest_get(f"/users/{USERNAME}/repos", "?per_page=100&type=owner") or []) if not r.get("fork")]


def language_mix(repos):
    counts = {}
    for r in repos:
        lang = r.get("language")
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:6]


def repo_totals(repos):
    """Stars/forks earned across all owned repos — safe, meaningful, and
    always available unauthenticated, unlike PR counts or contributions.
    Used to keep the card row from looking sparse when no token is set."""
    return sum(r.get("stargazers_count", 0) for r in repos), sum(r.get("forks_count", 0) for r in repos)


# GitHub's own per-language colors (linguist palette), used so the bar
# matches what people expect from repo language badges elsewhere on GitHub.
LANG_COLORS = {
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Python": "#3572A5",
    "HTML": "#e34c26", "C": "#555555", "C++": "#f34b7d", "CUDA": "#76b900",
    "Rust": "#dea584", "Solidity": "#AA6746", "PHP": "#4F5D95",
    "Jupyter Notebook": "#DA5B0B",
}


def build_cards(user, stars, forks, pr_total, pr_merged, followers_trusted, contrib):
    """Decide which stat cards actually have trustworthy data to show.

    Each entry is (label, value, gradient_start, gradient_end). A card is
    only included when its value is real — this is the "skip instead of
    fake it" behavior described in the module docstring. The first three
    (repos, stars, gists) are always safe unauthenticated, which also
    keeps the card row from looking sparse before STATS_PAT is ever set
    up; everything after that is conditional on having a real number.
    """
    cards = [
        ("PUBLIC REPOS", user.get("public_repos", 0), "#00f5d4", "#00bbf9"),
        ("STARS EARNED", stars, "#fb923c", "#fbbf24"),
        ("PUBLIC GISTS", user.get("public_gists", 0), "#60a5fa", "#00d4aa"),
    ]

    if pr_total is not None and pr_merged is not None:
        cards.append(("PULL REQUESTS", pr_total, "#9b5de5", "#f15bb5"))
        cards.append(("MERGED", pr_merged, "#fee440", "#f15bb5"))

    if followers_trusted is not None:
        cards.append(("FOLLOWERS", followers_trusted, "#00bbf9", "#9b5de5"))

    if contrib:
        current_streak, longest_streak = compute_streaks(contrib["days"])
        cards.append(("CONTRIBUTIONS/YR", contrib["total_contributions"], "#3fb950", "#00d4aa"))
        cards.append(("CURRENT STREAK", current_streak, "#fb7185", "#f97316"))
        cards.append(("LONGEST STREAK", longest_streak, "#f97316", "#fbbf24"))
        cards.append(("ORGS CONTRIBUTED", contrib["orgs_contributed"], "#f85149", "#ff6b35"))

    return cards


def render_svg(cards, lang_mix):
    """Render the final SVG: a row of animated stat cards on top, a
    language-mix bar + legend underneath. `cards` is already filtered down
    to entries with real data by build_cards()."""
    lang_total = sum(c for _, c in lang_mix) or 1
    bar_x = 24
    bar_w = 1104
    segments = []
    legend = []
    for i, (lang, count) in enumerate(lang_mix):
        seg_w = round(bar_w * count / lang_total)
        color = LANG_COLORS.get(lang, "#8b93a8")
        # Rounded end-caps only on the first/last segment so the bar reads
        # as one continuous pill rather than a row of separate chips.
        segments.append(
            f'<rect class="bar" x="{bar_x}" y="2" width="{seg_w}" height="24" '
            f'rx="{4 if i == 0 or i == len(lang_mix)-1 else 0}" fill="{color}" '
            f'style="animation-delay:{i*0.08:.2f}s"/>'
        )
        pct = round(100 * count / lang_total)
        legend.append(
            f'<circle cx="{24 + i*190}" cy="0" r="5" fill="{color}"/>'
            f'<text x="{34 + i*190}" y="4" fill="#c9d1d9" font-size="11">{lang} {pct}%</text>'
        )
        bar_x += seg_w

    # Cards wrap onto a second row once there are more than 6 — beyond
    # that, shrinking a single row to fit 1152px makes the card text
    # illegible. Two rows of up to 5 keeps each card comfortably >= 130px
    # wide across the whole 3-to-10-card range this can produce (3 with no
    # token, up to 10 once STATS_PAT unlocks every optional figure).
    n = len(cards)
    gap = 16
    per_row = n if n <= 6 else -(-n // 2)  # ceil(n/2) for the wrap case
    row_h = 110
    row_gap = 14
    usable_w = 1152 - 48  # 24px margin each side
    card_w = min(175, (usable_w - (per_row - 1) * gap) / per_row)
    label_size = 9 if card_w > 120 else 7.5
    value_size = 34 if card_w > 120 else 27

    rows = [cards[:per_row], cards[per_row:]] if n > per_row else [cards]
    card_svgs = []
    grads = []
    idx = 0
    for row_i, row_cards in enumerate(rows):
        row_w = len(row_cards) * card_w + (len(row_cards) - 1) * gap
        row_x = round((1152 - row_w) / 2)  # center each row independently
        y = 20 + row_i * (row_h + row_gap)
        for i, (label, value, c1, c2) in enumerate(row_cards):
            x = row_x + i * (card_w + gap)
            grads.append(
                f'<linearGradient id="cg{idx}" x1="0%" y1="0%" x2="100%" y2="100%">'
                f'<stop offset="0%" stop-color="{c1}"/><stop offset="100%" stop-color="{c2}"/></linearGradient>'
            )
            # Stagger each card's float animation delay so they don't all
            # bob up and down in perfect unison — reads as more "alive".
            card_svgs.append(f'''
  <g class="card" transform="translate({x:.1f},{y:.1f})" style="animation-delay:-{(idx%4)*1.5:.1f}s">
    <rect width="{card_w:.1f}" height="{row_h}" rx="12" fill="#12121a" stroke="url(#cg{idx})" stroke-width="1.5" filter="url(#glass)"/>
    <text x="14" y="32" fill="#8b93a8" font-size="{label_size}" letter-spacing="0.5">{label}</text>
    <text x="14" y="76" fill="url(#cg{idx})" font-size="{value_size}" font-weight="bold" class="stat-num">{value}</text>
  </g>''')
            idx += 1

    # Canvas height grows to make room for the second card row, then the
    # language bar section always sits a fixed distance below the last row.
    num_rows = len(rows)
    cards_block_h = num_rows * row_h + (num_rows - 1) * row_gap
    lang_section_y = 20 + cards_block_h + 30
    total_height = lang_section_y + 130

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1152" height="{total_height}" viewBox="0 0 1152 {total_height}">
  <defs>
    {"".join(grads)}
    <filter id="glass" x="-20%" y="-20%" width="140%" height="140%">
      <feTurbulence type="fractalNoise" baseFrequency="0.03" numOctaves="2" result="n"/>
      <feDisplacementMap in="SourceGraphic" in2="n" scale="4"/>
    </filter>
    <radialGradient id="bg" cx="50%" cy="0%" r="90%">
      <stop offset="0%" stop-color="#161622"/><stop offset="100%" stop-color="#0a0a0f"/>
    </radialGradient>
  </defs>
  <style>
    @keyframes cardFloat {{ 0%,100% {{ transform: translateY(0); }} 50% {{ transform: translateY(-5px); }} }}
    @keyframes barGrow {{ 0% {{ transform: scaleX(0); }} 100% {{ transform: scaleX(1); }} }}
    .card {{ animation: cardFloat 6s ease-in-out infinite; }}
    .bar {{ transform-origin: left; animation: barGrow 1.2s cubic-bezier(.16,1,.3,1) forwards; }}
    text {{ font-family: 'SF Mono','Fira Code',monospace; }}
  </style>
  <rect width="1152" height="{total_height}" fill="url(#bg)" rx="16"/>
  {"".join(card_svgs)}
  <g transform="translate(0,{lang_section_y})">
    <text x="24" y="0" fill="#8b93a8" font-size="11" letter-spacing="2">LANGUAGE MIX — LIVE FROM GITHUB API</text>
    <g transform="translate(0,16)">
      <rect x="24" y="0" width="1104" height="28" rx="6" fill="#12121a" stroke="#2a2a3a"/>
      {"".join(segments)}
    </g>
    <g transform="translate(0,68)">
      {"".join(legend)}
    </g>
  </g>
  <text x="24" y="{total_height-14}" fill="#5a6272" font-size="9">auto-generated · github actions · zero third-party dependency</text>
</svg>'''
    return svg


def main():
    user = fetch_user()
    repos = fetch_repo_data()
    stars, forks = repo_totals(repos)
    lang_mix = language_mix(repos)
    pr_total, pr_merged = fetch_pr_counts()
    contrib = fetch_contributions()

    # Followers is only trusted when authenticated — see module docstring
    # for why unauthenticated calls under-report this specific field.
    followers_trusted = user.get("followers") if USER_TOKEN else None

    cards = build_cards(user, stars, forks, pr_total, pr_merged, followers_trusted, contrib)
    svg = render_svg(cards, lang_mix)

    os.makedirs("assets", exist_ok=True)
    with open("assets/live-stats.svg", "w") as f:
        f.write(svg)

    shown = ", ".join(label for label, *_ in cards)
    print(f"Generated assets/live-stats.svg — authed={'yes' if USER_TOKEN else 'no'} — cards shown: {shown}")


if __name__ == "__main__":
    main()
