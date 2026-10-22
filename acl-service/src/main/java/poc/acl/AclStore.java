package poc.acl;

import io.confluent.kafka.schemaregistry.security.authorizer.SchemaRegistryResourceOperation;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.AclMessageSerializer;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.GlobalAclKey;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.GlobalAclValue;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.NoopKey;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SchemaRegistryAclAuthorizerUtils;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SchemaRegistryAclKey;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SchemaRegistryAclReaderThread;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SchemaRegistryAclValue;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SubjectAclKey;
import io.confluent.kafka.schemaregistry.security.authorizer.schemaregistryacl.SubjectAclValue;
import io.confluent.kafka.schemaregistry.security.config.SecureSchemaRegistryConfig;
import io.confluent.kafka.schemaregistry.storage.KafkaStore;
import java.util.ArrayList;
import java.util.EnumSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.clients.producer.RecordMetadata;
import org.apache.kafka.common.serialization.ByteArraySerializer;

/**
 * The ACL store of the Schema Registry ACL authorizer, used in-process the same way
 * sr-acl-cli (SchemaRegistryAclCommand) uses it: ACLs are records on the Kafka topic
 * {@code confluent.schema.registry.acl.topic}, written with the plugin's own serializer and read
 * back by the plugin's own reader thread. Schema Registry is not involved; its authorizer picks
 * the records up from the topic.
 *
 * <p>Unlike the CLI, which replays the whole topic on every invocation, the reader and producer
 * live as long as the service. Add/remove semantics are the CLI's: the new operation set for a
 * (principal, subject) or principal is the current set plus/minus the requested operations, and
 * the whole set is written as one record. Writes are serialized here; two writers elsewhere (a
 * second service instance, or the CLI) racing on the same key can still lose an update, exactly
 * as two concurrent CLI runs can.
 */
public final class AclStore implements AutoCloseable {

  private final SecureSchemaRegistryConfig config;
  private final String topic;
  private final int timeoutMs;
  private final AclMessageSerializer serializer = new AclMessageSerializer();
  private final SchemaRegistryAclAuthorizerUtils utils;
  private final SchemaRegistryAclReaderThread reader;
  private final KafkaProducer<byte[], byte[]> producer;

  // principal -> subject -> ops, and principal -> ops; kept current by the reader thread
  private final Map<String, Map<String, Set<SchemaRegistryResourceOperation>>> subjectOps = new ConcurrentHashMap<>();
  private final Map<String, Set<SchemaRegistryResourceOperation>> globalOps = new ConcurrentHashMap<>();

  public AclStore(SecureSchemaRegistryConfig config) throws Exception {
    this.config = config;
    this.topic = config.aclTopic();
    this.timeoutMs = config.getInt("kafkastore.init.timeout.ms");
    NoopKey noopKey = new NoopKey();
    utils = new SchemaRegistryAclAuthorizerUtils(config, topic, timeoutMs, config.bootstrapBrokers(), serializer, noopKey);
    utils.createOrVerifySchemaTopic();
    reader = new SchemaRegistryAclReaderThread(config, config.bootstrapBrokers(), topic, noopKey, serializer,
        "sr-acl-service", subjectOps, globalOps);
    reader.start();
    catchUp();
    producer = new KafkaProducer<>(producerProps());
  }

  /** Producer settings copied from SchemaRegistryAclCommand.publishAcl. */
  private java.util.Properties producerProps() {
    java.util.Properties p = new java.util.Properties();
    KafkaStore.addSchemaRegistryConfigsToClientProperties(config, p);
    p.put("bootstrap.servers", config.bootstrapBrokers());
    p.put("acks", "-1");
    p.put("key.serializer", ByteArraySerializer.class);
    p.put("value.serializer", ByteArraySerializer.class);
    p.put("retries", 0);
    p.put("enable.idempotence", false);
    p.put("security.protocol", config.getString("kafkastore.security.protocol"));
    return p;
  }

  /** Waits until the reader has applied everything on the topic (writes a no-op marker, like SR). */
  private void catchUp() throws Exception {
    reader.waitUntilOffset(utils.getLatestOffset(timeoutMs), timeoutMs, TimeUnit.MILLISECONDS);
  }

  public record SubjectAcl(String principal, String subject, Set<SchemaRegistryResourceOperation> operations) {}

  public record GlobalAcl(String principal, Set<SchemaRegistryResourceOperation> operations) {}

  public record Acls(List<SubjectAcl> subjectAcls, List<GlobalAcl> globalAcls) {}

