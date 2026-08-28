DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catalogreader') THEN
        CREATE ROLE catalogreader NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA catalog TO catalogreader;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO catalogreader;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalog GRANT SELECT ON TABLES TO catalogreader;
