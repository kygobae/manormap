# Reviews

Your review verdicts (exported from `review.html` as `verdicts.csv`) are personal, so everything in this folder except this README is git-ignored.

A convenient layout is `reviews/<powiat>/verdicts.csv` or `reviews/poland/verdicts.csv`. To carry verdicts into a new run:

```
gutshaus run --powiat 3209 --verdicts reviews/3209/verdicts.csv
gutshaus build --verdicts reviews/poland/verdicts.csv
```

To keep a history of your reviews, put them in a separate private repository.
