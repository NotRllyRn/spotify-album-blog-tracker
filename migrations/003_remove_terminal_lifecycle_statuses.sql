-- Remove terminal lifecycle history rows now that completed releases are purged.

DELETE FROM discord_prompt
WHERE release_id IN (
    SELECT spotify_id FROM release_lifecycle
    WHERE status IN ('published', 'trashed_post', 'ignored_single', 'deleted')
);

DELETE FROM release_lifecycle
WHERE status IN ('published', 'trashed_post', 'ignored_single', 'deleted');
