-- Tables the auth service reads, plus a login for it. Run once in the platform database.
CREATE TABLE Cluster (
  ID   int IDENTITY PRIMARY KEY,
  Name varchar(64) NOT NULL UNIQUE      -- first URL path segment: c3, c17, ... ("platform" = whoever registers schemas)
);
GO
CREATE TABLE ServiceWallets (
  ID        uniqueidentifier NOT NULL PRIMARY KEY DEFAULT NEWID(),
  Name      varchar(256) NOT NULL,      -- Service name, unique only within a cluster
  Password  varchar(256) NOT NULL,
  ClusterID int NOT NULL REFERENCES Cluster(ID),
  UNIQUE (ClusterID, Name)
);
GO
CREATE LOGIN sr_auth WITH PASSWORD = 'sr-auth-secret', CHECK_POLICY = OFF;
GO
CREATE USER sr_auth FOR LOGIN sr_auth;
GO
GRANT SELECT ON Cluster TO sr_auth;
GRANT SELECT ON ServiceWallets TO sr_auth;
GO

-- Example rows
INSERT INTO Cluster (Name) VALUES ('c17'), ('c3'), ('platform');
INSERT INTO ServiceWallets (Name, Password, ClusterID) VALUES
  ('alice', 'alice-c17-secret', (SELECT ID FROM Cluster WHERE Name = 'c17')),
  ('alice', 'alice-c3-secret',  (SELECT ID FROM Cluster WHERE Name = 'c3')),
  ('admin', 'platform-secret',  (SELECT ID FROM Cluster WHERE Name = 'platform'));
GO
