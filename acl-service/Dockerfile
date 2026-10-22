# sr-acl-service: sr-acl-cli as a REST service (see src/main/java/poc/acl/AclService.java).
# The Security Plugin classes it calls are not on Maven Central, so it compiles against the jars of
# the cp-schema-registry image itself and runs on that image with the same classpath as sr-acl-cli.
FROM confluentinc/cp-schema-registry:7.9.0 AS sr

FROM maven:3.9.9-eclipse-temurin-17 AS build
COPY --from=sr /usr/share/java /usr/share/java
COPY src /src
RUN javac --release 17 -d /out \
      -cp "/usr/share/java/confluent-security/schema-registry/*:/usr/share/java/confluent-common/*:/usr/share/java/rest-utils/*:/usr/share/java/schema-registry/*" \
      $(find /src -name '*.java') \
 && jar cf /sr-acl-service.jar -C /out .

FROM confluentinc/cp-schema-registry:7.9.0
COPY --from=build /sr-acl-service.jar /opt/sr-acl-service/sr-acl-service.jar
EXPOSE 8085
# Same classpath order as /usr/bin/sr-acl-cli
ENTRYPOINT ["bash", "-c", "exec java $ACL_SERVICE_OPTS -cp '/opt/sr-acl-service/*:/usr/share/java/confluent-security/schema-registry/*:/usr/share/java/confluent-common/*:/usr/share/java/rest-utils/*:/usr/share/java/schema-registry/*' poc.acl.AclService \"$0\"", "/etc/schema-registry/poc/sr.properties"]
