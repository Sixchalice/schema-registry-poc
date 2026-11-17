package probe;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.google.protobuf.Descriptors.Descriptor;
import com.google.protobuf.DynamicMessage;
import com.kjetland.jackson.jsonSchema.annotations.JsonSchemaTitle;
import io.confluent.kafka.schemaregistry.client.rest.entities.SchemaReference;
import io.confluent.kafka.schemaregistry.json.JsonSchema;
import io.confluent.kafka.schemaregistry.json.JsonSchemaUtils;
import io.confluent.kafka.schemaregistry.protobuf.ProtobufSchema;
import io.confluent.kafka.serializers.KafkaAvroDeserializer;
import io.confluent.kafka.serializers.KafkaAvroSerializer;
import io.confluent.kafka.serializers.json.KafkaJsonSchemaDeserializer;
import io.confluent.kafka.serializers.json.KafkaJsonSchemaSerializer;
import io.confluent.kafka.serializers.protobuf.KafkaProtobufDeserializer;
import io.confluent.kafka.serializers.protobuf.KafkaProtobufSerializer;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;

/** Item 2 (Java half): real Confluent 7.9.0 serializers with RecordNameStrategy,
 *  auto.register.schemas=false, use.latest.version=true. */
public class Main {
  static final String SR = System.getenv().getOrDefault("SR_URL", "http://schema-registry:8081");
  static final String BOOT = System.getenv().getOrDefault("BOOTSTRAP", "kafka:29092");
  static final String N = "p5java";
  static final String TOPIC = "p5-any-topic";
  static final ObjectMapper M = new ObjectMapper();
  static final HttpClient HTTP = HttpClient.newHttpClient();
  static KafkaProducer<String, byte[]> producer;

  interface Ser { byte[] run() throws Exception; }
  interface De { Object run(byte[] b) throws Exception; }

  static Map<String, Object> conf(Object... extra) {
    Map<String, Object> m = new HashMap<>();
    m.put("schema.registry.url", SR);
    m.put("auto.register.schemas", false);
    m.put("use.latest.version", true);
    m.put("value.subject.name.strategy", "io.confluent.kafka.serializers.subject.RecordNameStrategy");
    for (int i = 0; i < extra.length; i += 2) m.put((String) extra[i], extra[i + 1]);
    return m;
  }

  static String chain(Throwable t) {
    StringBuilder sb = new StringBuilder();
    for (; t != null; t = t.getCause()) sb.append(t.getClass().getSimpleName()).append(": ").append(t.getMessage()).append(" <- ");
    String s = sb.toString().replace("\n", " ");
    return s.length() > 330 && System.getenv("FULL") == null ? s.substring(0, 330) + "..." : s;
  }

  static byte[] attempt(String label, Ser s) {
    try {
      byte[] b = s.run();
      System.out.println("  " + label + ": OK (" + b.length + " bytes)");
      return b;
    } catch (Exception e) {
      System.out.println("  " + label + ": " + chain(e));
      return null;
    }
  }

  static void roundTrip(byte[] b, De d) {
    if (b == null) return;
    try {
      producer.send(new ProducerRecord<>(TOPIC, b)).get();
      Object o = d.run(b);
      String s = String.valueOf(o).replace("\n", " ");
      System.out.println("  produced; deserialized as " + o.getClass().getSimpleName() + ": " + (s.length() > 150 ? s.substring(0, 150) : s));
    } catch (Exception e) {
      System.out.println("  DESERIALIZE FAILED: " + chain(e));
    }
  }

