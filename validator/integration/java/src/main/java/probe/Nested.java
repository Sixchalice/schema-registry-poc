package probe;

import static probe.Main.*;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.google.protobuf.Descriptors.Descriptor;
import com.google.protobuf.Descriptors.EnumDescriptor;
import com.google.protobuf.DynamicMessage;
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
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;
import org.apache.kafka.clients.producer.KafkaProducer;

/** Types nested inside a referenced schema, used from another schema, with the Java serializers.
 *  Unlike Main and ProtoRefs this class checks its results: it exits 1 when a case does not
 *  behave as the validator assumes (Protobuf and JSON work). The Avro shape, which validate
 *  rejects, is only reported. */
public class Nested {
  static int failures = 0;

  static void expectOk(String label, Ser s, De d) {
    try {
      byte[] b = s.run();
      producer.send(new org.apache.kafka.clients.producer.ProducerRecord<>(TOPIC, b)).get();
      String back = String.valueOf(d.run(b)).replace("\n", " ");
      System.out.println("  " + label + ": OK (" + b.length + " bytes), read back: " + back);
    } catch (Exception e) {
      failures++;
      System.out.println("  " + label + ": UNEXPECTED FAILURE " + chain(e));
    }
  }

  static void expectFail(String label, Ser s) {
    try {
      s.run();
      failures++;
      System.out.println("  " + label + ": UNEXPECTED SUCCESS");
    } catch (Exception e) {
      System.out.println("  " + label + ": refused as expected: " + chain(e));
    }
  }

  /** Reports without counting: the Avro shape is rejected by validate whatever Java does. */
  static void result(String label, Ser s, De d) {
    try {
      byte[] b = s.run();
      System.out.println("  " + label + ": OK (" + b.length + " bytes), read back: " + String.valueOf(d.run(b)).replace("\n", " "));
    } catch (Exception e) {
      System.out.println("  " + label + ": FAILS " + chain(e));
    }
  }

  static GenericRecord customer(Schema as) {
    Schema addrSchema = as.getField("addr").schema();
    Schema countrySchema = as.getField("c").schema();
    GenericRecord ar = new GenericData.Record(addrSchema);
    ar.put("city", "TLV"); ar.put("country", new GenericData.EnumSymbol(countrySchema, "NL"));
    GenericRecord rec = new GenericData.Record(as);
    rec.put("addr", ar); rec.put("c", new GenericData.EnumSymbol(countrySchema, "US"));
    return rec;
  }

