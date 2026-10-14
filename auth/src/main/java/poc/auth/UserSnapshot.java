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
 * {@link #users}; one background thread replaces it with a freshly built map every reloadSeconds
 * (one connection, one query that reads the whole view). If the database is unreachable the last
 * snapshot stays in use.
 */
final class UserSnapshot {
  private static final Logger LOG = Log.getLogger(UserSnapshot.class);

  record Settings(String dbUrl, String dbUserName, String dbPassword, String usersQuery, String role,
                  long reloadSeconds) {
    static Settings from(Map<String, ?> o) {
      return new Settings(
          req(o, "dbUrl"), req(o, "dbUserName"), req(o, "dbPassword"),
          opt(o, "usersQuery", "SELECT username, password FROM sr_auth_users"),
          opt(o, "role", "sr-user"),
          Long.parseLong(opt(o, "reloadSeconds", "60")));
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

  private static volatile Map<String, UserInfo> users;   // null until the first successful load
  private static volatile long loadedAtMs;
  private static Settings settings;
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
    refresher.scheduleWithFixedDelay(() -> reload(users == null ? "retry after failed initial load" : "periodic"),
        s.reloadSeconds(), s.reloadSeconds(), TimeUnit.SECONDS);
  }

  /** Current snapshot, or null if no load has succeeded yet. */
  static Map<String, UserInfo> users() {
    return users;
  }

  private static void reload(String reason) {
    try {
      Map<String, UserInfo> loaded = load(settings);
      if (loaded.isEmpty() && users != null && !users.isEmpty()) {
        // Almost certainly a broken view or bad migration, not "everyone was deleted".
        LOG.warn("auth reload ({}) returned 0 users; keeping previous snapshot of {} users", reason, users.size());
        return;
      }
      users = loaded;
      loadedAtMs = System.currentTimeMillis();
      LOG.info("auth snapshot loaded ({}): {} users", reason, loaded.size());
    } catch (Exception e) {
      LOG.warn("auth reload ({}) failed, keeping snapshot from {}s ago: {}", reason, ageSeconds(), e.toString());
    }
  }

  private static String ageSeconds() {
    return users == null ? "never" : String.valueOf((System.currentTimeMillis() - loadedAtMs) / 1000);
  }

  /** Reads the whole view: one connection, one query. */
  static Map<String, UserInfo> load(Settings s) throws SQLException {
    try (Connection c = DriverManager.getConnection(s.dbUrl(), s.dbUserName(), s.dbPassword());
         Statement st = c.createStatement()) {
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
      return Map.copyOf(m);
    }
  }

}
