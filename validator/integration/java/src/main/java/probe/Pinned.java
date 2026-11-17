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

/** A reference pinned to an older version of the referenced Schema, with the Java Avro serializer
 *  (use.latest.version=true, auto.register.schemas=false, RecordNameStrategy). Money has v1 and v2;
 *  the registered Line references Money v1. Case (a), a client schema with Money v1 inlined, must work
 *  (exit 1 otherwise). Case (b), Money v2 inlined, is only reported: it shows what a producer that built
 *  its copy from the referenced subject's latest version gets. */
public class Pinned {
  static int failures = 0;

  static GenericRecord line(String lineText, boolean withCurrency) {
    Schema as = new Schema.Parser().parse(lineText);
    Schema moneySchema = as.getField("price").schema();
    GenericRecord money = new GenericData.Record(moneySchema);
    money.put("amount", 5L);
    if (withCurrency) money.put("currency", "USD");
    GenericRecord rec = new GenericData.Record(as);
    rec.put("price", money);
    return rec;
  }

  public static void main(String[] args) throws Exception {
    Map<String, Object> pc = new HashMap<>();
    pc.put("bootstrap.servers", BOOT);
    pc.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
    pc.put("value.serializer", "org.apache.kafka.common.serialization.ByteArraySerializer");
    producer = new KafkaProducer<>(pc);
    String P = "p8.pinned";
    delete(P + ".Line", P + ".Money");

    System.out.println("\n[AVRO reference pinned to Money v1, Money v2 exists] " + P + ".Line");
    String money1 = "{\"type\":\"record\",\"name\":\"Money\",\"namespace\":\"" + P + "\",\"fields\":[{\"name\":\"amount\",\"type\":\"long\"}]}";
    String money2 = "{\"type\":\"record\",\"name\":\"Money\",\"namespace\":\"" + P + "\",\"fields\":[{\"name\":\"amount\",\"type\":\"long\"},"
        + "{\"name\":\"currency\",\"type\":\"string\",\"default\":\"EUR\"}]}";
    String lineRef = "{\"type\":\"record\",\"name\":\"Line\",\"namespace\":\"" + P + "\",\"fields\":[{\"name\":\"price\",\"type\":\"" + P + ".Money\"}]}";
    register(P + ".Money", "AVRO", money1);
    register(P + ".Money", "AVRO", money2);
    register(P + ".Line", "AVRO", lineRef, P + ".Money", P + ".Money"); // the helper pins version 1

    KafkaAvroSerializer avro = new KafkaAvroSerializer(); avro.configure(conf(), false);
    KafkaAvroDeserializer avroDe = new KafkaAvroDeserializer(); avroDe.configure(conf(), false);

    // (a) The client schema has Money v1 inlined, the version the registered Line pins.
    GenericRecord v1 = line(lineRef.replace("\"" + P + ".Money\"", money1), false);
    Nested.expectOk("(a) client schema with Money v1 inlined", () -> avro.serialize(TOPIC, v1), b -> avroDe.deserialize(TOPIC, b));
    failures += Nested.failures;

    // (b) The client schema has Money v2 inlined (built from the referenced subject's latest version).
    GenericRecord v2 = line(lineRef.replace("\"" + P + ".Money\"", money2), true);
    Nested.result("(b) client schema with Money v2 inlined (reported, not counted)", () -> avro.serialize(TOPIC, v2), b -> avroDe.deserialize(TOPIC, b));

    producer.close();
    System.out.println(failures == 0 ? "\nPinned: all cases as expected" : "\nPinned: " + failures + " case(s) NOT as expected");
    System.exit(failures == 0 ? 0 : 1);
  }
}
