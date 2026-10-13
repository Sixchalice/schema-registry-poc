-- "Platform database" for the POC (SQL Server 2022). Run once by scripts/up.sh via sqlcmd.
-- Read by two login modules:
--   sr_auth -> poc.auth.SnapshotLoginModule (schema-registry, schema-registry-noplugin): whole view, polled
--   sr_jdbc -> Jetty's JDBCLoginModule (schema-registry-jdbc, test 8 baseline only): two queries per login
CREATE DATABASE platform;
GO
-- Change Tracking lets the snapshot module detect any insert/update/delete with one cheap query:
-- SELECT CHANGE_TRACKING_CURRENT_VERSION()
ALTER DATABASE platform SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON);
GO
USE platform;
GO
-- Simplified model of the platform's own tables.
CREATE TABLE clusters (
  cluster_id varchar(64) PRIMARY KEY
);
-- A tenant "Service" = SCRAM user on a given cluster (+ group.id, topic permissions, ...).
-- scram_password must be readable by Jetty: plaintext, or Jetty's "MD5:<hex>" / "CRYPT:<des-crypt>" forms.
CREATE TABLE services (
  cluster_id     varchar(64)  NOT NULL REFERENCES clusters,
  scram_user     varchar(128) NOT NULL,
  scram_password varchar(256) NOT NULL,
  PRIMARY KEY (cluster_id, scram_user)
);
CREATE TABLE platform_principals (
  username varchar(256) PRIMARY KEY,
  password varchar(256) NOT NULL
);
-- Change Tracking is enabled on the base tables (it can't be enabled on a view).
ALTER TABLE services ENABLE CHANGE_TRACKING;
ALTER TABLE platform_principals ENABLE CHANGE_TRACKING;
GO
-- The snapshot module runs:   SELECT username, password FROM sr_auth_users        (whole view)
-- Jetty's JDBCLoginModule runs: select password from sr_auth_users where username = ?
--                               select role     from sr_auth_roles where username = ?
CREATE VIEW sr_auth_users AS
  SELECT cluster_id + '.' + scram_user AS username, scram_password AS password FROM services
  UNION ALL
  SELECT username, password FROM platform_principals;
GO
CREATE VIEW sr_auth_roles AS
  SELECT username, CAST('sr-user' AS varchar(16)) AS role FROM sr_auth_users;
GO
-- Least-privilege logins, one per module so their database work can be counted separately.
-- CHECK_POLICY=OFF only so the POC passwords can stay simple.
CREATE LOGIN sr_auth WITH PASSWORD = 'sr-auth-secret', CHECK_POLICY = OFF, DEFAULT_DATABASE = platform;
CREATE USER sr_auth FOR LOGIN sr_auth;
GRANT SELECT ON sr_auth_users TO sr_auth;
CREATE LOGIN sr_jdbc WITH PASSWORD = 'sr-jdbc-secret', CHECK_POLICY = OFF, DEFAULT_DATABASE = platform;
CREATE USER sr_jdbc FOR LOGIN sr_jdbc;
GRANT SELECT ON sr_auth_users TO sr_jdbc;
GRANT SELECT ON sr_auth_roles TO sr_jdbc;
GO
-- Measurement only (tests 8 and 10): count logins (= new connections) and queries per login.
-- Plain statements arrive as SQL batches, prepared statements as RPCs. Read with
-- sys.dm_xe_session_targets (ms_stats in scripts/test.sh). STARTUP_STATE survives restarts.
USE master;
GO
CREATE EVENT SESSION sr_auth_activity ON SERVER
  ADD EVENT sqlserver.login (WHERE sqlserver.username = N'sr_auth'),
  ADD EVENT sqlserver.sql_batch_completed (WHERE sqlserver.username = N'sr_auth'),
  ADD EVENT sqlserver.rpc_completed (WHERE sqlserver.username = N'sr_auth')
  ADD TARGET package0.event_counter
  WITH (STARTUP_STATE = ON);
ALTER EVENT SESSION sr_auth_activity ON SERVER STATE = START;
CREATE EVENT SESSION sr_jdbc_activity ON SERVER
  ADD EVENT sqlserver.login (WHERE sqlserver.username = N'sr_jdbc'),
  ADD EVENT sqlserver.sql_batch_completed (WHERE sqlserver.username = N'sr_jdbc'),
  ADD EVENT sqlserver.rpc_completed (WHERE sqlserver.username = N'sr_jdbc')
  ADD TARGET package0.event_counter
  WITH (STARTUP_STATE = ON);
ALTER EVENT SESSION sr_jdbc_activity ON SERVER STATE = START;
GO
