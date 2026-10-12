# internship-radar

Checks five sources every 5 minutes and sends a phone push (via [ntfy](https://ntfy.sh)) when a new internship appears.

| Source | What it watches | Filter |
| --- | --- | --- |
| SimplifyJobs | [Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) `listings.json` | Summer 2027; software, AI/ML/data or quant; Bachelor's OK; U.S. location; title blocklist |
| SpeedyApply | [2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) README tables | No Spring/Fall/Winter titles; title blocklist |
| SAS | iCIMS careers search for "intern" | Title says intern/internship; U.S. location |
| NetApp | TalentBrew careers search for "intern" | Title says intern/internship; U.S. location |
| Lenovo | Avature careers search, newest first (full crawl every 6 hours) | Title says intern/internship; U.S. location |

Pushes from the three company sites are high priority. When the same role shows up in more than one source within 14 days, only the first one alerts.

There is no LLM or paid API in the loop. `radar.py` is plain Python using only the standard library. Filters, URLs and limits live in `config.json`. `state.json` is written by the workflow.

## How it runs

`.github/workflows/radar.yml` runs `python3 radar.py` on two triggers:

- GitHub's own schedule (`2-59/5 * * * *`). This is the backup, and it often runs late.
- A cron-job.org job that calls the `workflow_dispatch` API every 5 minutes. This is the main, on-time trigger.

The ntfy topic is stored in the `NTFY_TOPIC` Actions secret.

Health pushes:

- **Seeded**: the first run of a source records every current posting silently and sends one push.
- **Failing / recovered**: sent after 3 failed runs in a row, then at most once a day until the source works again.
- **Heartbeat**: daily after 8 AM Eastern, with the run count, per-source status, and roles alerted in the last 24 hours. "Trigger slow" means fewer than 150 runs happened.

## Commands

```sh
python3 radar.py --dry-run            # show what would alert; sends nothing, saves nothing
python3 radar.py --dry-run --source lenovo
python3 radar.py --test-push          # one sample alert to the phone
```

When running locally, the script reads the topic from `NTFY_TOPIC` or from a git-ignored `.ntfy_topic` file.

## Adding a company

Add one entry to `sources` in `config.json`. If the company's careers site runs on a platform that already has an adapter, that entry is all you need:

- `icims`: copy the SAS entry and change `url`.
- `talentbrew`: copy the NetApp entry and change `base`.
- `avature`: copy the Lenovo entry and change `url`. The sort field ids can differ per site.

For any other platform, write an `adapt_<type>` function in `radar.py` and register it in `ADAPTERS`. The function must return `{id, company, title, location, url}` postings. It must raise `SourceError` when its sanity marker is missing, so a broken page counts as a failure and never as zero jobs. The new source seeds silently on its first run.

## Pausing

1. Disable the workflow under GitHub → Actions → radar → "..." → Disable workflow, or run `gh workflow disable radar.yml`.
2. Disable the job on cron-job.org.

Re-enable both to resume. Postings that appeared while paused will alert on the next run.

The cron-job.org token is a fine-grained GitHub token that expires after one year. Renew it and update the job's `Authorization` header before then.
