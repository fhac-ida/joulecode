/*
 * joule:code – analyses JFR recordings (Java Flight Recorder) in the benchmark container.
 *
 * Usage: java -cp <dir> JfrStats <directory with *.jfr>
 * Environment: JOULECODE_CLASSES = lines "fully.qualified.Class<TAB>relative/file.java"
 *              (the project's top-level classes without protected paths).
 * Output:  one line "JOULECODE_JSON{...}" with self and total time per method.
 *
 * The samples `jdk.ExecutionSample` are counted. Self time belongs to the topmost
 * project method on the stack: JDK and library methods (ArrayList.contains, HashMap.get …)
 * count towards the caller – like `x in some_list` in cProfile and as in the other back-ends.
 * One sample corresponds to the sampling period (settings=profile: 10 ms); the times are
 * therefore estimates; the shares are what matters.
 *
 * Compiled with --release 17, so it runs in every image from Java 17 on (JRE too).
 */

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

import jdk.jfr.consumer.RecordedEvent;
import jdk.jfr.consumer.RecordedFrame;
import jdk.jfr.consumer.RecordedStackTrace;
import jdk.jfr.consumer.RecordingFile;

public final class JfrStats {
    private static final double PERIOD_S = 0.010;

    public static void main(String[] args) throws Exception {
        Map<String, String> classes = new HashMap<>();
        String env = System.getenv().getOrDefault("JOULECODE_CLASSES", "");
        for (String line : env.split("\n")) {
            String[] parts = line.split("\t");
            if (parts.length == 2) {
                classes.put(parts[0], parts[1]);
            }
        }
        Map<String, double[]> stats = new LinkedHashMap<>(); // key → {self, incl, line, samples}
        Map<String, String[]> meta = new HashMap<>(); // key → {file, function}
        long total = 0;
        List<Path> files;
        try (Stream<Path> s = Files.list(Path.of(args[0]))) {
            files = s.filter(p -> p.toString().endsWith(".jfr")).sorted().toList();
        }
        for (Path file : files) {
            for (RecordedEvent e : RecordingFile.readAllEvents(file)) {
                if (!e.getEventType().getName().equals("jdk.ExecutionSample")) {
                    continue;
                }
                RecordedStackTrace st = e.getStackTrace();
                if (st == null) {
                    continue;
                }
                total++;
                boolean selfDone = false;
                Set<String> seen = new HashSet<>();
                for (RecordedFrame f : st.getFrames()) {
                    if (!f.isJavaFrame() || f.getMethod() == null) {
                        continue;
                    }
                    String type = f.getMethod().getType().getName();
                    if (type.contains("$$Lambda")) {
                        continue; // generated lambda class – the work is in lambda$method$n
                    }
                    int dollar = type.indexOf('$');
                    String top = dollar < 0 ? type : type.substring(0, dollar);
                    String rel = classes.get(top);
                    if (rel == null) {
                        continue; // JDK or library – belongs to the next project caller
                    }
                    String simple = type.substring(type.lastIndexOf('.') + 1).replace('$', '.');
                    String function = simple + "." + f.getMethod().getName();
                    String key = rel + "\u0000" + function;
                    double[] s = stats.computeIfAbsent(key, k -> new double[] {0, 0, f.getLineNumber(), 0});
                    meta.putIfAbsent(key, new String[] {rel, function});
                    if (!selfDone) {
                        s[0] += PERIOD_S;
                        s[3] += 1;
                        selfDone = true;
                    }
                    if (seen.add(key)) {
                        s[1] += PERIOD_S;
                    }
                }
            }
        }
        StringBuilder out = new StringBuilder("JOULECODE_JSON{\"total_s\":").append(total * PERIOD_S)
                .append(",\"functions\":[");
        boolean first = true;
        for (Map.Entry<String, double[]> en : stats.entrySet()) {
            String[] m = meta.get(en.getKey());
            double[] s = en.getValue();
            if (!first) {
                out.append(',');
            }
            first = false;
            out.append("{\"file\":").append(quote(m[0])).append(",\"function\":").append(quote(m[1]))
                    .append(",\"line\":").append((int) s[2]).append(",\"ncalls\":0,\"samples\":").append((int) s[3])
                    .append(",\"tottime\":").append(s[0]).append(",\"cumtime\":").append(s[1]).append('}');
        }
        out.append("]}");
        System.out.println(out);
    }

    private static String quote(String s) {
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }
}
