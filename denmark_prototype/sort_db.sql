CREATE TABLE h3_res3_new AS (
    SELECT * FROM h3_res3_metrics
    ORDER BY longitude, latitude
);
DROP TABLE h3_res3_metrics;
ALTER TABLE h3_res3_new RENAME TO h3_res3_metrics;

CREATE TABLE h3_res7_new AS (
    SELECT * FROM h3_res7_metrics
    ORDER BY longitude, latitude
);
DROP TABLE h3_res7_metrics;
ALTER TABLE h3_res7_new RENAME TO h3_res7_metrics;