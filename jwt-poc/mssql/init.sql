-- Credential table for the JWT design, in the repo's existing `platform` database. Idempotent.
-- Rows are upserted by scripts/up.sh with bcrypt hashes. The auth service reads it as `sr_jwt_auth`.
USE platform;
GO
IF OBJECT_ID('sr_services') IS NULL
  CREATE TABLE sr_services (
    name          varchar(256) PRIMARY KEY,
    password_hash varchar(60)  NOT NULL   -- bcrypt, $2b$...
  );
GO
IF SUSER_ID('sr_jwt_auth') IS NULL
  CREATE LOGIN sr_jwt_auth WITH PASSWORD = 'sr-jwt-auth-secret', CHECK_POLICY = OFF, DEFAULT_DATABASE = platform;
GO
IF USER_ID('sr_jwt_auth') IS NULL
  CREATE USER sr_jwt_auth FOR LOGIN sr_jwt_auth;
GO
GRANT SELECT ON sr_services TO sr_jwt_auth;
GO
