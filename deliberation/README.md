# deliberation

Structured public input on a matter, modeled on Polis. Residents post short statements and vote agree, disagree or pass on each other's. Clustering then shows which groups of opinion exist and where they agree.

## Pieces

| Where | Role |
|---|---|
| `deliberation/clustering.py` | The algorithm (below) |
| `database/repositories_async/deliberation.py` | Tables `deliberations` (one per matter), `deliberation_participants` (pseudonymous), `deliberation_comments` (with `mod_status`), `deliberation_votes`, `deliberation_results` (cached clustering), plus `userland.deliberation_trusted_users` |
| `server/routes/deliberation.py` | `/api/v1/deliberations`: reading state, results and by-matter lookup is public; creating, commenting and voting need a session; moderation and `compute` are admin |
| `frontend/src/routes/deliberate/[matter_id]` | The page; a deliberation is created the first time someone opens one for a matter |
| `scripts/moderate.py` | CLI: `list`, `review <deliberation_id>`, `approve <comment_id>`, `reject <comment_id>` (`run.sh moderate`) |

## Clustering

The input is a participants by comments matrix: agree 1, pass 0, disagree -1, unvoted missing. With fewer than 3 participants or 2 comments there is no result.

1. Fill missing votes with the column mean.
2. Project to 2D with PCA for the scatter plot.
3. Choose `K = min(5, 2 + floor(participants / 12))`.
4. Run K-means on the imputed vote matrix (not the projection); the 2D centers are computed afterwards for display.
5. Score consensus per comment as the mean over clusters of the Laplace-smoothed agreement, `(agrees + 1) / (votes + 2)`. Above 0.8 every group agrees; below 0.5 the groups split.

The output holds 2D positions, cluster assignments and centers, consensus per comment, and agree, disagree and seen tallies per cluster. It is cached in `deliberation_results` until the next `compute`.

## Moderation

A comment from a trusted user is approved immediately. Anyone else's waits for review. The first approval marks its author as trusted, so moderation is needed once per person, not once per comment. A rejected comment is hidden.

A local-first version is sketched in [docs/DELIBERATION_LOCAL_FIRST.md](../docs/DELIBERATION_LOCAL_FIRST.md).
