import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Random;
import wordstats.WordStats;

/**
 * Benchmark: fixed, deterministic amount of work.
 *
 * <p>The energy for exactly this work is measured. BENCH_ROUNDS scales the run time
 * (on the measurement machine, choose it so that a run takes >= 20 s).
 */
public final class Run {
    public static void main(String[] args) {
        int rounds = Integer.parseInt(System.getenv().getOrDefault("BENCH_ROUNDS", "40"));
        int vocab = Integer.parseInt(System.getenv().getOrDefault("BENCH_VOCAB", "1500"));
        int words = Integer.parseInt(System.getenv().getOrDefault("BENCH_WORDS", "6000"));

        Random random = new Random(1234);
        List<String> texts = new ArrayList<>();
        for (int t = 0; t < 5; t++) {
            StringBuilder b = new StringBuilder();
            for (int i = 0; i < words; i++) {
                b.append("word").append(random.nextInt(vocab)).append(' ');
            }
            texts.add(b.toString());
        }

        long start = System.nanoTime();
        long checksum = 0;
        for (int i = 0; i < rounds; i++) {
            String text = texts.get(i % texts.size());
            checksum += WordStats.uniqueWords(text).size();
            for (Map.Entry<String, Integer> e : WordStats.frequentWords(text, 20)) {
                checksum += e.getValue();
            }
            checksum += WordStats.report(text, 10).length();
        }
        System.out.printf("rounds=%d checksum=%d seconds=%.2f%n", rounds, checksum, (System.nanoTime() - start) / 1e9);
    }
}
