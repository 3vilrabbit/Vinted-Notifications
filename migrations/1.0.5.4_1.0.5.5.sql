BEGIN TRANSACTION;

-- Per-query banwords, merged with the global list when a title is checked
ALTER TABLE queries ADD COLUMN banwords TEXT;

UPDATE parameters
SET value = '1.0.5.5'
WHERE key = 'version';

COMMIT;
