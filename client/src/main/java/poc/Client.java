package poc;

import io.confluent.kafka.serializers.KafkaAvroDeserializer;
import io.confluent.kafka.serializers.KafkaAvroSerializer;
import java.io.InputStream;
import java.time.Duration;
import java.util.List;
import java.util.Properties;
import java.util.UUID;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.apache.kafka.common.serialization.StringSerializer;

/**
 * Tenant-style Avro client: read-only against Schema Registry (auto.register.schemas=false).
 *
 * <p>Usage: {@code produce|consume <topic> <cluster> <scramUser> <password> <topic|record>}
 *
 * <p>Kafka login = scramUser; Schema Registry login = cluster.scramUser, same password.
 * Exit code 0 = success, 1 = failure (cause printed on stdout as RESULT line).
 */
public class Client {
  static final String BOOTSTRAP = env("BOOTSTRAP", "kafka:29092");
  static final String SR_URL = env("SR_URL", "http://schema-registry:8081");

  public static void main(String[] args) throws Exception {
    if (args.length != 6) {
      System.err.println("usage: produce|consume <topic> <cluster> <scramUser> <password> <topic|record>");
      System.exit(2);
    }
    String mode = args[0], topic = args[1], cluster = args[2], user = args[3], pass = args[4], strategy = args[5];
    Properties p = new Properties();
    p.put("bootstrap.servers", BOOTSTRAP);
    p.put("security.protocol", "SASL_PLAINTEXT");
    p.put("sasl.mechanism", "SCRAM-SHA-256");
    p.put("sasl.jaas.config", "org.apache.kafka.common.security.scram.ScramLoginModule required username=\""
        + user + "\" password=\"" + pass + "\";");
    p.put("schema.registry.url", SR_URL);
    p.put("basic.auth.credentials.source", "USER_INFO");
    p.put("basic.auth.user.info", cluster + "." + user + ":" + pass);
    p.put("auto.register.schemas", "false");
    p.put("use.latest.version", "false");
    if (strategy.equals("record")) {
      // Built-in strategy selected purely by client config
      p.put("value.subject.name.strategy", "io.confluent.kafka.serializers.subject.RecordNameStrategy");
    }
    try {
      if (mode.equals("produce")) {
        produce(p, topic);
      } else {
        consume(p, topic, cluster + "." + user);
      }
    } catch (Throwable t) {
      Throwable root = t;
      while (root.getCause() != null && root.getCause() != root) {
        root = root.getCause();
      }
      System.out.println("RESULT FAIL " + t.getClass().getSimpleName() + ": " + oneLine(t.getMessage())
          + " | root: " + root.getClass().getSimpleName() + ": " + oneLine(root.getMessage()));
      System.exit(1);
    }
  }

  static void produce(Properties p, String topic) throws Exception {
    p.put("key.serializer", StringSerializer.class.getName());
    p.put("value.serializer", KafkaAvroSerializer.class.getName());
    p.put("acks", "all");
    p.put("max.block.ms", "15000");
    Schema schema = loadSchema(topic.equals("payments") ? "payment.avsc" : "order.avsc");
    try (KafkaProducer<String, GenericRecord> producer = new KafkaProducer<>(p)) {
      GenericRecord r = new GenericData.Record(schema);
      String id = UUID.randomUUID().toString();
      r.put("id", id);
      if (topic.equals("payments")) {
        r.put("orderId", "o-1");
        r.put("cents", 1234L);
      } else {
        r.put("amount", 12.34);
      }
      var md = producer.send(new ProducerRecord<>(topic, id, r)).get();
      System.out.println("RESULT OK produced to " + md.topic() + "-" + md.partition() + "@" + md.offset());
    }
  }

  static void consume(Properties p, String topic, String srUser) {
    p.put("key.deserializer", StringDeserializer.class.getName());
    p.put("value.deserializer", KafkaAvroDeserializer.class.getName());
    // group.id must match the tenant's Kafka group ACL prefix (c17.svc-x)
    p.put("group.id", srUser + ".poc-" + UUID.randomUUID());
    p.put("auto.offset.reset", "earliest");
    p.put("enable.auto.commit", "false");
    try (KafkaConsumer<String, Object> consumer = new KafkaConsumer<>(p)) {
      consumer.subscribe(List.of(topic));
      long deadline = System.currentTimeMillis() + 20000;
      while (System.currentTimeMillis() < deadline) {
        for (ConsumerRecord<String, Object> rec : consumer.poll(Duration.ofSeconds(1))) {
          GenericRecord v = (GenericRecord) rec.value();
          System.out.println("RESULT OK consumed " + v.getSchema().getFullName() + " " + v);
          return;
        }
      }
      throw new IllegalStateException("no records within 20s");
    }
  }

  static Schema loadSchema(String name) throws Exception {
    try (InputStream in = Client.class.getResourceAsStream("/" + name)) {
      return new Schema.Parser().parse(in);
    }
  }

  static String oneLine(String s) {
    return s == null ? "" : s.replaceAll("\\s+", " ");
  }

  static String env(String k, String d) {
    String v = System.getenv(k);
    return v == null ? d : v;
  }
}
