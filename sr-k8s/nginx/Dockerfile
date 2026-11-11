FROM nginx:1.27.5-alpine
# The image renders /etc/nginx/templates/*.template into /etc/nginx/conf.d/ with envsubst at startup
COPY default.conf.template /etc/nginx/templates/default.conf.template
ENV AUTH_SERVICE=auth-service:8080 \
    SCHEMA_REGISTRY=schema-registry:8081
EXPOSE 8080
