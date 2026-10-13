package poc.auth;

import java.util.Map;

import javax.security.auth.Subject;
import javax.security.auth.callback.CallbackHandler;

import org.eclipse.jetty.jaas.spi.AbstractLoginModule;
import org.eclipse.jetty.jaas.spi.UserInfo;

/**
 * Jetty JAAS login module that checks Basic-auth credentials against an in-memory snapshot of the
 * auth view (see {@link UserSnapshot}). A login never touches the database.
 *
 * jaas.conf options: dbUrl, dbUserName, dbPassword (required); usersQuery, versionQuery, role,
 * changePollSeconds (default 5), fullReloadSeconds (default 300).
 */
public class SnapshotLoginModule extends AbstractLoginModule {

  @Override
  public void initialize(Subject subject, CallbackHandler callbackHandler,
                         Map<String, ?> sharedState, Map<String, ?> options) {
    super.initialize(subject, callbackHandler, sharedState, options);
    UserSnapshot.start(UserSnapshot.Settings.from(options));
  }

  /** Returning null rejects the login (unknown user); throwing also rejects it. */
  @Override
  public UserInfo getUserInfo(String userName) throws Exception {
    Map<String, UserInfo> users = UserSnapshot.users();
    if (users == null) throw new IllegalStateException("auth snapshot not loaded yet");
    return users.get(userName);
  }
}
