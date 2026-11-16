# syntax=docker/dockerfile:1
# acl-api: FastAPI service that runs the real sr-acl-cli (see README.md).
# Self-contained for air-gapped builds: the CLI + jars (sr-acl-cli/) and the Python wheels (wheels/)
# are in this directory. The only things pulled are the two base images below; point the build args
# at your internal registry if needed:
#   docker compose build --build-arg PYTHON_IMAGE=registry.local/python:3.12-slim \
#                        --build-arg JRE_IMAGE=registry.local/eclipse-temurin:17-jre
ARG PYTHON_IMAGE=python:3.12-slim
ARG JRE_IMAGE=eclipse-temurin:17-jre

FROM ${JRE_IMAGE} AS jre

FROM ${PYTHON_IMAGE}
ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    # The CLI is a short-lived JVM: small heap, fast start. (security-plugins-run-class reads these.)
    SECURITY_PLUGINS_HEAP_OPTS="-Xmx256m" \
    SECURITY_PLUGINS_JVM_PERFORMANCE_OPTS="-XX:+UseSerialGC -XX:TieredStopAtLevel=1 -Djava.awt.headless=true"

COPY --from=jre /opt/java/openjdk /opt/java/openjdk

# sr-acl-cli as shipped in cp-schema-registry 7.9.0: bin/ scripts + the 4 jar dirs on its classpath
COPY sr-acl-cli/ /opt/sr-acl-cli/
COPY tools-log4j.properties /opt/sr-acl-cli/etc/confluent-security/tools-log4j.properties

WORKDIR /app
COPY requirements.txt .
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links /tmp/wheels -r requirements.txt \
 && rm -rf /tmp/wheels \
 && chmod 0755 /opt/sr-acl-cli/bin/* \
 && useradd --system --uid 10001 --no-create-home acl
COPY acl_api/ acl_api/

USER acl
EXPOSE 8087
# One worker: writes are serialized and reads coalesced in-process (acl_api/services/).
CMD ["uvicorn", "--factory", "acl_api.main:create_app", "--host", "0.0.0.0", "--port", "8087", "--workers", "1"]
