-- "Platform database" for the POC. Runs once on first container start.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Simplified model of the platform's own tables.
CREATE TABLE clusters (
  cluster_id text PRIMARY KEY
);

-- A tenant "Service" = SCRAM user on a given cluster (+ group.id, topic permissions, ...).
-- scram_password must be readable by Jetty: plaintext, or Jetty's "MD5:<hex>" / "CRYPT:<des-crypt>" forms.
CREATE TABLE services (
  cluster_id     text NOT NULL REFERENCES clusters,
  scram_user     text NOT NULL,
  scram_password text NOT NULL,
  PRIMARY KEY (cluster_id, scram_user)
);

CREATE TABLE platform_principals (
  username text PRIMARY KEY,
  password text NOT NULL
);

-- What Jetty's JDBCLoginModule reads. It runs:
--   select password from sr_auth_users where username = ?
--   select role     from sr_auth_roles where username = ?
CREATE VIEW sr_auth_users AS
  SELECT cluster_id || '.' || scram_user AS username, scram_password AS password FROM services
  UNION ALL
  SELECT username, password FROM platform_principals;

CREATE VIEW sr_auth_roles AS
  SELECT username, 'sr-user'::text AS role FROM sr_auth_users;

-- Least-privilege DB login used by Schema Registry.
CREATE ROLE sr_auth LOGIN PASSWORD 'sr-auth-secret';
GRANT SELECT ON sr_auth_users, sr_auth_roles TO sr_auth;
-- Lets the test scripts read pg_stat_statements for all users.
GRANT pg_read_all_stats TO platform;
