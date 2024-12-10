import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from time import time

import dotenv
import matplotlib.pyplot as plt
import numpy as np
import requests

dotenv.load_dotenv()

# GitHub repository details
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OWNER = os.getenv("OWNER")
REPO = os.getenv("REPO")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")
BRANCH = os.getenv("BRANCH")

# error if any of the required env vars are not set
if not GITHUB_TOKEN or not OWNER or not REPO or not WORKFLOW_ID or not BRANCH:
    raise ValueError("Missing required environment variables")

# GitHub API base URL
BASE_URL = "https://api.github.com"

# Headers for authorization
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Update cache configuration
CACHE_DIR = Path(".cache")
WORKFLOW_CACHE_FILE = CACHE_DIR / "workflow_failures.json"
JOBS_CACHE_FILE = CACHE_DIR / "job_failures.json"
CACHE_EXPIRY = 3600  # Cache expires after 1 hour (in seconds)


def load_cache(cache_file):
    """Load cached data if it exists and isn't expired."""
    if not cache_file.exists():
        return None

    try:
        with open(cache_file, "r") as f:
            cache_data = json.load(f)

        # Check if cache is expired
        if time() - cache_data["timestamp"] > CACHE_EXPIRY:
            logger.info(
                f"Cache has expired for {cache_file.name}, will fetch fresh data"
            )
            return None

        logger.info(f"Using cached data from {cache_file.name}")
        return cache_data["data"]
    except Exception as e:
        logger.warning(f"Failed to load cache {cache_file.name}: {e}")
        return None


def save_cache(data, cache_file):
    """Save data to cache file."""
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_data = {"timestamp": time(), "data": data}
        with open(cache_file, "w") as f:
            json.dump(cache_data, f)
        logger.info(f"Saved data to {cache_file.name}")
    except Exception as e:
        logger.warning(f"Failed to save cache {cache_file.name}: {e}")


def get_failed_workflow_runs():
    """Get all failed workflow runs, using cache if available."""
    cached_runs = load_cache(WORKFLOW_CACHE_FILE)
    if cached_runs is not None:
        return cached_runs

    # If no cache or expired, fetch from GitHub
    logger.info("Fetching workflow runs from GitHub API...")
    url = f"{BASE_URL}/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW_ID}/runs"
    params = {"branch": BRANCH, "status": "failure"}

    response = requests.get(url, headers=HEADERS, params=params)
    response.raise_for_status()

    runs = response.json().get("workflow_runs", [])
    runs = sorted(runs, key=lambda x: x["created_at"], reverse=True)

    # Save to cache for next time
    save_cache(runs, WORKFLOW_CACHE_FILE)

    logger.info(f"Found {len(runs)} failed workflow runs")
    return runs


def find_failed_jobs(run_id):
    """Find failed jobs for a workflow run, using cache if available."""
    # Load jobs cache
    jobs_cache = load_cache(JOBS_CACHE_FILE) or {}

    # Check if we have cached jobs for this run
    if str(run_id) in jobs_cache:
        return jobs_cache[str(run_id)]

    # If not in cache, fetch from API
    url = f"{BASE_URL}/repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()

    jobs = response.json().get("jobs", [])
    failed_jobs = [job for job in jobs if job["conclusion"] == "failure"]

    # Update cache with new jobs
    jobs_cache[str(run_id)] = failed_jobs
    save_cache(jobs_cache, JOBS_CACHE_FILE)

    return failed_jobs


def create_failure_timeline(job_failures):
    """Create a heatmap of failures over time."""
    daily_failures = defaultdict(lambda: defaultdict(int))

    for run in job_failures:
        for job in run["jobs"]:
            date = datetime.fromisoformat(
                job["started_at"].replace("Z", "+00:00")
            ).date()
            job_name = job["name"]
            daily_failures[date][job_name] += 1

    dates = sorted(daily_failures.keys())
    job_names = sorted(
        set(
            job_name
            for failures in daily_failures.values()
            for job_name in failures.keys()
        )
    )

    # Create the data matrix
    data = np.zeros((len(job_names), len(dates)))
    for i, job in enumerate(job_names):
        for j, date in enumerate(dates):
            data[i, j] = daily_failures[date][job]

    plt.figure(figsize=(15, 10))

    plt.imshow(data, aspect="auto", cmap="YlOrRd")
    plt.colorbar(label="Number of Failures")

    plt.title("CI Failures Over Time", pad=20, size=14)
    plt.xlabel("Date", size=12)
    plt.ylabel("Jobs", size=12)

    date_strings = [d.strftime("%Y-%m-%d") for d in dates]
    plt.xticks(range(len(dates)), date_strings, rotation=45, ha="right")
    plt.yticks(range(len(job_names)), job_names)

    # Add value labels
    for i in range(len(job_names)):
        for j in range(len(dates)):
            if data[i, j] > 0:
                plt.text(j, i, int(data[i, j]), ha="center", va="center")

    plt.tight_layout()
    plt.savefig("failure_timeline.png", bbox_inches="tight", dpi=300)
    plt.close()


def main():
    job_failure_counts = defaultdict(int)
    failure_data = []  # Store all failure data for timeline

    try:
        runs = get_failed_workflow_runs()
        logger.info(f"Analyzing failures on branch '{BRANCH}'")
        logger.info(f"Going through workflow runs from newest to oldest...")

        for run in runs:
            run_id = run["id"]
            run_date = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            logger.info(f"\nChecking workflow run from {run_date}")

            failed_jobs = find_failed_jobs(run_id)
            if failed_jobs:
                failure_data.append({"date": run_date, "jobs": failed_jobs})

            for job in failed_jobs:
                failure_date = datetime.fromisoformat(
                    job["started_at"].replace("Z", "+00:00")
                )
                job_name = job["name"]
                job_failure_counts[job_name] += 1

                logger.info(f"Found failure in job '{job_name}':")
                logger.info(f"  Workflow run ID: {run_id}")
                logger.info(f"  Failed at: {failure_date}")
                logger.info(f"  URL: {job['html_url']}")

        # Print summary
        if job_failure_counts:
            logger.info("\nFailure Summary by Job:")
            for job_name, count in sorted(job_failure_counts.items()):
                logger.info(f"  {job_name}: {count} failures")
            logger.info(
                f"Total failures across all jobs: {sum(job_failure_counts.values())}"
            )

            # Create timeline visualization
            create_failure_timeline(failure_data)
        else:
            logger.info(f"No failed jobs found in the workflow on branch '{BRANCH}'.")

    except requests.HTTPError as e:
        logger.error(f"HTTP Error: {e}")
    except Exception as e:
        logger.error(f"Error: {e}")


if __name__ == "__main__":
    main()
