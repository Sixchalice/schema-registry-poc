package probe;

import static probe.Main.*;

import io.confluent.kafka.serializers.KafkaAvroDeserializer;
import io.confluent.kafka.serializers.KafkaAvroSerializer;
import java.util.HashMap;
import java.util.Map;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;
import org.apache.kafka.clients.producer.KafkaProducer;

/** Audit 2 F2: a top-level Avro enum or fixed Schema, used as a reference. Color (enum) and Md5 (fixed)
 *  are registered as their own subjects; the registered Order record references both. With the Java Avro
 *  serializer (use.latest.version=true, auto.register.schemas=false, RecordNameStrategy):
 *  (a) an Order whose client schema has Color and Md5 inlined must produce and read back;
 *  (b) a bare Color value and (c) a bare Md5 value must be refused (the audit's F2 failure).
 *  Exit 1 if any case is not as expected. */
public class Enums {
  static int failures = 0;

  static void mustExist(String subject) throws Exception {
    if (!http("GET", "/subjects/" + enc(subject) + "/versions/latest", null).startsWith("200")) {
      failures++;
      System.out.println("  NOT REGISTERED: " + subject);
    }
  }

  public static void main(String[] args) throws Exception {
    Map<String, Object> pc = new HashMap<>();
    pc.put("bootstrap.servers", BOOT);
    pc.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
    pc.put("value.serializer", "org.apache.kafka.common.serialization.ByteArraySerializer");
    producer = new KafkaProducer<>(pc);
    String P = "p9.enumref";
    delete(P + ".Order", P + ".Color", P + ".Md5");

    System.out.println("\n[AVRO top-level enum and fixed as references] " + P + ".Order");
    String color = "{\"type\":\"enum\",\"name\":\"Color\",\"namespace\":\"" + P + "\",\"symbols\":[\"RED\",\"BLUE\"]}";
    String md5 = "{\"type\":\"fixed\",\"name\":\"Md5\",\"namespace\":\"" + P + "\",\"size\":2}";
    String orderRef = "{\"type\":\"record\",\"name\":\"Order\",\"namespace\":\"" + P + "\",\"fields\":["
        + "{\"name\":\"color\",\"type\":\"" + P + ".Color\"},{\"name\":\"hash\",\"type\":\"" + P + ".Md5\"}]}";
    register(P + ".Color", "AVRO", color);
    register(P + ".Md5", "AVRO", md5);
    register(P + ".Order", "AVRO", orderRef, P + ".Color", P + ".Color", P + ".Md5", P + ".Md5");
    for (String s : new String[] {P + ".Color", P + ".Md5", P + ".Order"}) mustExist(s);

    KafkaAvroSerializer avro = new KafkaAvroSerializer(); avro.configure(conf(), false);
    KafkaAvroDeserializer avroDe = new KafkaAvroDeserializer(); avroDe.configure(conf(), false);

    Schema orderSchema = new Schema.Parser().parse(
        orderRef.replace("\"" + P + ".Color\"", color).replace("\"" + P + ".Md5\"", md5));
    Schema colorSchema = orderSchema.getField("color").schema();
    Schema md5Schema = orderSchema.getField("hash").schema();
    GenericRecord order = new GenericData.Record(orderSchema);
    order.put("color", new GenericData.EnumSymbol(colorSchema, "BLUE"));
    order.put("hash", new GenericData.Fixed(md5Schema, new byte[] {1, 2}));

    Nested.expectOk("(a) Order referencing Color and Md5", () -> avro.serialize(TOPIC, order), b -> avroDe.deserialize(TOPIC, b));
    Nested.expectFail("(b) bare Color value", () -> avro.serialize(TOPIC, new GenericData.EnumSymbol(colorSchema, "RED")));
    Nested.expectFail("(c) bare Md5 value", () -> avro.serialize(TOPIC, new GenericData.Fixed(md5Schema, new byte[] {1, 2})));
    failures += Nested.failures;

    producer.close();
    System.out.println(failures == 0 ? "\nEnums: all cases as expected" : "\nEnums: " + failures + " case(s) NOT as expected");
    System.exit(failures == 0 ? 0 : 1);
  }
}
