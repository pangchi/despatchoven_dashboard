-- Run as the postgres superuser:  sudo -u postgres psql -f deploy/setup_db.sql
-- Change the password, then put the same value in .env (DB_PASSWORD).
CREATE ROLE protocol3 LOGIN PASSWORD 'change-me';
CREATE DATABASE protocol3 OWNER protocol3;
-- Tables and daily partitions are created automatically by the app on start.
