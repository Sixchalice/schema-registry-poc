#!/usr/bin/env bash
# Restores sr-acl-cli/ (scripts + the 4 jar dirs on its classpath) from the cp-schema-registry image.
# Needed after a fresh clone, since the jars aren't in git. Run on a machine that has (or can pull) the image.
set -euo pipefail
cd "$(dirname "$0")"
image=${1:-confluentinc/cp-schema-registry:7.9.0}
cid=$(docker create "$image")
trap 'docker rm "$cid" >/dev/null' EXIT
mkdir -p sr-acl-cli/bin sr-acl-cli/share/java/confluent-security
docker cp "$cid:/usr/bin/sr-acl-cli" sr-acl-cli/bin/
docker cp "$cid:/usr/bin/security-plugins-run-class" sr-acl-cli/bin/
docker cp "$cid:/usr/share/java/confluent-security/schema-registry" sr-acl-cli/share/java/confluent-security/
# portable shebang (the image has #!/usr/bin/bash)
sed -i.bak '1s|^#!/usr/bin/bash$|#!/usr/bin/env bash|' sr-acl-cli/bin/sr-acl-cli sr-acl-cli/bin/security-plugins-run-class
rm -f sr-acl-cli/bin/*.bak
for dir in confluent-common rest-utils schema-registry; do
  docker cp "$cid:/usr/share/java/$dir" sr-acl-cli/share/java/
done
echo "sr-acl-cli/: $(find sr-acl-cli -name '*.jar' | wc -l | tr -d ' ') jars, $(du -sh sr-acl-cli | cut -f1)"
