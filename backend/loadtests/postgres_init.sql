CREATE EXTENSION IF NOT EXISTS postgis;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'travelapp') THEN
        CREATE ROLE travelapp LOGIN PASSWORD 'travelapp';
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS catalog AUTHORIZATION catalogowner;
CREATE TABLE IF NOT EXISTS catalog.build (buildid INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS catalog.location (
    locationid UUID PRIMARY KEY,
    canonicalname TEXT NOT NULL,
    importance DOUBLE PRECISION NOT NULL DEFAULT 0,
    pointgeom geometry(Point, 4326)
);
CREATE TABLE IF NOT EXISTS catalog.region (
    regionid UUID PRIMARY KEY,
    name TEXT NOT NULL,
    adcode TEXT NOT NULL,
    level INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog.locationname (
    locationid UUID NOT NULL,
    normalizedname TEXT NOT NULL,
    nametype TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS catalog.boundary (
    regionid UUID PRIMARY KEY,
    centergeom geometry(Point, 4326)
);
CREATE TABLE IF NOT EXISTS catalog.poi (
    locationid UUID PRIMARY KEY,
    address TEXT,
    category TEXT NOT NULL,
    typename TEXT,
    imageurl TEXT
);

GRANT USAGE ON SCHEMA catalog TO travelapp;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO travelapp;
