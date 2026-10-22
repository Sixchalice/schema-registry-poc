package poc.acl;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.confluent.kafka.schemaregistry.security.authorizer.SchemaRegistryResourceOperation;
import io.confluent.kafka.schemaregistry.security.config.SecureSchemaRegistryConfig;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.concurrent.Executors;

/**
 * REST front end for {@link AclStore}: what sr-acl-cli --list/--add/--remove does, over HTTP.
 *
 * <pre>
 *   GET    /health                     no auth; 200 once the ACL topic has been read
 *   GET    /operations                 operations accepted in requests
 *   GET    /acls[?principal=&subject=] current ACLs
 *   POST   /acls                       add    (body below), like --add
 *   DELETE /acls                       remove (body below), like --remove
 *
 *   {"principals": ["c17.svc-a"], "subjects": ["orders-value"], "topics": ["orders"],
 *    "operations": ["SUBJECT_READ", "SUBJECT_WRITE"]}
 * </pre>
 *
 * Validation follows SchemaRegistryAclCommandOptions: a topic T means subjects T-key and T-value,
 * "*" in principals/subjects/operations means all, subject operations need a subject or topic, and
 * global-only requests must not name one. The service itself is protected by HTTP Basic with one
 * admin credential (ACL_SERVICE_USER / ACL_SERVICE_PASSWORD): whoever can call it can grant anything.
 *
 * <p>Usage: {@code AclService <schema-registry.properties>} (the effective SR config, for the
 * Kafka connection and the ACL topic name).
 */
public final class AclService {

