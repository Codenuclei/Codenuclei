#!/usr/bin/env python3
"""
Self-hosted GitHub stats generator.

Runs inside GitHub Actions. Pulls real data straight from the GitHub API
(REST + GraphQL) and renders a self-contained multi-color SVG — no
third-party Vercel/Heroku service in the loop, so nothing can silently 404
or go into deployment-paused limbo again.

Auth:
  - GH_TOKEN: a token with `read:user` scope, provided via a repo secret
    (never hard-coded, never logged). Falls back to unauthenticated REST
    calls for anything that doesn't strictly need it, so the script still
    produces a card even if the secret isn't configured yet.
"""
import json
import os
import sys
import urllib.request
import urllib.error

USERNAME = os.environ.get("GH_USERNAME", "Codenuclei")

# REST_TOKEN: any authenticated token works here (the Actions-provided
# GITHUB_TOKEN is enough) — the search API just needs *some* auth, no
# special scopes. USER_TOKEN: only needed for the GraphQL contributions
# query below, which requires a token belonging to the actual user
# (a fine-grained PAT with read:user scope, stored as a repo secret).
REST_TOKEN = os.environ.get("GH_TOKEN", "").strip()
USER_TOKEN = os.environ.get("GH_USER_TOKEN", "").strip()

REST_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": USERNAME}
if REST_TOKEN:
    REST_HEADERS["Authorization"] = f"Bearer {REST_TOKEN}"


def rest_get(path, params=""):
    url = f"https://api.github.com{path}{params}"
    req = urllib.request.Request(url, headers=REST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"WARN: REST {path} -> {e.code}", file=sys.stderr)
        return None


def graphql(query, variables):
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
    return rest_get(f"/users/{USERNAME}") or {}


def fetch_pr_counts():
    total = rest_get("/search/issues", f"?q=author:{USERNAME}+type:pr")
    merged = rest_get("/search/issues", f"?q=author:{USERNAME}+type:pr+is:merged")
    return (
        total.get("total_count", 0) if total else 0,
        merged.get("total_count", 0) if merged else 0,
    )


def fetch_contributions():
    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar { totalContributions }
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
    return {
        "total_contributions": cc["contributionCalendar"]["totalContributions"],
        "commits": cc["totalCommitContributions"],
        "issues": cc["totalIssueContributions"],
        "prs": cc["totalPullRequestContributions"],
        "reviews": cc["totalPullRequestReviewContributions"],
        "orgs_contributed": u["repositoriesContributedTo"]["totalCount"],
    }


def fetch_language_mix():
    repos = rest_get(f"/users/{USERNAME}/repos", "?per_page=100&type=owner") or []
    counts = {}
    for r in repos:
        if r.get("fork"):
            continue
        lang = r.get("language")
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:6]


LANG_COLORS = {
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Python": "#3572A5",
    "HTML": "#e34c26", "C": "#555555", "C++": "#f34b7d", "CUDA": "#76b900",
    "Rust": "#dea584", "Solidity": "#AA6746", "PHP": "#4F5D95",
    "Jupyter Notebook": "#DA5B0B",
}


def render_svg(user, pr_total, pr_merged, contrib, lang_mix):
    public_repos = user.get("public_repos", 0)
    followers = user.get("followers", 0)
    orgs = contrib["orgs_contributed"] if contrib else 0
    total_contrib = contrib["total_contributions"] if contrib else 0

    lang_total = sum(c for _, c in lang_mix) or 1
    bar_x = 24
    bar_w = 1104
    segments = []
    legend = []
    for i, (lang, count) in enumerate(lang_mix):
        seg_w = round(bar_w * count / lang_total)
        color = LANG_COLORS.get(lang, "#8b93a8")
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

    cards = [
        ("PUBLIC REPOS", public_repos, "#00f5d4", "#00bbf9"),
        ("PULL REQUESTS", pr_total, "#9b5de5", "#f15bb5"),
        ("MERGED", pr_merged, "#fee440", "#f15bb5"),
        ("FOLLOWERS", followers, "#00bbf9", "#9b5de5"),
    ]
    if contrib:
        cards.append(("CONTRIBUTIONS/YR", total_contrib, "#3fb950", "#00d4aa"))
        cards.append(("ORGS CONTRIBUTED", orgs, "#f85149", "#ff6b35"))

    card_w, card_h, gap = 175, 110, 16
    card_svgs = []
    grads = []
    for i, (label, value, c1, c2) in enumerate(cards):
        x = 24 + i * (card_w + gap)
        grads.append(
            f'<linearGradient id="cg{i}" x1="0%" y1="0%" x2="100%" y2="100%">'
            f'<stop offset="0%" stop-color="{c1}"/><stop offset="100%" stop-color="{c2}"/></linearGradient>'
        )
        card_svgs.append(f'''
  <g class="card" transform="translate({x},20)" style="animation-delay:-{(i%4)*1.5:.1f}s">
    <rect width="{card_w}" height="{card_h}" rx="12" fill="#12121a" stroke="url(#cg{i})" stroke-width="1.5" filter="url(#glass)"/>
    <text x="16" y="34" fill="#8b93a8" font-size="9" letter-spacing="1">{label}</text>
    <text x="16" y="76" fill="url(#cg{i})" font-size="34" font-weight="bold" class="stat-num">{value}</text>
  </g>''')

    total_width = 24 + len(cards) * (card_w + gap)
    total_height = 300

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
  <g transform="translate(0,170)">
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
    pr_total, pr_merged = fetch_pr_counts()
    contrib = fetch_contributions()
    lang_mix = fetch_language_mix()

    svg = render_svg(user, pr_total, pr_merged, contrib, lang_mix)

    os.makedirs("assets", exist_ok=True)
    with open("assets/live-stats.svg", "w") as f:
        f.write(svg)

    print(f"Generated assets/live-stats.svg — repos={user.get('public_repos')} prs={pr_total} merged={pr_merged} contrib_auth={'yes' if contrib else 'no'}")


if __name__ == "__main__":
    main()
