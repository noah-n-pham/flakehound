"""Read the production boards back over the tunnel and print them side by side.

The claim `/public/flaky` makes is a negative one — a private repo's rows are *not*
there — and a negative is only evidence if the rows exist somewhere. So this prints
what the token-gated endpoint holds for the private repo, then the public board, then
which repos were eligible for it at all. Run against production; reads only.
"""

import argparse
import json
import os
import sys
import urllib.request


def get(base: str, path: str, token: str | None = None) -> object:
    # Cloudflare answers urllib's default `Python-urllib/3.12` with a 403 before the
    # request ever reaches the tunnel, so this has to name itself.
    request = urllib.request.Request(f"{base}{path}", headers={"User-Agent": "flakehound-scripts"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("API_BASE_URL"))
    parser.add_argument("--repo-id", type=int, required=True, help="the private repo to contrast")
    parser.add_argument("--window-days", type=int, default=90)
    args = parser.parse_args()

    token = os.environ.get("INTERNAL_API_TOKEN")
    if not args.base_url or not token:
        sys.exit("API_BASE_URL and INTERNAL_API_TOKEN must be set")
    window = f"?window_days={args.window_days}"

    print(f"authenticated  /api/repos/{args.repo_id}/flaky")
    for row in get(args.base_url, f"/api/repos/{args.repo_id}/flaky{window}", token):
        print(
            f"  {row['job_name']:<18} opps {row['opportunities']:>3}"
            f"  failures {row['failures']}  flakes {row['flakes']}"
            f"  wilson {row['wilson_lower']:.4f}-{row['wilson_upper']:.4f}"
        )

    print("\nno token       /public/flaky")
    board = get(args.base_url, f"/public/flaky{window}")
    for row in board:
        print(
            f"  {row['repo_full_name']:<26} {row['job_name']:<18}"
            f" opps {row['opportunities']:>3}  flakes {row['flakes']}"
            f"  wilson {row['wilson_lower']:.4f}-{row['wilson_upper']:.4f}"
        )
    print(f"  {len(board)} rows")

    print("\nwhich repos were eligible")
    for row in get(args.base_url, "/api/repos", token):
        eligible = not row["private"] and row["active"]
        print(
            f"  {row['full_name']:<26} private={row['private']!s:<5}"
            f" active={row['active']!s:<5} jobs={row['job_count']:<3}"
            f" -> {'on the public board' if eligible else 'filtered out'}"
        )


if __name__ == "__main__":
    main()
