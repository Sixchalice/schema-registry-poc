# cp-schema-registry 7.9.0 already ships the Security Plugin
# (/usr/share/java/confluent-security/schema-registry) and Jetty 9.4.57 incl. jetty-jaas.
# The only missing piece is a PostgreSQL JDBC driver for Jetty's JDBCLoginModule.
FROM confluentinc/cp-schema-registry:7.9.0
ADD --chmod=644 https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar \
    /usr/share/java/schema-registry/postgresql-42.7.4.jar