  public static void main(String[] args) throws Exception {
    Map<String, Object> pc = new HashMap<>();
    pc.put("bootstrap.servers", BOOT);
    pc.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
    pc.put("value.serializer", "org.apache.kafka.common.serialization.ByteArraySerializer");
    producer = new KafkaProducer<>(pc);
    String P = "p7.nested";
    delete(P + ".PCustomer", P + ".PAddress", P + ".JCustomer", P + ".JAddress", P + ".ACustomer", P + ".AAddress", P + ".AColor", P + ".APaint");

    // ---- PROTOBUF: a message and an enum nested in the imported PAddress ----
    System.out.println("\n[PROTOBUF nested message and enum of an imported Schema] " + P + ".PCustomer");
    String imp = "p7/nested/PAddress.proto";
    String paddr = "syntax = \"proto3\";\npackage " + P + ";\nmessage PAddress {\n  string city = 1;\n"
        + "  message Geo { double lat = 1; }\n  enum Kind { HOME = 0; WORK = 1; }\n}\n";
    String pcust = "syntax = \"proto3\";\npackage " + P + ";\nimport \"" + imp + "\";\n"
        + "message PCustomer {\n  PAddress.Geo geo = 1;\n  " + P + ".PAddress.Kind kind = 2;\n}\n";
    register(P + ".PAddress", "PROTOBUF", paddr);
    register(P + ".PCustomer", "PROTOBUF", pcust, imp, P + ".PAddress");
    ProtobufSchema ps = new ProtobufSchema(pcust, List.of(new SchemaReference(imp, P + ".PAddress", 1)), Map.of(imp, paddr), null, null);
    Descriptor pd = ps.toDescriptor();
    Descriptor geo = pd.findFieldByName("geo").getMessageType();
    EnumDescriptor kind = pd.findFieldByName("kind").getEnumType();
    DynamicMessage pm = DynamicMessage.newBuilder(pd)
        .setField(pd.findFieldByName("geo"), DynamicMessage.newBuilder(geo).setField(geo.findFieldByName("lat"), 1.5).build())
        .setField(pd.findFieldByName("kind"), kind.findValueByName("WORK")).build();
    KafkaProtobufSerializer<DynamicMessage> proto = new KafkaProtobufSerializer<>();
    proto.configure(conf("reference.subject.name.strategy", ProtoRefs.Q), false);
    KafkaProtobufDeserializer<com.google.protobuf.Message> protoDe = new KafkaProtobufDeserializer<>(); protoDe.configure(conf(), false);
    expectOk("serialize", () -> proto.serialize(TOPIC, pm), b -> protoDe.deserialize(TOPIC, b));

    // ---- JSON: a definition nested in the referenced JAddress, itself pointing at another one ----
    System.out.println("\n[JSON $ref to a definition inside a referenced Schema] " + P + ".JCustomer");
    String jaddr = "{\"title\":\"" + P + ".JAddress\",\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},"
        + "\"definitions\":{\"Geo\":{\"type\":\"object\",\"properties\":{\"zip\":{\"$ref\":\"#/definitions/Zip\"}},\"required\":[\"zip\"]},"
        + "\"Zip\":{\"type\":\"string\",\"minLength\":5}}}";
    String jcust = "{\"title\":\"" + P + ".JCustomer\",\"type\":\"object\",\"properties\":{\"geo\":{\"$ref\":\"address.json#/definitions/Geo\"}},\"required\":[\"geo\"]}";
    register(P + ".JAddress", "JSON", jaddr);
    register(P + ".JCustomer", "JSON", jcust, "address.json", P + ".JAddress");
    JsonSchema js = new JsonSchema(jcust, List.of(new SchemaReference("address.json", P + ".JAddress", 1)), Map.of("address.json", jaddr), null);
    ObjectNode good = JsonSchemaUtils.envelope(js, M.readTree("{\"geo\":{\"zip\":\"12345\"}}"));
    ObjectNode bad = JsonSchemaUtils.envelope(js, M.readTree("{\"geo\":{\"zip\":\"1\"}}"));
    KafkaJsonSchemaSerializer<Object> json = new KafkaJsonSchemaSerializer<>(); json.configure(conf("json.fail.invalid.schema", true), false);
    KafkaJsonSchemaDeserializer<Object> jsonDe = new KafkaJsonSchemaDeserializer<>(); jsonDe.configure(conf(), false);
    expectOk("serialize", () -> json.serialize(TOPIC, good), b -> jsonDe.deserialize(TOPIC, b));
    expectFail("payload that breaks the nested definition", () -> json.serialize(TOPIC, bad));

    // ---- AVRO: the enum Country nested in the referenced AAddress (the shape validate rejects) ----
    System.out.println("\n[AVRO type nested in a referenced Schema: the shape validate rejects] " + P + ".ACustomer");
    String country = "{\"type\":\"enum\",\"name\":\"Country\",\"symbols\":[\"NL\",\"US\"]}";
    String aaddr = "{\"type\":\"record\",\"name\":\"AAddress\",\"namespace\":\"" + P + "\",\"fields\":[{\"name\":\"city\",\"type\":\"string\"},{\"name\":\"country\",\"type\":" + country + "}]}";
    String acust = "{\"type\":\"record\",\"name\":\"ACustomer\",\"namespace\":\"" + P + "\",\"fields\":[{\"name\":\"addr\",\"type\":\"" + P + ".AAddress\"},{\"name\":\"c\",\"type\":\"" + P + ".Country\"}]}";
    register(P + ".AAddress", "AVRO", aaddr);
    register(P + ".ACustomer", "AVRO", acust, P + ".AAddress", P + ".AAddress");
    KafkaAvroSerializer avro = new KafkaAvroSerializer(); avro.configure(conf(), false);
    KafkaAvroDeserializer avroDe = new KafkaAvroDeserializer(); avroDe.configure(conf(), false);
    // (1) The client parses AAddress first and then the registered text of ACustomer.
    Schema.Parser parser = new Schema.Parser();
    parser.parse(aaddr);
    GenericRecord byName = customer(parser.parse(acust));
    result("client schema built from the two registered texts", () -> avro.serialize(TOPIC, byName), b -> avroDe.deserialize(TOPIC, b));
    // (2) The client's own schema has AAddress inlined, as in Main's reference case.
    String inlined = acust.replace("\"" + P + ".AAddress\"", aaddr);
    GenericRecord withInline = customer(new Schema.Parser().parse(inlined));
    result("client schema with AAddress inlined", () -> avro.serialize(TOPIC, withInline), b -> avroDe.deserialize(TOPIC, b));

    // ---- AVRO: a top-level enum registered as its own Schema, referenced by a record (audit F5) ----
    System.out.println("\n[AVRO enum as a Schema of its own] " + P + ".APaint");
    String acolor = "{\"type\":\"enum\",\"name\":\"AColor\",\"namespace\":\"" + P + "\",\"symbols\":[\"RED\",\"GREEN\"]}";
    String apaint = "{\"type\":\"record\",\"name\":\"APaint\",\"namespace\":\"" + P + "\",\"fields\":[{\"name\":\"color\",\"type\":\"" + P + ".AColor\"}]}";
    register(P + ".AColor", "AVRO", acolor);
    register(P + ".APaint", "AVRO", apaint, P + ".AColor", P + ".AColor");
    Schema paintSchema = new Schema.Parser().parse(apaint.replace("\"" + P + ".AColor\"", acolor));
    GenericRecord paint = new GenericData.Record(paintSchema);
    paint.put("color", new GenericData.EnumSymbol(paintSchema.getField("color").schema(), "GREEN"));
    expectOk("record with the enum inlined", () -> avro.serialize(TOPIC, paint), b -> avroDe.deserialize(TOPIC, b));

    producer.close();
    System.out.println(failures == 0 ? "\nNested: all cases as expected" : "\nNested: " + failures + " case(s) NOT as expected");
    System.exit(failures == 0 ? 0 : 1);
  }
}
