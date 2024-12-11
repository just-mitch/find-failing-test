import json
import logging
import os
import re
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
LOGS_DIR = CACHE_DIR / "logs"  # New directory for storing log files
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
        logger.info(f"Using cached jobs for run {run_id}")
        return jobs_cache[str(run_id)]

    # If not in cache, fetch from API with pagination
    all_jobs = []
    page = 1
    per_page = 100  # Maximum allowed by GitHub API

    while True:
        url = f"{BASE_URL}/repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs"
        params = {"page": page, "per_page": per_page}
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()

        jobs = response.json().get("jobs", [])
        if not jobs:
            break

        all_jobs.extend(jobs)

        # Check if we've received all jobs
        if len(jobs) < per_page:
            break

        page += 1

    failed_jobs = [job for job in all_jobs if job["conclusion"] == "failure"]

    # Fetch logs for yarn-project-test jobs
    for job in failed_jobs:
        if job["name"] == "yarn-project-test":
            try:
                get_job_logs(job["id"])
            except Exception as e:
                logger.warning(f"Failed to fetch logs for job {job['id']}: {e}")

    # Update cache with new jobs
    jobs_cache[str(run_id)] = failed_jobs
    save_cache(jobs_cache, JOBS_CACHE_FILE)

    return failed_jobs


def get_job_logs(job_id):
    """Fetch and cache logs for a specific job."""
    # Create logs directory if it doesn't exist
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_file = LOGS_DIR / f"job_{job_id}.txt"

    # Check if we have a recent log file (less than CACHE_EXPIRY old)
    if log_file.exists() and time() - log_file.stat().st_mtime < CACHE_EXPIRY:
        logger.info(f"Using cached log file for job {job_id}")
        with open(log_file, "r") as f:
            return f.read()

    # If not in cache or expired, fetch from API
    logger.info(f"Fetching logs for job {job_id}...")

    # Get the download URL
    logs_url = f"{BASE_URL}/repos/{OWNER}/{REPO}/actions/jobs/{job_id}/logs"
    response = requests.get(logs_url, headers=HEADERS, allow_redirects=False)
    response.raise_for_status()

    # Follow the redirect to get the actual logs
    if response.status_code == 302:
        download_url = response.headers["Location"]
        log_response = requests.get(download_url)
        log_response.raise_for_status()

        # Save logs to file
        with open(log_file, "w") as f:
            f.write(log_response.text)

        return log_response.text
    else:
        raise Exception("Expected redirect response for logs URL")


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


def parse_test_failures(log_content):
    """Parse log content to find test failures and extract test information."""
    failures = []
    for line in log_content.splitlines():
        if " FAIL " in line:
            # logger.info(f"Found test failure: {line}")
            try:
                # Extract package name (e.g., "@aztec/prover-node")
                # use regex to find the package name
                package_match = re.search(r"\[(@aztec/[a-zA-Z0-9-]+)\]", line)
                package = package_match.group(1) if package_match else None

                # Extract test file name (e.g., "prover-node.test.ts")
                test_file_match = re.search(r"[a-zA-Z0-9/]+\.test\.ts", line)
                test_file = test_file_match.group(0) if test_file_match else None

                if package and test_file:
                    failures.append(f"{package} ({test_file})")
            except Exception as e:
                logger.warning(f"Failed to parse test failure line: {e}")
                continue

    return list(set(failures))


def main():
    job_failure_counts = defaultdict(int)
    test_failure_counts = defaultdict(int)
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

        # Parse all log files for test failures
        logger.info("\nAnalyzing test failures from logs...")
        for log_file in LOGS_DIR.glob("job_*.txt"):
            try:
                with open(log_file, "r") as f:
                    log_content = f.read()
                    failures = parse_test_failures(log_content)
                    for failure in failures:
                        test_failure_counts[failure] += 1
            except Exception as e:
                logger.warning(f"Failed to process log file {log_file}: {e}")

        logger.info(f"Test failures: {test_failure_counts}")
        # Print summary
        if job_failure_counts:
            logger.info("\nFailure Summary by Job:")
            for job_name, count in sorted(job_failure_counts.items()):
                logger.info(f"  {job_name}: {count} failures")
            logger.info(
                f"Total failures across all jobs: {sum(job_failure_counts.values())}"
            )

            # Print test failure summary
            if test_failure_counts:
                logger.info("Yarn Project Test Failures:")
                for failure, count in sorted(test_failure_counts.items()):
                    logger.info(f"{failure}: {count} failures")

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
