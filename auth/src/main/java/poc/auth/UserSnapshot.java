package poc.auth;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import org.eclipse.jetty.jaas.spi.UserInfo;
import org.eclipse.jetty.util.log.Log;
import org.eclipse.jetty.util.log.Logger;
import org.eclipse.jetty.util.security.Credential;

/**
 * Process-wide, in-memory copy of the auth view.
 *
 * JAAS creates a new login module for every login, so all state here is static. Requests only read
 * {@link #users}; one background thread replaces it with a freshly built map:
 *  - every changePollSeconds it reads SQL Server's change-tracking version (one number, no rows)
 *    and reloads everything if it moved;
 *  - every fullReloadSeconds it reloads everything regardless.
 * If the database is unreachable the last snapshot stays in use.
 */
final class UserSnapshot {
  private static final Logger LOG = Log.getLogger(UserSnapshot.class);

  record Settings(String dbUrl, String dbUserName, String dbPassword, String usersQuery, String versionQuery,
                  String role, long changePollSeconds, long fullReloadSeconds) {
    static Settings from(Map<String, ?> o) {
      return new Settings(
          req(o, "dbUrl"), req(o, "dbUserName"), req(o, "dbPassword"),
          opt(o, "usersQuery", "SELECT username, password FROM sr_auth_users"),
          opt(o, "versionQuery", "SELECT CHANGE_TRACKING_CURRENT_VERSION()"),
          opt(o, "role", "sr-user"),
          Long.parseLong(opt(o, "changePollSeconds", "5")),
          Long.parseLong(opt(o, "fullReloadSeconds", "300")));
    }
    private static String req(Map<String, ?> o, String k) {
      Object v = o.get(k);
      if (v == null) throw new IllegalArgumentException("missing JAAS option " + k);
      return v.toString();
    }
    private static String opt(Map<String, ?> o, String k, String def) {
      Object v = o.get(k);
      return v == null ? def : v.toString();
    }
  }

  /** One read of the database: change-tracking version (null if tracking is off) and all users. */
  record Load(Long version, Map<String, UserInfo> users) {}

  private static volatile Map<String, UserInfo> users;   // null until the first successful load
  private static volatile long loadedAtMs;
  private static Settings settings;
  private static Long lastVersion;
  private static volatile ScheduledExecutorService refresher;

  private UserSnapshot() {}

  /** Called on every login; only the first call does anything. */
  static void start(Settings s) {
    if (refresher == null) {
      synchronized (UserSnapshot.class) {
        if (refresher == null) startOnce(s);
      }
    }
  }

  private static void startOnce(Settings s) {
    settings = s;
    reload("initial");
    refresher = Executors.newSingleThreadScheduledExecutor(r -> {
      Thread t = new Thread(r, "sr-auth-snapshot");
      t.setDaemon(true);
      return t;
    });
    refresher.scheduleWithFixedDelay(UserSnapshot::tick, s.changePollSeconds(), s.changePollSeconds(), TimeUnit.SECONDS);
  }

  /** Current snapshot, or null if no load has succeeded yet. */
  static Map<String, UserInfo> users() {
    return users;
  }

  private static void tick() {
    try {
      if (users == null) {
        reload("retry after failed initial load");
      } else if (System.currentTimeMillis() - loadedAtMs >= settings.fullReloadSeconds() * 1000) {
        reload("periodic");
      } else {
        Long v = currentVersion(settings);
        if (v != null && !v.equals(lastVersion)) reload("change-tracking version " + lastVersion + " -> " + v);
      }
    } catch (Exception e) {
      // Database down or query failing: keep serving the last snapshot.
      LOG.warn("auth DB check failed, keeping snapshot from {}s ago: {}", ageSeconds(), e.toString());
    }
  }

  private static void reload(String reason) {
    try {
      Load l = load(settings);
      if (l.users().isEmpty() && users != null && !users.isEmpty()) {
        // Almost certainly a broken view or bad migration, not "everyone was deleted".
        LOG.warn("auth reload ({}) returned 0 users; keeping previous snapshot of {} users", reason, users.size());
        return;
      }
      users = l.users();
      lastVersion = l.version();
      loadedAtMs = System.currentTimeMillis();
      LOG.info("auth snapshot loaded ({}): {} users, change-tracking version {}", reason, l.users().size(), l.version());
    } catch (Exception e) {
      LOG.warn("auth reload ({}) failed, keeping snapshot from {}s ago: {}", reason, ageSeconds(), e.toString());
    }
  }

  private static String ageSeconds() {
    return users == null ? "never" : String.valueOf((System.currentTimeMillis() - loadedAtMs) / 1000);
  }

  /** Reads the version first, so a change made during the load is picked up by the next poll. */
  static Load load(Settings s) throws SQLException {
    try (Connection c = DriverManager.getConnection(s.dbUrl(), s.dbUserName(), s.dbPassword());
         Statement st = c.createStatement()) {
      Long version = version(st, s);
      Map<String, UserInfo> m = new HashMap<>();
      List<String> roles = List.of(s.role());
      try (ResultSet rs = st.executeQuery(s.usersQuery())) {
        while (rs.next()) {
          String name = rs.getString(1);
          // Credential understands Jetty's forms: plaintext, "MD5:<hex>", "CRYPT:<des-crypt>"
          if (m.put(name, new UserInfo(name, Credential.getCredential(rs.getString(2)), roles)) != null) {
            LOG.warn("duplicate username {} in auth view; last row wins", name);
          }
        }
      }
      return new Load(version, Map.copyOf(m));
    }
  }

  static Long currentVersion(Settings s) throws SQLException {
    try (Connection c = DriverManager.getConnection(s.dbUrl(), s.dbUserName(), s.dbPassword());
         Statement st = c.createStatement()) {
      return version(st, s);
    }
  }

  private static Long version(Statement st, Settings s) throws SQLException {
    try (ResultSet rs = st.executeQuery(s.versionQuery())) {
      if (!rs.next()) return null;
      long v = rs.getLong(1);
      return rs.wasNull() ? null : v;
    }
  }
}