  private static final ObjectMapper JSON = new ObjectMapper()
      .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, true);

  record AclRequest(List<String> principals, List<String> subjects, List<String> topics, List<String> operations) {}

  /** A 4xx answer with a message for the caller. */
  static final class BadRequest extends Exception {
    final int status;
    BadRequest(int status, String message) { super(message); this.status = status; }
  }

  private final AclStore store;
  private final byte[] expectedAuth;

  private AclService(AclStore store, String user, String password) {
    this.store = store;
    this.expectedAuth = ("Basic " + Base64.getEncoder().encodeToString(
        (user + ":" + password).getBytes(StandardCharsets.UTF_8))).getBytes(StandardCharsets.UTF_8);
  }

  public static void main(String[] args) throws Exception {
    if (args.length != 1) {
      System.err.println("usage: AclService <schema-registry.properties>");
      System.exit(2);
    }
    String user = requireEnv("ACL_SERVICE_USER");
    String password = requireEnv("ACL_SERVICE_PASSWORD");
    int port = Integer.parseInt(System.getenv().getOrDefault("ACL_SERVICE_PORT", "8085"));

    Properties props = new Properties();
    try (InputStream in = new FileInputStream(args[0])) {
      props.load(in);
    }
    AclStore store = new AclStore(new SecureSchemaRegistryConfig(props));
    AclService service = new AclService(store, user, password);

    HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
    server.createContext("/health", ex -> service.handle(ex, false));
    server.createContext("/operations", ex -> service.handle(ex, true));
    server.createContext("/acls", ex -> service.handle(ex, true));
    server.setExecutor(Executors.newFixedThreadPool(8));
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      server.stop(1);
      store.close();
    }));
    server.start();
    System.out.println("sr-acl-service listening on :" + port + ", ACL topic read");
  }

  private static String requireEnv(String name) {
    String v = System.getenv(name);
    if (v == null || v.isEmpty()) {
      System.err.println(name + " must be set");
      System.exit(2);
    }
    return v;
  }

  private void handle(HttpExchange ex, boolean authRequired) throws IOException {
    try {
      if (authRequired && !authorized(ex)) {
        ex.getResponseHeaders().add("WWW-Authenticate", "Basic realm=\"sr-acl-service\"");
        throw new BadRequest(401, "unauthorized");
      }
      String path = ex.getRequestURI().getPath();
      String method = ex.getRequestMethod();
      Object body = switch (path + " " + method) {
        case "/health GET" -> Map.of("status", "UP");
        case "/operations GET" -> Map.of(
            "subject", SchemaRegistryResourceOperation.SUBJECT_RESOURCE_OPERATIONS,
            "global", SchemaRegistryResourceOperation.GLOBAL_RESOURCE_OPERATIONS);
        case "/acls GET" -> {
          Map<String, String> q = query(ex);
          yield store.list(q.get("principal"), q.get("subject"));
        }
        case "/acls POST" -> change(ex, true);
        case "/acls DELETE" -> change(ex, false);
        default -> throw new BadRequest(404, "no route for " + method + " " + path);
      };
      send(ex, 200, body);
    } catch (BadRequest e) {
      send(ex, e.status, Map.of("error", e.getMessage()));
    } catch (Exception e) {
      e.printStackTrace();
      send(ex, 500, Map.of("error", String.valueOf(e)));
    } finally {
      ex.close();
    }
  }

  private boolean authorized(HttpExchange ex) {
    String h = ex.getRequestHeaders().getFirst("Authorization");
    return h != null && MessageDigest.isEqual(expectedAuth, h.getBytes(StandardCharsets.UTF_8));
  }

  private AclStore.Acls change(HttpExchange ex, boolean add) throws Exception {
    AclRequest req;
    try (InputStream in = ex.getRequestBody()) {
      req = JSON.readValue(in, AclRequest.class);
    } catch (com.fasterxml.jackson.core.JacksonException e) {
      throw new BadRequest(400, "invalid JSON body: " + e.getOriginalMessage());
    }
    List<String> principals = all(nonBlank(req.principals(), "principals"));

    Set<SchemaRegistryResourceOperation> ops = EnumSet.noneOf(SchemaRegistryResourceOperation.class);
    for (String name : nonBlank(req.operations(), "operations")) {
      if (name.equals("*")) {
        ops.addAll(SchemaRegistryResourceOperation.ACL_COMMAND_OPERATIONS);
        continue;
      }
      String n = name.toUpperCase();
      if (n.equals("GLOBAL_SUBJECTS_READ")) n = "GLOBAL_READ"; // alias the CLI accepts
      try {
        SchemaRegistryResourceOperation op = SchemaRegistryResourceOperation.valueOf(n);
        if (!SchemaRegistryResourceOperation.ACL_COMMAND_OPERATIONS.contains(op)) throw new IllegalArgumentException();
        ops.add(op);
      } catch (IllegalArgumentException e) {
        throw new BadRequest(400, "invalid operation: " + name);
      }
    }

    List<String> subjects = new ArrayList<>(req.subjects() == null ? List.of() : nonBlank(req.subjects(), "subjects"));
    for (String t : req.topics() == null ? List.<String>of() : nonBlank(req.topics(), "topics")) {
      if (t.equals("*")) subjects.add("*");
      else { subjects.add(t + "-key"); subjects.add(t + "-value"); }
    }
    subjects = all(subjects);

    boolean hasSubjectOps = ops.stream().anyMatch(SchemaRegistryResourceOperation.SUBJECT_RESOURCE_OPERATIONS::contains);
    boolean hasGlobalOps = ops.stream().anyMatch(SchemaRegistryResourceOperation.GLOBAL_RESOURCE_OPERATIONS::contains);
    if (hasSubjectOps && subjects.isEmpty()) {
      throw new BadRequest(400, "subjects or topics are required for subject operations");
    }
    if (hasGlobalOps && !hasSubjectOps && !subjects.isEmpty()) {
      throw new BadRequest(400, "subjects/topics can't be given for global operations only");
    }
    return store.apply(add, principals, subjects, ops);
  }

  private static List<String> nonBlank(List<String> values, String field) throws BadRequest {
    if (values == null || values.isEmpty()) throw new BadRequest(400, field + " is required");
    for (String v : values) {
      if (v == null || v.isBlank()) throw new BadRequest(400, field + " must not contain blank values");
    }
    return values;
  }

  /** "*" anywhere in the list means just "*", as in SchemaRegistryAclCommandOptions. */
  private static List<String> all(List<String> values) {
    return values.contains("*") ? List.of("*") : values.stream().distinct().toList();
  }

  private static Map<String, String> query(HttpExchange ex) {
    Map<String, String> q = new HashMap<>();
    String raw = ex.getRequestURI().getRawQuery();
    if (raw == null) return q;
    for (String pair : raw.split("&")) {
      int i = pair.indexOf('=');
      if (i > 0) {
        q.put(URLDecoder.decode(pair.substring(0, i), StandardCharsets.UTF_8),
            URLDecoder.decode(pair.substring(i + 1), StandardCharsets.UTF_8));
      }
    }
    return q;
  }

  private static void send(HttpExchange ex, int status, Object body) throws IOException {
    byte[] bytes = JSON.writerWithDefaultPrettyPrinter().writeValueAsBytes(body);
    ex.getResponseHeaders().set("Content-Type", "application/json");
    ex.sendResponseHeaders(status, bytes.length);
    ex.getResponseBody().write(bytes);
  }
}
