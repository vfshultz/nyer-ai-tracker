# New Yorker A.I. coverage tracker

Counts New Yorker articles about A.I., by month and year, with growth rates, split into:

- **Online**: articles the magazine tags "Artificial Intelligence" or "Artificial Intelligence (A.I.)".
- **Print**: print-magazine pieces (which carry no topic tags), found through each weekly issue's
  table of contents. A piece counts if its headline mentions A.I. or its text mentions A.I.
  at least 5 times ("A.I.", "artificial intelligence", "chatbot", "ChatGPT",
  "large language model", "machine learning").

## Run it

```
pip install -r requirements.txt
python nyer_ai_tracker.py
```

Default range: 2023-01-01 to today. The first run takes roughly 2 hours (about 5,000 pages,
1.5 s apart). Progress is saved to `cache.json`: Ctrl-C is safe, and rerunning resumes.
Later runs only fetch new material.

Results in `output/` (CSVs open cleanly in Excel):

- `articles.csv`: every counted piece, with channel, why it counted, and A.I. mention count
- `monthly_counts.csv`: online, print, total per month, plus a 3-month average
- `annual_counts.csv`: yearly totals with year-over-year change
- `ai_articles_by_month.png`: stacked chart

## Options

| Flag | What it does |
|---|---|
| `--start 2024-01-01` | Different window |
| `--analyze-only` | Recompute stats from the cache without fetching |
| `--mag-threshold 8` | Stricter (or looser) rule for print pieces; use with `--analyze-only` |
| `--no-magazine` | Online tags only |
| `--discover URL` | Diagnose one article: date, tags, word count, A.I. mentions |
| `--strict` | Only count online articles whose own page shows an A.I. tag |

## What it measures

Online counts reflect the editors' tagging, so they include pieces where A.I. is a side topic
and can miss untagged ones. Print counts use a text rule, so they depend on the threshold.
Check `articles.csv` to judge whether the rule fits your purpose.

## Automatic monthly updates (optional)

Push this folder to a GitHub repo. `.github/workflows/update.yml` reruns the tracker on the 1st
of each month and commits the results; you can also start it from the repo's **Actions** tab.
If the site blocks GitHub's servers, the job will fail; run it locally instead.