  /** Current ACLs from memory; null filters match everything. Empty operation sets are left out. */
  public Acls list(String principal, String subject) {
    List<SubjectAcl> subjects = new ArrayList<>();
    subjectOps.forEach((p, bySubject) -> {
      if (principal != null && !principal.equals(p)) return;
      bySubject.forEach((s, ops) -> {
        if ((subject == null || subject.equals(s)) && !ops.isEmpty()) subjects.add(new SubjectAcl(p, s, copy(ops)));
      });
    });
    List<GlobalAcl> globals = new ArrayList<>();
    if (subject == null) {
      globalOps.forEach((p, ops) -> {
        if ((principal == null || principal.equals(p)) && !ops.isEmpty()) globals.add(new GlobalAcl(p, copy(ops)));
      });
    }
    subjects.sort((a, b) -> (a.principal() + "\0" + a.subject()).compareTo(b.principal() + "\0" + b.subject()));
    globals.sort((a, b) -> a.principal().compareTo(b.principal()));
    return new Acls(subjects, globals);
  }

  /**
   * Adds ({@code add=true}) or removes the requested operations for every principal x subject
   * (subject operations) and every principal (global operations), as in
   * SchemaRegistryAclCommand.processSubjectOperations / processGlobalOperations. Returns the
   * records that were written, with their resulting operation sets.
   */
  public synchronized Acls apply(boolean add, List<String> principals, List<String> subjects,
                                 Set<SchemaRegistryResourceOperation> requested) throws Exception {
    catchUp(); // merge against the latest state, including writes made by the CLI or other instances
    Set<SchemaRegistryResourceOperation> subjectReq = EnumSet.noneOf(SchemaRegistryResourceOperation.class);
    subjectReq.addAll(requested);
    subjectReq.retainAll(SchemaRegistryResourceOperation.SUBJECT_RESOURCE_OPERATIONS);
    Set<SchemaRegistryResourceOperation> globalReq = EnumSet.noneOf(SchemaRegistryResourceOperation.class);
    globalReq.addAll(requested);
    globalReq.retainAll(SchemaRegistryResourceOperation.GLOBAL_RESOURCE_OPERATIONS);

    Map<SchemaRegistryAclKey, SchemaRegistryAclValue> records = new LinkedHashMap<>();
    List<SubjectAcl> subjectResult = new ArrayList<>();
    List<GlobalAcl> globalResult = new ArrayList<>();
    for (String p : principals) {
      if (!subjectReq.isEmpty()) {
        for (String s : subjects) {
          Set<SchemaRegistryResourceOperation> ops = copy(subjectOps.getOrDefault(p, Map.of()).get(s));
          if (add) ops.addAll(subjectReq); else ops.removeAll(subjectReq);
          records.put(new SubjectAclKey(p, s), new SubjectAclValue(p, s, ops));
          subjectResult.add(new SubjectAcl(p, s, ops));
        }
      }
      if (!globalReq.isEmpty()) {
        Set<SchemaRegistryResourceOperation> ops = copy(globalOps.get(p));
        if (add) ops.addAll(globalReq); else ops.removeAll(globalReq);
        records.put(new GlobalAclKey(p), new GlobalAclValue(p, ops));
        globalResult.add(new GlobalAcl(p, ops));
      }
    }
    publish(records);
    return new Acls(subjectResult, globalResult);
  }

  /** As SchemaRegistryAclCommand.publishAcl: partition 0, then wait until the reader has applied the last record. */
  private void publish(Map<SchemaRegistryAclKey, SchemaRegistryAclValue> records) throws Exception {
    Future<RecordMetadata> last = null;
    for (Map.Entry<SchemaRegistryAclKey, SchemaRegistryAclValue> e : records.entrySet()) {
      last = producer.send(new ProducerRecord<>(topic, 0,
          serializer.serializeSchemaRegistryAclKey(e.getKey()), serializer.serializeSchemaRegistryAclValue(e.getValue())));
    }
    if (last != null) {
      long offset = last.get(timeoutMs, TimeUnit.MILLISECONDS).offset();
      reader.waitUntilOffset(offset, timeoutMs, TimeUnit.MILLISECONDS);
    }
  }

  private static Set<SchemaRegistryResourceOperation> copy(Set<SchemaRegistryResourceOperation> ops) {
    Set<SchemaRegistryResourceOperation> out = EnumSet.noneOf(SchemaRegistryResourceOperation.class);
    if (ops != null) out.addAll(ops);
    return out;
  }

  @Override
  public void close() {
    producer.close();
    reader.shutdown();
    utils.shutdown();
  }
}
