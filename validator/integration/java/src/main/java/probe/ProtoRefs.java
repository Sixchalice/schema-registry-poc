package probe;

import static probe.Main.*;

import com.google.protobuf.Descriptors.Descriptor;
import com.google.protobuf.DynamicMessage;
import io.confluent.kafka.schemaregistry.client.rest.entities.SchemaReference;
import io.confluent.kafka.schemaregistry.protobuf.ProtobufSchema;
import io.confluent.kafka.serializers.protobuf.KafkaProtobufDeserializer;
import io.confluent.kafka.serializers.protobuf.KafkaProtobufSerializer;
import io.confluent.kafka.serializers.subject.QualifiedReferenceSubjectNameStrategy;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.apache.kafka.clients.producer.KafkaProducer;

/** Java Protobuf clients resolve every import to a subject through reference.subject.name.strategy,
 *  even with use.latest.version=true. Which import-path convention makes that subject = package.Message? */
public class ProtoRefs {
  static final String Q = "io.confluent.kafka.serializers.subject.QualifiedReferenceSubjectNameStrategy";

  static DynamicMessage customer(String msgName, String imp, String paddr, String text) {
    ProtobufSchema s = new ProtobufSchema(text, List.of(new SchemaReference(imp, "unused", 1)), Map.of(imp, paddr), null, null);
    Descriptor d = s.toDescriptor();
    Descriptor a = d.findFieldByName("addr").getMessageType();
    return DynamicMessage.newBuilder(d).setField(d.findFieldByName("addr"),
        DynamicMessage.newBuilder(a).setField(a.findFieldByName("city"), "TLV").build()).build();
  }

  public static void main(String[] args) throws Exception {
    Map<String, Object> pc = new HashMap<>();
    pc.put("bootstrap.servers", BOOT);
    pc.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
    pc.put("value.serializer", "org.apache.kafka.common.serialization.ByteArraySerializer");
    producer = new KafkaProducer<>(pc);
    String P = "p6.shop";
    delete(P + ".QCustomer", P + ".QCustomerBadRef", P + ".QCustomerDeep", P + ".SubjCustomer", P + ".QMid", P + ".PAddress");

    QualifiedReferenceSubjectNameStrategy q = new QualifiedReferenceSubjectNameStrategy();
    for (String ref : new String[] {"p6/shop/PAddress.proto", "p6/shop/address.proto", "PAddress.proto", "p6.shop.PAddress", "p6.shop.PAddress.proto", "google/protobuf/timestamp.proto"})
      System.out.println("  Qualified strategy: " + ref + " -> " + q.subjectName(ref, "t", false, null));

    String paddr = "syntax = \"proto3\";\npackage " + P + ";\nmessage PAddress { string city = 1; }\n";
    register(P + ".PAddress", "PROTOBUF", paddr);
    KafkaProtobufDeserializer<com.google.protobuf.Message> de = new KafkaProtobufDeserializer<>(); de.configure(conf(), false);

    System.out.println("\n[A] import \"p6/shop/PAddress.proto\" + QualifiedReferenceSubjectNameStrategy");
    String impA = "p6/shop/PAddress.proto";
    String custA = "syntax = \"proto3\";\npackage " + P + ";\nimport \"" + impA + "\";\nmessage QCustomer { PAddress addr = 1; }\n";
    register(P + ".QCustomer", "PROTOBUF", custA, impA, P + ".PAddress");
    DynamicMessage mA = customer("QCustomer", impA, paddr, custA);
    KafkaProtobufSerializer<DynamicMessage> serQ = new KafkaProtobufSerializer<>(); serQ.configure(conf("reference.subject.name.strategy", Q), false);
    roundTrip(attempt("qualified strategy", () -> serQ.serialize(TOPIC, mA)), b -> de.deserialize(TOPIC, b));
    KafkaProtobufSerializer<DynamicMessage> serD = new KafkaProtobufSerializer<>(); serD.configure(conf(), false);
    attempt("default strategy  ", () -> serD.serialize(TOPIC, mA));

    System.out.println("\n[B] import \"p6.shop.PAddress\" (import path = subject name) + default strategy");
    String impB = "p6.shop.PAddress";
    String custB = "syntax = \"proto3\";\npackage " + P + ";\nimport \"" + impB + "\";\nmessage SubjCustomer { PAddress addr = 1; }\n";
    register(P + ".SubjCustomer", "PROTOBUF", custB, impB, P + ".PAddress");
    DynamicMessage mB = customer("SubjCustomer", impB, paddr, custB);
    roundTrip(attempt("default strategy  ", () -> serD.serialize(TOPIC, mB)), b -> de.deserialize(TOPIC, b));

    System.out.println("\n[C] registered with reference name != import path (registry accepts); can Java read it back?");
    String custC = custA.replace("QCustomer", "QCustomerBadRef");
    register(P + ".QCustomerBadRef", "PROTOBUF", custC, "something/else.proto", P + ".PAddress");
    DynamicMessage mC = customer("QCustomerBadRef", impA, paddr, custC);
    roundTrip(attempt("qualified strategy", () -> serQ.serialize(TOPIC, mC)), b -> de.deserialize(TOPIC, b));

    System.out.println("\n[D] transitive: QCustomerDeep -> QMid -> PAddress, qualified strategy");
    String impM = "p6/shop/QMid.proto";
    String mid = "syntax = \"proto3\";\npackage " + P + ";\nimport \"" + impA + "\";\nmessage QMid { PAddress addr = 1; }\n";
    register(P + ".QMid", "PROTOBUF", mid, impA, P + ".PAddress");
    String deep = "syntax = \"proto3\";\npackage " + P + ";\nimport \"" + impM + "\";\nmessage QCustomerDeep { QMid mid = 1; }\n";
    register(P + ".QCustomerDeep", "PROTOBUF", deep, impM, P + ".QMid");
    ProtobufSchema ds = new ProtobufSchema(deep, List.of(new SchemaReference(impM, "x", 1), new SchemaReference(impA, "x", 1)), Map.of(impM, mid, impA, paddr), null, null);
    DynamicMessage mD = DynamicMessage.newBuilder(ds.toDescriptor()).build();
    roundTrip(attempt("qualified strategy", () -> serQ.serialize(TOPIC, mD)), b -> de.deserialize(TOPIC, b));

    System.out.println("\n[E] JSON POJO with @JsonSchemaTitle and latest.compatibility.strict=false");
    io.confluent.kafka.serializers.json.KafkaJsonSchemaSerializer<Object> js = new io.confluent.kafka.serializers.json.KafkaJsonSchemaSerializer<>();
    js.configure(conf("latest.compatibility.strict", false), false);
    attempt("serialize", () -> js.serialize(TOPIC, new Main.TitledOrder()));
    producer.close();
  }
}
