package probe;

import static probe.Main.*;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.File;
import org.apache.avro.Schema;

/** Field defaults with Java Avro's own parser (audit 2 F5). Reads the case file the unit tests use
 *  (tests/schemas/avro_defaults.json, the path is the argument), parses each case's schema with a
 *  default Schema.Parser (names and defaults validated) and exits 1 when a result is not the case's
 *  "java" column. Needs no registry and no Kafka. */
public class Defaults {
  public static void main(String[] args) throws Exception {
    int failures = 0;
    System.out.println("\n[AVRO field defaults with Schema.Parser] " + args[0]);
    for (JsonNode c : M.readTree(new File(args[0]))) {
      String label = c.get("label").asText();
      String result;
      try {
        new Schema.Parser().parse(M.writeValueAsString(c.get("schema")));
        result = "accept";
        System.out.println("  " + label + ": accept");
      } catch (Exception e) {
        result = "reject";
        System.out.println("  " + label + ": reject (" + chain(e) + ")");
      }
      if (!result.equals(c.get("java").asText())) {
        failures++;
        System.out.println("    NOT as expected: the case says " + c.get("java").asText());
      }
    }
    System.out.println(failures == 0 ? "\nDefaults: all cases as expected" : "\nDefaults: " + failures + " case(s) NOT as expected");
    System.exit(failures == 0 ? 0 : 1);
  }
}
