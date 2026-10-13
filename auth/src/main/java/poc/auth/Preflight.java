package poc.auth;

import javax.security.auth.login.AppConfigurationEntry;
import javax.security.auth.login.Configuration;

/**
 * Run by the container before Schema Registry starts, so a database outage fails startup instead of
 * the first request. Login modules are only created on the first login, so the check can't live in
 * {@link SnapshotLoginModule} itself.
 *
 * Reads the same JAAS entry (-Djava.security.auth.login.config) and does one full load.
 * Usage: Preflight [jaas-entry-name]   (default SchemaRegistryRealm). Exit code 0 = OK.
 */
public final class Preflight {
  public static void main(String[] args) {
    String realm = args.length > 0 ? args[0] : "SchemaRegistryRealm";
    try {
      AppConfigurationEntry[] entries = Configuration.getConfiguration().getAppConfigurationEntry(realm);
      if (entries == null) throw new IllegalStateException("no JAAS entry named " + realm);
      for (AppConfigurationEntry e : entries) {
        if (!e.getLoginModuleName().equals(SnapshotLoginModule.class.getName())) continue;
        UserSnapshot.Load l = UserSnapshot.load(UserSnapshot.Settings.from(e.getOptions()));
        if (l.version() == null) {
          throw new IllegalStateException("change tracking is not enabled on the auth database (version query returned NULL)");
        }
        System.out.printf("auth preflight OK: %d users, change-tracking version %d%n", l.users().size(), l.version());
        return;
      }
      throw new IllegalStateException("JAAS entry " + realm + " does not use " + SnapshotLoginModule.class.getName());
    } catch (Exception e) {
      System.err.println("auth preflight FAILED: " + e);
      System.exit(1);
    }
  }
}