  static String http(String method, String path, String body) throws Exception {
    HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(SR + path)).header("Content-Type", "application/vnd.schemaregistry.v1+json");
    b.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body));
    HttpResponse<String> r = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
    return r.statusCode() + " " + r.body();
  }

  static String enc(String s) { return URLEncoder.encode(s, StandardCharsets.UTF_8); }

  static void register(String subject, String type, String schema, String... nameSubjectPairs) throws Exception {
    ObjectNode body = M.createObjectNode().put("schema", schema);
    if (!type.equals("AVRO")) body.put("schemaType", type);
    if (nameSubjectPairs.length > 0) {
      ArrayNode refs = body.putArray("references");
      for (int i = 0; i < nameSubjectPairs.length; i += 2) refs.addObject().put("name", nameSubjectPairs[i]).put("subject", nameSubjectPairs[i + 1]).put("version", 1);
    }
    String res = http("POST", "/subjects/" + enc(subject) + "/versions", M.writeValueAsString(body));
    if (!res.startsWith("200")) System.out.println("  REGISTER FAILED " + subject + ": " + (res.length() > 300 ? res.substring(res.length() - 300) : res));
  }

  static void delete(String... subjects) throws Exception {
    for (String s : subjects) { http("DELETE", "/subjects/" + enc(s), null); http("DELETE", "/subjects/" + enc(s) + "?permanent=true", null); }
  }

  // ---- JSON POJOs ----
  public static class PlainOrder { public String id = "1"; }

  @JsonSchemaTitle("p5java.TitledOrder")
  public static class TitledOrder { public String id = "1"; }

  @io.confluent.kafka.schemaregistry.annotations.Schema(
      value = "{\"title\":\"p5java.AnnotatedOrder\",\"type\":\"object\",\"properties\":{\"id\":{\"type\":\"string\"}}}", refs = {})
  public static class AnnotatedOrder { public String id = "1"; }

  public static void main(String[] args) throws Exception {
    Map<String, Object> pc = new HashMap<>();
    pc.put("bootstrap.servers", BOOT);
    pc.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
    pc.put("value.serializer", "org.apache.kafka.common.serialization.ByteArraySerializer");
    producer = new KafkaProducer<>(pc);
    // dependents first
    delete(N + ".Customer", N + ".JCustomer", N + ".PCustomer", N + ".PCustomerBadRef", N + ".Order", N + ".real.Dotted", N + ".Address",
        N + ".TitledOrder", N + ".AnnotatedOrder", N + ".EnvOrder", N + ".JAddress", N + ".POrder", N + ".PAddress", N + ".PStamped", N + ".Multi1", N + ".Multi2",
        "PlainOrder", "Plain Order", N + ".PlainOrder");

    // ================= AVRO =================
    System.out.println("\n[AVRO simple] expected_name = " + N + ".Order");
    String a1 = "{\"type\":\"record\",\"name\":\"Order\",\"namespace\":\"" + N + "\",\"fields\":[{\"name\":\"id\",\"type\":\"string\"}]}";
    Schema s1 = new Schema.Parser().parse(a1);
    GenericRecord r1 = new GenericData.Record(s1); r1.put("id", "1");
    KafkaAvroSerializer avro = new KafkaAvroSerializer(); avro.configure(conf(), false);
    KafkaAvroDeserializer avroDe = new KafkaAvroDeserializer(); avroDe.configure(conf(), false);
    attempt("before registering", () -> avro.serialize(TOPIC, r1));
    register(N + ".Order", "AVRO", a1);
    roundTrip(attempt("after registering ", () -> avro.serialize(TOPIC, r1)), b -> avroDe.deserialize(TOPIC, b));

    System.out.println("\n[AVRO dotted name + conflicting namespace] expected_name = " + N + ".real.Dotted");
    String a2 = "{\"type\":\"record\",\"name\":\"" + N + ".real.Dotted\",\"namespace\":\"ignored.ns\",\"fields\":[{\"name\":\"id\",\"type\":\"string\"}]}";
    Schema s2 = new Schema.Parser().parse(a2);
    GenericRecord r2 = new GenericData.Record(s2); r2.put("id", "1");
    attempt("before registering", () -> avro.serialize(TOPIC, r2));
    register(N + ".real.Dotted", "AVRO", a2);
    roundTrip(attempt("after registering ", () -> avro.serialize(TOPIC, r2)), b -> avroDe.deserialize(TOPIC, b));

    System.out.println("\n[AVRO with reference; the client's own schema has Address inlined] expected_name = " + N + ".Customer");
    String addr = "{\"type\":\"record\",\"name\":\"Address\",\"namespace\":\"" + N + "\",\"fields\":[{\"name\":\"city\",\"type\":\"string\"}]}";
    String custRegistered = "{\"type\":\"record\",\"name\":\"Customer\",\"namespace\":\"" + N + "\",\"fields\":[{\"name\":\"addr\",\"type\":\"" + N + ".Address\"}]}";
    String custClient = "{\"type\":\"record\",\"name\":\"Customer\",\"namespace\":\"" + N + "\",\"fields\":[{\"name\":\"addr\",\"type\":" + addr + "}]}";
    Schema s3 = new Schema.Parser().parse(custClient);
    GenericRecord ra = new GenericData.Record(s3.getField("addr").schema()); ra.put("city", "TLV");
    GenericRecord r3 = new GenericData.Record(s3); r3.put("addr", ra);
    attempt("before registering", () -> avro.serialize(TOPIC, r3));
    register(N + ".Address", "AVRO", addr);
    register(N + ".Customer", "AVRO", custRegistered, N + ".Address", N + ".Address");
    roundTrip(attempt("after registering ", () -> avro.serialize(TOPIC, r3)), b -> avroDe.deserialize(TOPIC, b));

    // ================= JSON =================
    KafkaJsonSchemaSerializer<Object> json = new KafkaJsonSchemaSerializer<>(); json.configure(conf(), false);
    KafkaJsonSchemaDeserializer<Object> jsonDe = new KafkaJsonSchemaDeserializer<>(); jsonDe.configure(conf(), false);

    System.out.println("\n[JSON plain POJO, no annotations] which subject does the client ask for?");
    attempt("serialize", () -> json.serialize(TOPIC, new PlainOrder()));
    System.out.println("  schema the client derives: " + JsonSchemaUtils.getSchema(new PlainOrder()).canonicalString());

    System.out.println("\n[JSON POJO with @JsonSchemaTitle] expected_name = " + N + ".TitledOrder");
    attempt("before registering", () -> json.serialize(TOPIC, new TitledOrder()));
    System.out.println("  schema the client derives: " + JsonSchemaUtils.getSchema(new TitledOrder()).canonicalString());
    register(N + ".TitledOrder", "JSON", "{\"title\":\"" + N + ".TitledOrder\",\"type\":\"object\",\"properties\":{\"id\":{\"type\":\"string\"}}}");
    roundTrip(attempt("after registering ", () -> json.serialize(TOPIC, new TitledOrder())), b -> jsonDe.deserialize(TOPIC, b));

    System.out.println("\n[JSON POJO with Confluent @Schema(value=<schema text>)] expected_name = " + N + ".AnnotatedOrder");
    attempt("before registering", () -> json.serialize(TOPIC, new AnnotatedOrder()));
    register(N + ".AnnotatedOrder", "JSON", "{\"title\":\"" + N + ".AnnotatedOrder\",\"type\":\"object\",\"properties\":{\"id\":{\"type\":\"string\"}}}");
    roundTrip(attempt("after registering ", () -> json.serialize(TOPIC, new AnnotatedOrder())), b -> jsonDe.deserialize(TOPIC, b));

    System.out.println("\n[JSON JsonNode + schema envelope] expected_name = " + N + ".EnvOrder");
    String j1 = "{\"title\":\"" + N + ".EnvOrder\",\"type\":\"object\",\"properties\":{\"id\":{\"type\":\"string\"}}}";
    JsonNode payload = M.readTree("{\"id\":\"1\"}");
    ObjectNode env1 = JsonSchemaUtils.envelope(new JsonSchema(j1), payload);
    attempt("before registering", () -> json.serialize(TOPIC, env1));
    register(N + ".EnvOrder", "JSON", j1);
    roundTrip(attempt("after registering ", () -> json.serialize(TOPIC, env1)), b -> jsonDe.deserialize(TOPIC, b));

    System.out.println("\n[JSON with external $ref] expected_name = " + N + ".JCustomer");
    String jaddr = "{\"title\":\"" + N + ".JAddress\",\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}}}";
    String jcust = "{\"title\":\"" + N + ".JCustomer\",\"type\":\"object\",\"properties\":{\"addr\":{\"$ref\":\"address.json\"}}}";
    JsonSchema jcs = new JsonSchema(jcust, List.of(new SchemaReference("address.json", N + ".JAddress", 1)), Map.of("address.json", jaddr), null);
    ObjectNode env2 = JsonSchemaUtils.envelope(jcs, M.readTree("{\"addr\":{\"city\":\"TLV\"}}"));
    attempt("before registering", () -> json.serialize(TOPIC, env2));
    register(N + ".JAddress", "JSON", jaddr);
    register(N + ".JCustomer", "JSON", jcust, "address.json", N + ".JAddress");
    roundTrip(attempt("after registering ", () -> json.serialize(TOPIC, env2)), b -> jsonDe.deserialize(TOPIC, b));
    ObjectNode env2bad = JsonSchemaUtils.envelope(jcs, M.readTree("{\"addr\":{\"city\":5}}"));
    KafkaJsonSchemaSerializer<Object> jsonV = new KafkaJsonSchemaSerializer<>(); jsonV.configure(conf("json.fail.invalid.schema", true), false);
    attempt("invalid payload with json.fail.invalid.schema=true", () -> jsonV.serialize(TOPIC, env2bad));

    // ================= PROTOBUF =================
    KafkaProtobufSerializer<DynamicMessage> proto = new KafkaProtobufSerializer<>(); proto.configure(conf(), false);
    KafkaProtobufDeserializer<com.google.protobuf.Message> protoDe = new KafkaProtobufDeserializer<>(); protoDe.configure(conf(), false);

    System.out.println("\n[PROTOBUF simple] expected_name = " + N + ".POrder");
    String p1 = "syntax = \"proto3\";\npackage " + N + ";\nmessage POrder { string id = 1; }\n";
    Descriptor d1 = new ProtobufSchema(p1).toDescriptor();
    DynamicMessage m1 = DynamicMessage.newBuilder(d1).setField(d1.findFieldByName("id"), "1").build();
    attempt("before registering", () -> proto.serialize(TOPIC, m1));
    register(N + ".POrder", "PROTOBUF", p1);
    roundTrip(attempt("after registering ", () -> proto.serialize(TOPIC, m1)), b -> protoDe.deserialize(TOPIC, b));

    System.out.println("\n[PROTOBUF with java_package / java_outer_classname options] expected_name = " + N + ".POrder");
    String p1j = "syntax = \"proto3\";\npackage " + N + ";\noption java_package = \"com.other.pkg\";\noption java_outer_classname = \"Wrapper\";\nmessage POrder { string id = 1; }\n";
    Descriptor d1j = new ProtobufSchema(p1j).toDescriptor();
    DynamicMessage m1j = DynamicMessage.newBuilder(d1j).setField(d1j.findFieldByName("id"), "1").build();
    attempt("serialize (subject already registered above)", () -> proto.serialize(TOPIC, m1j));

    System.out.println("\n[PROTOBUF importing google/protobuf/timestamp.proto] expected_name = " + N + ".PStamped");
    String p2 = "syntax = \"proto3\";\npackage " + N + ";\nimport \"google/protobuf/timestamp.proto\";\nmessage PStamped { google.protobuf.Timestamp at = 1; }\n";
    Descriptor d2 = new ProtobufSchema(p2).toDescriptor();
    DynamicMessage m2 = DynamicMessage.newBuilder(d2).build();
    attempt("before registering", () -> proto.serialize(TOPIC, m2));
    register(N + ".PStamped", "PROTOBUF", p2);
    roundTrip(attempt("after registering ", () -> proto.serialize(TOPIC, m2)), b -> protoDe.deserialize(TOPIC, b));

    System.out.println("\n[PROTOBUF with import of another Schema, reference name = import path] expected_name = " + N + ".PCustomer");
    String imp = N + "/address.proto";
    String paddr = "syntax = \"proto3\";\npackage " + N + ";\nmessage PAddress { string city = 1; }\n";
    String pcust = "syntax = \"proto3\";\npackage " + N + ";\nimport \"" + imp + "\";\nmessage PCustomer { PAddress addr = 1; }\n";
    ProtobufSchema pcs = new ProtobufSchema(pcust, List.of(new SchemaReference(imp, N + ".PAddress", 1)), Map.of(imp, paddr), null, null);
    Descriptor d3 = pcs.toDescriptor();
    Descriptor dAddr = d3.findFieldByName("addr").getMessageType();
    DynamicMessage m3 = DynamicMessage.newBuilder(d3).setField(d3.findFieldByName("addr"),
        DynamicMessage.newBuilder(dAddr).setField(dAddr.findFieldByName("city"), "TLV").build()).build();
    attempt("before registering", () -> proto.serialize(TOPIC, m3));
    register(N + ".PAddress", "PROTOBUF", paddr);
    register(N + ".PCustomer", "PROTOBUF", pcust, imp, N + ".PAddress");
    roundTrip(attempt("after registering ", () -> proto.serialize(TOPIC, m3)), b -> protoDe.deserialize(TOPIC, b));

    System.out.println("\n[PROTOBUF registered with reference name != import path (the registry accepts this)]");
    String pbad = pcust.replace("PCustomer", "PCustomerBadRef");
    register(N + ".PCustomerBadRef", "PROTOBUF", pbad, "something/else.proto", N + ".PAddress");
    ProtobufSchema pbs = new ProtobufSchema(pbad, List.of(new SchemaReference(imp, N + ".PAddress", 1)), Map.of(imp, paddr), null, null);
    Descriptor d4 = pbs.toDescriptor();
    DynamicMessage m4 = DynamicMessage.newBuilder(d4).setField(d4.findFieldByName("addr"),
        DynamicMessage.newBuilder(dAddr).setField(dAddr.findFieldByName("city"), "TLV").build()).build();
    roundTrip(attempt("serialize", () -> proto.serialize(TOPIC, m4)), b -> protoDe.deserialize(TOPIC, b));

    System.out.println("\n[PROTOBUF file with two top-level messages] which subject per message?");
    String p5 = "syntax = \"proto3\";\npackage " + N + ";\nmessage Multi1 { string a = 1; }\nmessage Multi2 { string b = 1; }\n";
    ProtobufSchema ms = new ProtobufSchema(p5);
    DynamicMessage mm1 = DynamicMessage.newBuilder(ms.toDescriptor("Multi1")).build();
    DynamicMessage mm2 = DynamicMessage.newBuilder(ms.toDescriptor("Multi2")).build();
    attempt("first message ", () -> proto.serialize(TOPIC, mm1));
    attempt("second message", () -> proto.serialize(TOPIC, mm2));

    producer.close();
  }
}
