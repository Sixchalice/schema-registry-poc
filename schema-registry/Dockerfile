# cp-schema-registry 7.9.0 already ships the Security Plugin
# (/usr/share/java/confluent-security/schema-registry) and Jetty 9.4.57 incl. jetty-jaas.
# The only missing piece is the SQL Server JDBC driver, used by poc.auth.SnapshotLoginModule
# and by Jetty's JDBCLoginModule (test 8 baseline).
FROM confluentinc/cp-schema-registry:7.9.0
ADD --chmod=644 https://repo1.maven.org/maven2/com/microsoft/sqlserver/mssql-jdbc/12.8.1.jre11/mssql-jdbc-12.8.1.jre11.jar \
    /usr/share/java/schema-registry/mssql-jdbc-12.8.1.jre11.jar
