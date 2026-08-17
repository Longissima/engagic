-- Promote the latest winning run's semantic signals to the chunk root. The
-- processor and standing SQL read this compact current verdict without having
-- to repeat a JSON array scan.
WITH winners AS (
    SELECT q.id,
           run.value AS winner
    FROM queue q
    CROSS JOIN LATERAL (
        SELECT value
        FROM jsonb_array_elements(
            COALESCE(q.processing_metadata->'chunk'->'runs', '[]'::jsonb)
        ) WITH ORDINALITY AS candidate(value, ordinality)
        WHERE candidate.value->>'winning_rung' IS NOT NULL
        ORDER BY ordinality DESC
        LIMIT 1
    ) AS run
    WHERE q.job_type = 'meeting'
      AND q.processing_metadata->'chunk' IS NOT NULL
      AND q.processing_metadata->'chunk'->'quality' IS NULL
)
UPDATE queue q
SET processing_metadata = jsonb_set(
        q.processing_metadata,
        '{chunk}',
        (q.processing_metadata->'chunk') || jsonb_build_object(
            'quality', winners.winner->'quality',
            'morphology', winners.winner->'morphology',
            'profile', winners.winner->'profile'
        ),
        true
    ),
    updated_at = CURRENT_TIMESTAMP
FROM winners
WHERE q.id = winners.id;
