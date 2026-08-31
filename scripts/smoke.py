"""Sanity-check your .env before starting the web app.

Signs in to Tableau Cloud and prints the current jobs. Catches a bad PAT or
wrong site content URL early, with a clear error, instead of failing inside
the UI.

    python scripts/smoke.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

import tableau


def main() -> None:
    load_dotenv()
    session = tableau.TableauSession()
    site = session.config.site_content_url or "<default>"
    print(f"Signing in to {session.config.server_url} (site: {site}) ...")
    jobs = tableau.list_jobs(session)
    print(f"Signed in OK. {len(jobs)} job(s) currently on the site:")
    for job in jobs[:20]:
        print(f"  - [{job['status']}] {job['type']} (id={job['id']})")
    if len(jobs) > 20:
        print(f"  ... and {len(jobs) - 20} more")


if __name__ == "__main__":
    main()
