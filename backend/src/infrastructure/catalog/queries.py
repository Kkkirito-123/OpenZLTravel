"""PostGIS 地点目录的只读 SQL。"""

CITY_SQL = """
SELECT
    r.name,
    r.adcode,
    ST_Y(COALESCE(l.pointgeom, b.centergeom)) AS latitude,
    ST_X(COALESCE(l.pointgeom, b.centergeom)) AS longitude
FROM catalog.locationname AS n
JOIN catalog.region AS r ON r.regionid = n.locationid
JOIN catalog.location AS l ON l.locationid = r.regionid
LEFT JOIN catalog.boundary AS b ON b.regionid = r.regionid
WHERE n.normalizedname = %s
  AND r.level BETWEEN 1 AND 3
  AND COALESCE(l.pointgeom, b.centergeom) IS NOT NULL
ORDER BY
    (r.status = 'current') DESC,
    CASE r.level WHEN 2 THEN 3 WHEN 1 THEN 2 WHEN 3 THEN 1 ELSE 0 END DESC,
    (n.nametype = 'official') DESC,
    n.priority DESC,
    l.importance DESC,
    r.regionid
LIMIT 1
"""

PLACE_SQL = """
WITH matched AS (
    SELECT
        l.locationid,
        l.canonicalname,
        l.pointgeom,
        l.importance,
        p.address,
        p.category,
        p.typename,
        p.imageurl,
        rm.regionid,
        n.nametype,
        n.priority
    FROM catalog.locationname AS n
    JOIN catalog.location AS l ON l.locationid = n.locationid
    JOIN catalog.poi AS p ON p.locationid = l.locationid
    JOIN catalog.regionmatch AS rm ON rm.locationid = l.locationid
    WHERE n.normalizedname = %s
      AND p.category = 'attraction'
      AND l.pointgeom IS NOT NULL
      AND rm.regionid IS NOT NULL
)
SELECT
    matched.locationid,
    matched.canonicalname,
    matched.address,
    matched.category,
    matched.typename,
    matched.imageurl,
    ST_Y(matched.pointgeom) AS latitude,
    ST_X(matched.pointgeom) AS longitude,
    city.name AS cityname,
    city.adcode AS cityadcode,
    ST_Y(COALESCE(citylocation.pointgeom, boundary.centergeom)) AS citylatitude,
    ST_X(COALESCE(citylocation.pointgeom, boundary.centergeom)) AS citylongitude
FROM matched
JOIN catalog.region AS matchedregion ON matchedregion.regionid = matched.regionid
JOIN catalog.region AS city ON matchedregion.path <@ city.path AND city.level = 2
JOIN catalog.location AS citylocation ON citylocation.locationid = city.regionid
LEFT JOIN catalog.boundary AS boundary ON boundary.regionid = city.regionid
WHERE COALESCE(citylocation.pointgeom, boundary.centergeom) IS NOT NULL
ORDER BY
    (city.status = 'current') DESC,
    (matched.nametype = 'official') DESC,
    matched.priority DESC,
    matched.importance DESC,
    matched.locationid
LIMIT 1
"""

POI_SQL = """
WITH center AS (
    SELECT ST_SetSRID(ST_MakePoint(%s, %s), 4326) AS pointgeom
), candidates AS (
    SELECT
        l.locationid,
        l.canonicalname,
        l.importance,
        p.address,
        p.category,
        p.typename,
        p.imageurl,
        ST_Y(l.pointgeom) AS latitude,
        ST_X(l.pointgeom) AS longitude,
        ST_Distance(l.pointgeom::geography, center.pointgeom::geography) AS distance
    FROM catalog.poi AS p
    JOIN catalog.location AS l ON l.locationid = p.locationid
    CROSS JOIN center
    WHERE p.category IN ('attraction', 'restaurant', 'hotel')
      AND l.pointgeom && ST_Expand(center.pointgeom, 1.0)
      AND ST_DWithin(l.pointgeom::geography, center.pointgeom::geography, 80000)
), ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY category
        ORDER BY distance, importance DESC, locationid
    ) AS categoryrank
    FROM candidates
)
SELECT
    locationid,
    canonicalname,
    address,
    category,
    typename,
    imageurl,
    latitude,
    longitude
FROM ranked
WHERE categoryrank <= CASE category
    WHEN 'attraction' THEN 24
    WHEN 'restaurant' THEN 12
    ELSE 15
END
ORDER BY category, categoryrank
"""

DESTINATION_SQL = """
WITH requested_region AS (
    SELECT r.regionid, r.path
    FROM catalog.locationname AS n
    JOIN catalog.region AS r ON r.regionid = n.locationid
    WHERE n.normalizedname = %s
      AND r.level BETWEEN 0 AND 2
    ORDER BY
      (r.status = 'current') DESC,
      (n.nametype = 'official') DESC,
      n.priority DESC
    LIMIT 1
), origin AS (
    SELECT COALESCE(l.pointgeom, b.centergeom) AS pointgeom
    FROM catalog.locationname AS n
    JOIN catalog.region AS r ON r.regionid = n.locationid
    JOIN catalog.location AS l ON l.locationid = r.regionid
    LEFT JOIN catalog.boundary AS b ON b.regionid = r.regionid
    WHERE n.normalizedname = %s
      AND r.level BETWEEN 1 AND 3
      AND COALESCE(l.pointgeom, b.centergeom) IS NOT NULL
    ORDER BY CASE r.level WHEN 2 THEN 3 WHEN 1 THEN 2 ELSE 1 END DESC
    LIMIT 1
), cities AS (
    SELECT
        r.regionid,
        r.path,
        r.name,
        r.adcode,
        COALESCE(l.pointgeom, b.centergeom) AS pointgeom
    FROM requested_region AS requested
    JOIN catalog.region AS r ON r.path <@ requested.path AND r.level = 2
    JOIN catalog.location AS l ON l.locationid = r.regionid
    LEFT JOIN catalog.boundary AS b ON b.regionid = r.regionid
    WHERE r.status = 'current'
      AND COALESCE(l.pointgeom, b.centergeom) IS NOT NULL
)
SELECT
    city.regionid,
    city.name,
    city.adcode,
    ST_Y(city.pointgeom) AS latitude,
    ST_X(city.pointgeom) AS longitude,
    ST_Distance(city.pointgeom::geography, origin.pointgeom::geography) / 1000 AS distance_km,
    COUNT(p.locationid) FILTER (WHERE p.category = 'attraction') AS attraction_count,
    COUNT(p.locationid) FILTER (WHERE p.category = 'restaurant') AS restaurant_count,
    COUNT(p.locationid) FILTER (WHERE p.category = 'hotel') AS hotel_count,
    ARRAY_REMOVE(ARRAY_AGG(DISTINCT p.typename), NULL) AS type_names
FROM cities AS city
CROSS JOIN origin
LEFT JOIN catalog.region AS child_region ON child_region.path <@ city.path
LEFT JOIN catalog.regionmatch AS match ON match.regionid = child_region.regionid
LEFT JOIN catalog.poi AS p ON p.locationid = match.locationid
GROUP BY city.regionid, city.name, city.adcode, city.pointgeom, origin.pointgeom
HAVING COUNT(p.locationid) FILTER (WHERE p.category = 'attraction') > 0
ORDER BY attraction_count DESC, restaurant_count DESC, hotel_count DESC, city.name
LIMIT 50
"""
